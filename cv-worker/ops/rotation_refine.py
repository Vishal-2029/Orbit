"""Correcting the phone's recorded poses against what the photos show.

Pose stitching trusts the sensor quaternion completely. That is the right
default - it is the reason a blank wall places as reliably as a bookshelf - but
it is not exact. A phone's heading drifts a few degrees over a couple of
minutes of turning, and its tilt is only as good as the accelerometer's idea of
level while somebody is moving. Over 32 photos the upper ring ends up rotated
slightly against the middle one.

A few degrees does not sound like much. At 4096 pixels around, one degree is
eleven pixels, so a three degree error puts the same window frame in two places
thirty pixels apart. Multi-band blending then shows both of them, which is the
doubled edges and the wavy ceiling lines in the finished sphere.

Rotation is not the only thing that is wrong, and it is not the biggest. Every
photo is a frame grabbed from the live camera video, and phones STABILISE video
by cropping a window that moves from frame to frame. So each photo's optical
centre sits somewhere different - measured on a real 32-photo capture, up to 85
pixels at 640 wide, an eighth of the frame. A shift is not a rotation, and the
previous version of this module could only solve rotations: it turned each
shift into a false rotation to explain it, so both the placement and the angle
came out wrong. That is what broke the window mullions and the stair rails.

So this module solves both at once: one rotation and one image-centre shift per
photo, in a single bundle adjustment over every overlapping pair.

What it deliberately does NOT do is throw the sensor away. Feature matching
alone cannot tell which way is up: a solution where the entire world is tilted
fifteen degrees fits the photographs exactly as well as the correct one, and
picking the wrong one puts the horizon on a slant and the ceiling off to one
side. So the solve is seeded from the sensor, only gyro-overlapping pairs are
ever matched, the result is put back into the sensor's gravity and heading, and
every camera is held within a few degrees of where the phone said it was, so one
bad match on a repetitive railing cannot drag the panorama somewhere absurd.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Resolution features are found at. The pose of a photo is a global property of
# its overlaps - it does not need fine detail, and matching full-size phone
# photos costs seconds each for an answer that does not change.
WORK_WIDTH = 640

# Two photos are only worth matching when the sensor already says they overlap.
# Beyond this angle between their optical axes they share nothing, and testing
# them anyway is where false matches on repetitive architecture come from.
# Expressed as a multiple of the field of view. Tightening it to one frame
# width lost the diagonal pairs that tie one ring to the next, and cameras on
# the rings above and below then came out several degrees off. False matches
# across a sliver of overlap are removed by the sensor check instead.
OVERLAP_FOV_FACTOR = 1.25

# OpenCV's pair confidence (inliers relative to matches) a pair needs before the
# bundle adjuster uses it. This is OpenCV's own stitcher's threshold.
PAIR_CONF = 1.0

# How far a camera may be moved from where the phone said it was. The sensor is
# wrong by a few degrees, not by twenty; a correction larger than this is a
# matching failure, not a sensor failure.
MAX_CORRECTION_DEG = 10.0

# How far a photo's centre may be moved, as a fraction of its width. Video
# stabilisation crops a margin of roughly a tenth of the frame on each side.
MAX_SHIFT_FRAC = 0.15

# A pair still this far out after the solve, in pixels at WORK_WIDTH, is not a
# stabilisation shift or a drifting compass - the phone moved sideways between
# the two shots, and near and far things no longer line up under ANY rotation.
# Such a pair is removed and the rest solved again, so that parallax cannot
# pull its neighbours out of place along with it.
PARALLAX_GATE_PX = 4.0

# The gate is applied in rounds, worst pairs first, re-solving in between. Done
# all at once it judged every pair against a solve the bad pairs had already
# dragged out of place, so on a real capture 44 of 45 failed and nothing was
# refined at all. Four rounds took the same capture to 20 pairs at 1.2 px.
GATE_ROUNDS = 4

# Iterations the bundle adjuster may take per round. OpenCV's default is a
# thousand; a solve that has not settled in sixty is being pulled about by a
# bad pair, which the next round removes anyway. Keeps a round to seconds.
BA_MAX_ITER = 60

# Range the field of view may be measured in, and the least number of pairs
# the measurement needs before it is believed over the default.
FOV_RANGE_DEG = (50, 75)
FOV_MIN_PAIRS = 3
# How close to a pure rotation a pair's homography must come, at its best field
# of view, to count towards measuring it. A stabilised or parallax pair never
# gets there, whatever the field of view, so it cannot bias the answer.
FOV_ROTATION_TOL = 0.03


def _project_to_so3(m):
    """Nearest true rotation matrix to `m`, in the least-squares sense.

    Returned as float32, because these matrices go straight to OpenCV's warper
    and it asserts on CV_32F rather than converting. The SVD itself runs in
    double precision - the whole point of this function is to remove
    accumulated error, so it would be perverse to add some back.
    """
    u, _, vt = np.linalg.svd(np.asarray(m, dtype=np.float64))
    r = u @ vt
    if np.linalg.det(r) < 0:                 # a reflection is not a rotation
        u[:, -1] *= -1
        r = u @ vt
    return r.astype(np.float32)


def _angle_between(a, b):
    """Angle in degrees of the rotation that takes `a` to `b`."""
    cos = (np.trace(np.asarray(a, np.float64).T @ np.asarray(b, np.float64)) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, float(cos)))))


def _detector():
    if hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=1500)
    return cv2.ORB_create(nfeatures=2000)


def _pinhole(width, height, hfov_deg):
    f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0, width / 2.0], [0, f, height / 2.0], [0, 0, 1.0]]), f


def _pairs_to_try(rotations, fov_deg):
    """Index pairs the sensor says overlap, so the rest are never matched."""
    forward = [R[:, 2] for R in rotations]      # the warper's camera looks along +Z
    limit = math.radians(fov_deg * OVERLAP_FOV_FACTOR)
    out = []
    for i in range(len(rotations)):
        for j in range(i + 1, len(rotations)):
            cos = float(np.clip(np.dot(forward[i], forward[j]), -1.0, 1.0))
            if math.acos(cos) <= limit:
                out.append((i, j))
    return out


def _edges(pairwise):
    """Indices of the usable pairs in OpenCV's n*n match table, one per pair."""
    return [p for p, m in enumerate(pairwise)
            if 0 <= m.src_img_idx < m.dst_img_idx and m.confidence > PAIR_CONF]


