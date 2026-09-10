"""Correcting the phone's recorded rotations against what the photos show.

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

The photos themselves know better than the compass. Two overlapping shots of
the same wall constrain their RELATIVE rotation very precisely, because a pure
rotation between two views is a homography and a homography is recoverable from
a few dozen matched points. This module measures those relative rotations and
finds the set of absolute rotations that agrees with them best.

What it deliberately does NOT do is throw the sensor away. Feature matching
alone cannot tell which way is up: a solution where the entire world is tilted
fifteen degrees fits the photographs exactly as well as the correct one, and
picking the wrong one puts the horizon on a slant and the ceiling off to one
side. So the sensor sets the global frame - gravity and heading - and matching
is only allowed to fix the rotations RELATIVE to each other. Every camera is
also held within a few degrees of where the phone said it was, so one bad match
on a repetitive railing cannot drag the panorama somewhere absurd.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Resolution features are found at. The rotation between two photos is a global
# property of the overlap - it does not need fine detail, and matching full-size
# phone photos costs seconds each for an answer that does not change.
WORK_WIDTH = 640

# Two photos are only worth matching when the sensor already says they overlap.
# Beyond this angle between their optical axes they share nothing, and testing
# them anyway is where false matches on repetitive architecture come from.
# Expressed as a multiple of the field of view.
OVERLAP_FOV_FACTOR = 1.25

# A pair needs this many RANSAC inliers before its measured rotation is trusted.
MIN_INLIERS = 25

# How far a camera may be moved from where the phone said it was. The sensor is
# wrong by a few degrees, not by twenty; a correction larger than this is a
# matching failure, not a sensor failure.
MAX_CORRECTION_DEG = 10.0

# Rotation averaging converges quickly - it is a linear problem being solved by
# repeated projection - and the later passes move things by fractions of a
# degree.
ITERATIONS = 12


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
    cos = (np.trace(a.T @ b) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, float(cos)))))


def _detector():
    if hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=1500)
    return cv2.ORB_create(nfeatures=2000)


def _features(images, work_width):
    det = _detector()
    out = []
    for img in images:
        h, w = img.shape[:2]
        scale = work_width / float(w)
        small = (cv2.resize(img, (work_width, max(1, int(h * scale))),
                            interpolation=cv2.INTER_AREA) if scale < 1 else img)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        kp, desc = det.detectAndCompute(gray, None)
        out.append((kp, desc, small.shape[1], small.shape[0]))
    return out


def _matcher(desc_type):
    if desc_type == np.uint8:                # ORB
        return cv2.BFMatcher(cv2.NORM_HAMMING)
    return cv2.BFMatcher(cv2.NORM_L2)


def _relative_rotation(fi, fj, K, ratio=0.75):
    """Rotation taking camera i's frame to camera j's, from the pixels alone.

    For two views from the SAME point, x_j = K R_ij K^-1 x_i exactly - there is
    no translation term and therefore no depth in the equation. So the
    homography between them is a pure rotation wearing a camera matrix, and
    stripping K off both sides recovers it. This is why a panorama can be solved
    without knowing how far away anything is, and why it stops working the
    moment the photographer walks.

    Returns (R_ij, inlier_count) or (None, 0).
    """
    kp_i, desc_i = fi[0], fi[1]
    kp_j, desc_j = fj[0], fj[1]
    if desc_i is None or desc_j is None or len(kp_i) < 8 or len(kp_j) < 8:
        return None, 0

    try:
        pairs = _matcher(desc_i.dtype.type).knnMatch(desc_i, desc_j, k=2)
    except cv2.error:
        return None, 0

    # Lowe's ratio test: a match is only meaningful when the best candidate is
    # clearly better than the second best. On repetitive architecture - a row of
    # identical windows, a railing - the two are equally good, and this is what
    # stops those from being matched confidently in the wrong place.
    src, dst = [], []
    for pair in pairs:
        if len(pair) == 2 and pair[0].distance < ratio * pair[1].distance:
            src.append(kp_i[pair[0].queryIdx].pt)
            dst.append(kp_j[pair[0].trainIdx].pt)
    if len(src) < MIN_INLIERS:
        return None, 0

    H, mask = cv2.findHomography(np.float32(src), np.float32(dst),
                                 cv2.RANSAC, 3.0, maxIters=2000, confidence=0.995)
    if H is None or mask is None:
        return None, 0
    inliers = int(mask.sum())
    if inliers < MIN_INLIERS:
        return None, inliers

    # H = K R_ij K^-1, so R_ij = K^-1 H K - up to scale, which the projection
    # back onto SO(3) removes along with the accumulated numerical error.
    Kinv = np.linalg.inv(K)
    R = _project_to_so3(Kinv @ H @ K)
    return R, inliers


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


def refine(images, rotations, K, fov_deg, work_width=WORK_WIDTH,
           max_correction_deg=MAX_CORRECTION_DEG):
    """Rotations that agree with the photographs, anchored to the sensor's frame.

    images     BGR frames, in the same order as `rotations`
    rotations  camera-to-world matrices from the sensor, one per image
    K          camera matrix at `work_width` resolution
    fov_deg    horizontal field of view, used only to decide which pairs overlap

    Returns (refined_rotations, stats). On any failure the input rotations come
    back unchanged - a capture placed by a drifting compass is still a capture,
    whereas one placed by a bad homography is a mess.
    """
    n = len(rotations)
    stats = {"pairs_tried": 0, "pairs_used": 0, "max_correction_deg": 0.0,
             "mean_correction_deg": 0.0, "clamped": 0}
    if n < 3:
        return rotations, stats

    try:
        feats = _features(images, work_width)
    except cv2.error as e:
        log.warning("[orbit-worker] rotation refinement: features unavailable (%s)", e)
        return rotations, stats

    pairs = _pairs_to_try(rotations, fov_deg)
    stats["pairs_tried"] = len(pairs)

    # measured[i] holds (j, R_ij, weight): what photo j says photo i's rotation
    # should be, relative to j's own.
    measured = [[] for _ in range(n)]
    for i, j in pairs:
        R_ij, inliers = _relative_rotation(feats[i], feats[j], K)
        if R_ij is None:
            continue
        # Sanity: the measurement must be near what the sensor already believes.
        # A homography fitted to a repeating railing can be confidently wrong,
        # and this is the cheapest place to catch it.
        sensor_ij = rotations[j].T @ rotations[i]
        if _angle_between(sensor_ij, R_ij) > max_correction_deg * 2.0:
            continue
        stats["pairs_used"] += 1
        measured[i].append((j, R_ij, float(inliers)))
        measured[j].append((i, R_ij.T, float(inliers)))

    if stats["pairs_used"] < max(2, n // 4):
        log.info("[orbit-worker] rotation refinement: only %d usable pairs from "
                 "%d tried; keeping the sensor rotations",
                 stats["pairs_used"], stats["pairs_tried"])
        return rotations, stats

    # Rotation averaging. Each neighbour votes for where camera i should be
    # (R_j @ R_ij); the votes are summed as matrices and projected back onto
    # SO(3), which is the closed-form average of a set of rotations. The sensor
    # itself votes too, with a modest weight, so a camera nobody matched stays
    # where the phone put it instead of drifting off.
    current = [R.copy() for R in rotations]
    sensor_weight = max(1.0, np.mean([w for m in measured for _, _, w in m] or [1.0]) * 0.15)
    for _ in range(ITERATIONS):
        updated = []
        for i in range(n):
            acc = current[i] * sensor_weight if not measured[i] else rotations[i] * sensor_weight
            for j, R_ij, weight in measured[i]:
                acc = acc + (current[j] @ R_ij) * weight
            updated.append(_project_to_so3(acc))
        current = updated

    # Put the world back where the sensor said it was.
    #
    # Averaging fixes the cameras relative to each other but leaves the whole
    # set free to rotate as one, and nothing in the photographs objects to that.
    # Left alone it tilts the horizon and swings the heading. So the single
    # global rotation that best maps the refined set back onto the sensor set is
    # found and applied - gravity and compass from the sensor, relative geometry
    # from the photographs, which is what each of them is actually good at.
    acc = np.zeros((3, 3))
    for R_new, R_old in zip(current, rotations):
        acc += R_old @ R_new.T
    align = _project_to_so3(acc)
    current = [_project_to_so3(align @ R) for R in current]

    # Nobody moves more than the sensor could plausibly have been wrong by.
    out = []
    corrections = []
    for R_new, R_old in zip(current, rotations):
        delta = _angle_between(R_old, R_new)
        if delta > max_correction_deg:
            stats["clamped"] += 1
            out.append(R_old)
            continue
        corrections.append(delta)
        out.append(R_new)

    if corrections:
        stats["max_correction_deg"] = round(max(corrections), 2)
        stats["mean_correction_deg"] = round(float(np.mean(corrections)), 2)
    log.info("[orbit-worker] rotation refinement: %d of %d pairs matched, "
             "cameras moved %.1f deg on average (worst %.1f), %d clamped",
             stats["pairs_used"], stats["pairs_tried"],
             stats["mean_correction_deg"], stats["max_correction_deg"],
             stats["clamped"])
    return out, stats