def _non_rotation(H, K):
    """How far K^-1 H K is from a pure rotation. Zero means exactly one."""
    M = np.linalg.inv(K) @ np.asarray(H, np.float64) @ K
    det = np.linalg.det(M)
    if not np.isfinite(det) or abs(det) < 1e-12:
        return float("inf")
    M = M / np.cbrt(det)
    return float(np.linalg.norm(M - _project_to_so3(M).astype(np.float64)))


def estimate_hfov(pairwise, width, height, default_deg):
    """The camera's horizontal field of view, measured from the photos.

    For two views from one point the homography between them is K R K^-1, so
    with the RIGHT field of view K^-1 H K is exactly a rotation, and with a
    wrong one it is not. Sweeping the field of view and keeping the one that
    makes the pairs most rotation-like measures it without any calibration.
    Measured on a real capture it came out at 63 degrees with a sharp minimum,
    where the code had assumed 65.

    Only pairs that come close to a rotation at their best field of view are
    counted: a stabilised or parallax pair is not a rotation at any, and would
    only pull the answer about. Too few clean pairs, and the default stands.
    """
    fovs = np.arange(FOV_RANGE_DEG[0], FOV_RANGE_DEG[1] + 0.25, 0.5)
    # OpenCV's matcher fits each pair's homography in coordinates CENTRED on
    # the middle of the image, so the camera matrix has its centre at the
    # origin here. With the centre at (w/2, h/2) no pair ever looked like a
    # rotation and the measurement silently never ran.
    Ks = []
    for f in fovs:
        K = _pinhole(width, height, f)[0]
        K[0, 2] = K[1, 2] = 0.0
        Ks.append(K)
    scores = []
    for p in _edges(pairwise):
        H = pairwise[p].H
        if H is None or np.asarray(H).shape != (3, 3):
            continue
        row = np.array([_non_rotation(H, K) for K in Ks])
        if np.isfinite(row).all() and row.min() < FOV_ROTATION_TOL:
            scores.append(row)
    if len(scores) < FOV_MIN_PAIRS:
        return default_deg, len(scores)
    best = float(fovs[int(np.argmin(np.median(np.array(scores), axis=0)))])
    # At either end of the range the true answer may lie beyond it, so the
    # measurement says nothing trustworthy.
    if best <= FOV_RANGE_DEG[0] or best >= FOV_RANGE_DEG[1]:
        return default_deg, len(scores)
    return best, len(scores)


def _camera(R, focal, cx, cy):
    c = cv2.detail.CameraParams()
    c.focal = float(focal)
    c.aspect = 1.0
    c.ppx = float(cx)
    c.ppy = float(cy)
    c.R = np.asarray(R, np.float32)
    c.t = np.zeros((3, 1), np.float64)
    return c


def _pair_error(pairwise, p, feats, cams):
    """Median reprojection error of one pair's inliers under the solved poses."""
    m = pairwise[p]
    i, j = m.src_img_idx, m.dst_img_idx
    H = (cams[j].K() @ cams[j].R.astype(np.float64).T @ cams[i].R.astype(np.float64)
         @ np.linalg.inv(cams[i].K()))
    kp_i, kp_j = feats[i].getKeypoints(), feats[j].getKeypoints()
    inl = m.getInliers()
    src, dst = [], []
    for k, mt in enumerate(m.getMatches()):
        if inl[k]:
            src.append(kp_i[mt.queryIdx].pt)
            dst.append(kp_j[mt.trainIdx].pt)
    if len(src) < 8:
        return None
    proj = cv2.perspectiveTransform(np.float32(src).reshape(-1, 1, 2), H).reshape(-1, 2)
    return float(np.median(np.linalg.norm(proj - np.float32(dst), axis=1)))


def _drop_inconsistent_pairs(pairwise, rotations, width, height, hfov_deg, limit_deg):
    """Remove pairs whose match disagrees with the sensor by more than limit_deg.

    The sensor is wrong by a few degrees, not by twenty. A pair whose homography
    implies a rotation that far from what the phone recorded has matched the
    wrong railing, window or stair edge, and left in it drags the first solve
    so far out that every good pair then looks bad too. The matcher fits each
    homography in coordinates centred on the image, hence the centred K.

    Returns how many pairs were removed.
    """
    K, _ = _pinhole(width, height, hfov_deg)
    K[0, 2] = K[1, 2] = 0.0
    Kinv = np.linalg.inv(K)
    dropped = 0
    for p in _edges(pairwise):
        m = pairwise[p]
        H = m.H
        if H is None or np.asarray(H).shape != (3, 3):
            continue
        # H takes photo src to photo dst: x_dst = K R_dst^T R_src K^-1 x_src.
        photo = _project_to_so3(Kinv @ np.asarray(H, np.float64) @ K)
        sensor = (np.asarray(rotations[m.dst_img_idx], np.float64).T
                  @ np.asarray(rotations[m.src_img_idx], np.float64))
        if _angle_between(sensor, photo) > limit_deg:
            _drop_pair(pairwise, p)
            dropped += 1
    return dropped


def _drop_pair(pairwise, p):
    m = pairwise[p]
    a, b = m.src_img_idx, m.dst_img_idx
    for q in pairwise:
        if (q.src_img_idx, q.dst_img_idx) in ((a, b), (b, a)):
            q.confidence = 0.0


def _unchanged(rotations, stats, hfov_deg):
    return ([np.asarray(R, np.float32) for R in rotations],
            [(0.0, 0.0)] * len(rotations), hfov_deg, stats)


def refine_poses(images, rotations, hfov_deg, work_width=WORK_WIDTH,
                 max_correction_deg=MAX_CORRECTION_DEG, measure_fov=True):
    """Rotations and image-centre shifts that agree with the photographs.

    images     BGR frames, in the same order as `rotations`, all one size
    rotations  camera-to-world matrices from the sensor, one per image
    hfov_deg   the field of view to assume, and to fall back on
    measure_fov  measure the field of view from the photos first

    Returns (rotations, shifts, hfov_deg, stats). `shifts` is one (dx, dy) per
    photo in pixels of the images as given: where that photo's optical centre
    actually sits relative to the middle of the frame. On any failure the
    sensor rotations come back with zero shifts - a capture placed by a
    drifting compass is still a capture, whereas one placed by a bad solve is a
    mess.
    """
    n = len(rotations)
    stats = {"pairs_tried": 0, "pairs_used": 0, "parallax_dropped": 0,
             "inconsistent_dropped": 0,
             "max_correction_deg": 0.0, "mean_correction_deg": 0.0,
             "max_shift_px": 0.0, "clamped": 0, "hfov_deg": hfov_deg,
             "fov_pairs": 0}
    if n < 3:
        return _unchanged(rotations, stats, hfov_deg)

    h, w = images[0].shape[:2]
    scale = min(1.0, work_width / float(w))
    sw, sh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))

    try:
        finder = _detector()
        feats = []
        for i, img in enumerate(images):
            small = (cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)
                     if img.shape[1] != sw else img)
            ft = cv2.detail.computeImageFeatures2(finder, small)
            ft.img_idx = i
            feats.append(ft)

        pairs = _pairs_to_try(rotations, hfov_deg)
        stats["pairs_tried"] = len(pairs)
        mask = np.zeros((n, n), np.uint8)
        for i, j in pairs:
            mask[i, j] = mask[j, i] = 1
        matcher = cv2.detail.BestOf2NearestMatcher_create(False, 0.3)
        pairwise = matcher.apply2(feats, mask)
        matcher.collectGarbage()
    except cv2.error as e:
        log.warning("[orbit-worker] pose refinement: features unavailable (%s)", e)
        return _unchanged(rotations, stats, hfov_deg)

    stats["inconsistent_dropped"] = _drop_inconsistent_pairs(
        pairwise, rotations, sw, sh, hfov_deg, 2.0 * max_correction_deg)

    if measure_fov:
        hfov_deg, stats["fov_pairs"] = estimate_hfov(pairwise, sw, sh, hfov_deg)
        stats["hfov_deg"] = hfov_deg
    _, focal = _pinhole(sw, sh, hfov_deg)

    edges = _edges(pairwise)
    if len(edges) < max(2, n // 4):
        log.info("[orbit-worker] pose refinement: only %d usable pairs from %d "
                 "tried; keeping the sensor rotations", len(edges), len(pairs))
        stats["pairs_used"] = len(edges)
        return _unchanged(rotations, stats, hfov_deg)

    # Rotations are always refined by the adjuster; the mask adds the image
    # centre (ppx, ppy) and nothing else. Focal length is left alone - it was
    # just measured, and freeing it lets the solver trade it against the
    # shifts - and phone pixels are square.
    refine_mask = np.zeros((3, 3), np.uint8)
    refine_mask[0, 2] = refine_mask[1, 2] = 1
    cams = None
    try:
        for rnd in range(GATE_ROUNDS + 1):
            adjuster = cv2.detail_BundleAdjusterReproj()
            adjuster.setConfThresh(PAIR_CONF)
            adjuster.setRefinementMask(refine_mask)
            adjuster.setTermCriteria(
                (cv2.TERM_CRITERIA_COUNT + cv2.TERM_CRITERIA_EPS, BA_MAX_ITER, 1e-4))
            seed = [_camera(R, focal, sw / 2.0, sh / 2.0) for R in rotations]
            ok, cams = adjuster.apply(feats, pairwise, seed)
            if not ok or not cams:
                log.info("[orbit-worker] pose refinement: the solve did not converge; "
                         "keeping the sensor rotations")
                return _unchanged(rotations, stats, hfov_deg)
            errors = [(_pair_error(pairwise, p, feats, cams) or 0.0, p)
                      for p in _edges(pairwise)]
            above = [(e, p) for e, p in errors if e > PARALLAX_GATE_PX]
            if not above or rnd == GATE_ROUNDS:
                break
            # The worst first: everything past twice the typical error, or if
            # nothing is that far out, whatever is still over the gate.
            cut = max(PARALLAX_GATE_PX, 2.0 * float(np.median([e for e, _ in errors])))
            bad = [p for e, p in above if e > cut] or [p for _, p in above]
            for p in bad:
                _drop_pair(pairwise, p)
            stats["parallax_dropped"] += len(bad)
            if len(_edges(pairwise)) < max(2, n // 4):
                log.info("[orbit-worker] pose refinement: %d of %d pairs show "
                         "parallax; keeping the sensor rotations",
                         stats["parallax_dropped"], len(edges))
                return _unchanged(rotations, stats, hfov_deg)
    except cv2.error as e:
        log.warning("[orbit-worker] pose refinement: solve failed (%s)", e)
        return _unchanged(rotations, stats, hfov_deg)
    stats["pairs_used"] = len(_edges(pairwise))

    # Put the world back where the sensor said it was.
    #
    # The solve fixes the cameras relative to each other but leaves the whole
    # set free to rotate as one, and nothing in the photographs objects to that.
    # Left alone it tilts the horizon and swings the heading. So the single
    # global rotation that best maps the solved set back onto the sensor set is
    # found and applied - gravity and compass from the sensor, relative geometry
    # from the photographs, which is what each of them is actually good at.
    solved = [c.R.astype(np.float64) for c in cams]
    acc = np.zeros((3, 3))
    for R_new, R_old in zip(solved, rotations):
        acc += np.asarray(R_old, np.float64) @ R_new.T
    align = _project_to_so3(acc).astype(np.float64)
    solved = [_project_to_so3(align @ R) for R in solved]

    # Nobody moves more than the sensor could plausibly have been wrong by, and
    # no centre moves further than stabilisation can crop.
    out_r, out_s, corrections, shift_sizes = [], [], [], []
    limit_px = MAX_SHIFT_FRAC * w
    for R_new, R_old, cam in zip(solved, rotations, cams):
        dx = (cam.ppx - sw / 2.0) / scale
        dy = (cam.ppy - sh / 2.0) / scale
        delta = _angle_between(R_old, R_new)
        if delta > max_correction_deg or abs(dx) > limit_px or abs(dy) > limit_px:
            stats["clamped"] += 1
            out_r.append(np.asarray(R_old, np.float32))
            out_s.append((0.0, 0.0))
            continue
        corrections.append(delta)
        shift_sizes.append(math.hypot(dx, dy))
        out_r.append(R_new)
        out_s.append((float(dx), float(dy)))

    if corrections:
        stats["max_correction_deg"] = round(max(corrections), 2)
        stats["mean_correction_deg"] = round(float(np.mean(corrections)), 2)
        stats["max_shift_px"] = round(max(shift_sizes), 1)
    log.info("[orbit-worker] pose refinement: %d of %d pairs used (%d disagreed with "
             "the sensor, %d dropped for parallax), field of view %.1f deg from %d "
             "pairs, cameras moved %.1f deg on average (worst %.1f), centres shifted "
             "up to %.0f px, %d clamped",
             stats["pairs_used"], stats["pairs_tried"], stats["inconsistent_dropped"],
             stats["parallax_dropped"],
             hfov_deg, stats["fov_pairs"], stats["mean_correction_deg"],
             stats["max_correction_deg"], stats["max_shift_px"], stats["clamped"])
    return out_r, out_s, hfov_deg, stats


def refine(images, rotations, K, fov_deg, work_width=WORK_WIDTH,
           max_correction_deg=MAX_CORRECTION_DEG):
    """Rotations only, at a fixed field of view - see refine_poses.

    K is accepted for compatibility and not needed: the solve builds its own
    camera matrix at `work_width` from `fov_deg`.
    """
    rots, _, _, stats = refine_poses(images, rotations, fov_deg, work_width=work_width,
                                     max_correction_deg=max_correction_deg,
                                     measure_fov=False)
    return rots, stats
