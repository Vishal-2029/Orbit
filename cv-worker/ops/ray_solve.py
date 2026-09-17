"""Camera rotations and field of view, solved from the rays two photos share.

refine_poses asks OpenCV's BestOf2NearestMatcher which photos overlap and
trusts a pair only above a fixed confidence. On a capture pointed at a plain
floor that throws nearly everything away: a real 9-photo ring, tilted down 45
degrees at a white tiled balcony, kept 5 of 27 pairs, could not measure the
field of view "from 0 pairs", and fell back on the phone's compass. The compass
had drifted up to ten degrees partway round, and the lens was assumed to see 63
degrees when it saw 74. The finished 360 showed both at once: a top edge that
climbed in steps, one photo at a time, and window frames broken at every join.

This asks the question more directly. For each pair the sensor says overlaps,
match features, turn the matches into rays through the lens, and find the one
rotation that carries one photo's rays onto the other's (Kabsch inside RANSAC).
A pure rotation fits genuinely matched rays only at the RIGHT field of view, so
the count of rays that agree peaks there - that is the measurement. The pair
rotations are then reconciled into one rotation per camera, held near the
sensor so it still decides which way is up and where north is.

On that ring: field of view 74, seams from 26-119 px down to 2-7 px on every
pair that could be matched, and the stepped edge gone.

It never replaces a result on faith. The caller compares its reprojection error
against whatever it already had and keeps the better one.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Width the photos are matched at. Detail on a low-texture floor is the whole
# problem, so this is higher than refine_poses' 640.
WORK_WIDTH = 1024

# Pairs further apart than this, by the sensor, cannot overlap.
MAX_PAIR_ANGLE_DEG = 95.0

# A matched ray within this many pixels of where the rotation puts it agrees.
INLIER_PX = 4.0

# Fewer agreeing rays than this and the pair says nothing trustworthy.
MIN_INLIERS = 8

# The sensor is trusted very differently about the two halves of a rotation.
#
# TILT - pitch and roll - comes from gravity, which the phone always knows.
# On a real 32-photo sphere the photos agreed with the sensor's tilt to within
# a degree or two on nearly every pair.
#
# HEADING - yaw - has no fixed reference in gyro mode, and it jumps. The same
# capture had shots recorded 15-30 degrees from where the photos put them: one
# logged at 317 degrees sat at 300, right on its dot. A single limit for both
# halves treated those real heading corrections as a broken solve and threw the
# whole thing away, leaving a 32-photo sphere placed on the bad headings.
#
# So a pair is judged on tilt alone, and heading is left to the photos.
MAX_PAIR_TILT_DISAGREEMENT_DEG = 8.0
MAX_PAIR_YAW_DISAGREEMENT_DEG = 60.0

# How firmly each camera is held to its sensor rotation, per axis of a
# world-frame correction. Tilt is held hard; heading only lightly - enough to
# pin down a camera no pair reaches, and the capture's overall facing.
TILT_PRIOR = 3.0
YAW_PRIOR = 0.02

# A single correction beyond these means the solve has come apart.
MAX_TILT_CORRECTION_DEG = 10.0
MAX_YAW_CORRECTION_DEG = 60.0

# Residual, in degrees, beyond which a pair is progressively down-weighted, so
# one false match cannot drag its cameras across the sphere.
ROBUST_SCALE_DEG = 3.0

# World up in the warper's frame (+Y is down). Only the axis matters here.
_VERTICAL = np.array([0.0, 1.0, 0.0])

FOV_RANGE_DEG = (50, 90)


def _rays(pts, K_inv):
    r = np.c_[pts, np.ones(len(pts))] @ K_inv.T
    return r / np.linalg.norm(r, axis=1, keepdims=True)


def _kabsch(A, B):
    """Rotation R minimising |R A - B| over matched unit rays."""
    U, _, Vt = np.linalg.svd(B.T @ A)
    return U @ np.diag([1.0, 1.0, np.linalg.det(U @ Vt)]) @ Vt


def _angle(A, B):
    return math.degrees(math.acos(np.clip((np.trace(A.T @ B) - 1) / 2, -1, 1)))


def _project(rays, R, K):
    p = rays @ R.T @ K.T
    return p[:, :2] / p[:, 2:3]


def _K(w, h, hfov_deg, shift=(0.0, 0.0)):
    f = (w / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0, w / 2.0 + shift[0]],
                     [0, f, h / 2.0 + shift[1]],
                     [0, 0, 1.0]])


class RaySolver:
    """Features are found once; error() and solve() reuse them."""

    def __init__(self, images, sensor_rotations, work_width=WORK_WIDTH):
        self.n = len(images)
        self.sensor = [np.asarray(R, np.float64) for R in sensor_rotations]
        h, w = images[0].shape[:2]
        self.scale = min(1.0, work_width / float(w))
        self.w = max(1, int(round(w * self.scale)))
        self.h = max(1, int(round(h * self.scale)))
        self.pairs = {}

        # A lower contrast threshold than SIFT's 0.04. Dark granite stairs and a
        # shaded floor hold plenty of texture, but faint: at the default, a real
        # join between two stairwell photos kept 7 rotation-consistent matches -
        # too few to use, so the join was left to the drifting compass. At 0.02
        # it kept 21, for well under a second more.
        sift = cv2.SIFT_create(nfeatures=8000, contrastThreshold=0.02)
        feats = []
        for img in images:
            small = cv2.resize(img, (self.w, self.h), interpolation=cv2.INTER_AREA) \
                if img.shape[1] != self.w else img
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            feats.append(sift.detectAndCompute(gray, None))

        matcher = cv2.BFMatcher()
        for a in range(self.n):
            for b in range(a + 1, self.n):
                cos = float(np.clip(self.sensor[a][:, 2] @ self.sensor[b][:, 2], -1, 1))
                if math.degrees(math.acos(cos)) > MAX_PAIR_ANGLE_DEG:
                    continue
                (ka, da), (kb, db) = feats[a], feats[b]
                # knnMatch(k=2) asserts on fewer than two descriptors - the
                # same trap that took refine_poses down on a blank ceiling.
                if da is None or db is None or len(da) < 2 or len(db) < 2:
                    continue
                good = [m for m, s in matcher.knnMatch(da, db, k=2)
                        if m.distance < 0.75 * s.distance]
                if len(good) < 15:
                    continue
                good = sorted(good, key=lambda m: m.distance)[:400]
                self.pairs[(a, b)] = (
                    np.float32([ka[m.queryIdx].pt for m in good]),
                    np.float32([kb[m.trainIdx].pt for m in good]))

    def _pair_rotation(self, pa, pb, K, iters=200):
        """Best pure rotation carrying a's rays to b's, and its inlier mask."""
        K_inv = np.linalg.inv(K)
        ra, rb = _rays(pa, K_inv), _rays(pb, K_inv)
        rng = np.random.default_rng(1)
        best = None
        for _ in range(iters):
            s = rng.choice(len(ra), 3, replace=False)
            R = _kabsch(ra[s], rb[s])
            inl = np.linalg.norm(_project(ra, R, K) - pb, axis=1) < INLIER_PX
            if best is None or inl.sum() > best.sum():
                best = inl
        if best.sum() < MIN_INLIERS:
            return None, best
        return _kabsch(ra[best], rb[best]), best

    def _measure_fov(self, hint):
        """The field of view at which the most matched rays fit pure rotations."""
        # The busiest pairs decide it; scanning every pair at every angle costs
        # far more on a 30-photo sphere and changes the answer by nothing.
        ranked = sorted(self.pairs.values(), key=lambda p: -len(p[0]))[:12]

        def score(fov):
            K = _K(self.w, self.h, fov)
            return sum(int(self._pair_rotation(pa, pb, K, iters=120)[1].sum())
                       for pa, pb in ranked)

        coarse = {f: score(f) for f in range(FOV_RANGE_DEG[0], FOV_RANGE_DEG[1] + 1, 4)}
        centre = max(coarse, key=coarse.get)
        fine = {f: score(f) for f in range(max(FOV_RANGE_DEG[0], centre - 3),
                                           min(FOV_RANGE_DEG[1], centre + 3) + 1)}
        fov = max(fine, key=fine.get)
        # A flat curve is no measurement: keep what the caller believed.
        if fine[fov] < 40 or fine[fov] < 1.15 * min(coarse.values()):
            return float(hint), fine[fov]
        return float(fov), fine[fov]

    def error(self, rotations, hfov_deg, shifts=None, keys=None):
        """Median, over pairs, of the typical pixel miss between matched points.

        Only over `keys` - the pairs the solve found genuinely matched - so both
        sets of poses are judged on the same, real overlaps. Scoring every pair
        the sensor thought might overlap let junk matches from pairs that share
        nothing swamp the figure at over a thousand pixels either way.

        The 30th percentile inside each pair rather than the median: matches
        include outliers whatever the poses, and what should fall as the poses
        improve is how far the GOOD matches land from each other.
        """
        errs = []
        for (a, b), (pa, pb) in self.pairs.items():
            if keys is not None and (a, b) not in keys:
                continue
            sa = sb = (0.0, 0.0)
            if shifts is not None:
                sa = (shifts[a][0] * self.scale, shifts[a][1] * self.scale)
                sb = (shifts[b][0] * self.scale, shifts[b][1] * self.scale)
            Ka, Kb = _K(self.w, self.h, hfov_deg, sa), _K(self.w, self.h, hfov_deg, sb)
            Ra, Rb = np.asarray(rotations[a], float), np.asarray(rotations[b], float)
            v = _rays(pa, np.linalg.inv(Ka)) @ (Rb.T @ Ra).T
            front = v[:, 2] > 1e-6
            if front.sum() < MIN_INLIERS:
                errs.append(float(max(self.w, self.h)))
                continue
            p = v[front] @ Kb.T
            p = p[:, :2] / p[:, 2:3]
            errs.append(float(np.percentile(np.linalg.norm(p - pb[front], axis=1), 30)))
        return float(np.median(errs)) if errs else float("inf")

    def solve(self, hfov_hint):
        """(rotations, hfov_deg, stats), or None when the photos say too little."""
        if len(self.pairs) < max(2, self.n // 3):
            return None
        fov, fov_score = self._measure_fov(hfov_hint)
        K = _K(self.w, self.h, fov)

        rel = {}
        for (a, b), (pa, pb) in self.pairs.items():
            R, inl = self._pair_rotation(pa, pb, K)
            if R is None:
                continue
            tilt, yaw = _tilt_yaw_disagreement(R, self.sensor[a], self.sensor[b])
            if tilt > MAX_PAIR_TILT_DISAGREEMENT_DEG or yaw > MAX_PAIR_YAW_DISAGREEMENT_DEG:
                continue
            rel[(a, b)] = (R, int(inl.sum()))
        if len(rel) < max(2, self.n // 3):
            return None

        # Gauss-Newton on world-frame corrections: R_i = exp(c_i) S_i, where the
        # prior on c_i is strong about the horizontal axes (tilt) and weak about
        # the vertical (heading). Pair residual e = log(Rab^T R_b^T R_a);
        # left-perturbing R_a by d gives e + R_a^T d, and R_b by d gives
        # e - R_a^T d. Robustly reweighted, so a false match loses its pull.
        n = self.n
        R = [S.copy() for S in self.sensor]
        prior_w = np.diag([TILT_PRIOR, YAW_PRIOR, TILT_PRIOR])
        robust = math.radians(ROBUST_SCALE_DEG)

        for _ in range(60):
            rows, rhs = [], []
            for (a, b), (Rab, count) in rel.items():
                e = cv2.Rodrigues(Rab.T @ R[b].T @ R[a])[0].ravel()
                weight = math.sqrt(count) / (1.0 + (np.linalg.norm(e) / robust) ** 2)
                J = np.zeros((3, 3 * n))
                J[:, 3 * a:3 * a + 3] = R[a].T
                J[:, 3 * b:3 * b + 3] = -R[a].T
                rows.append(J * weight)
                rhs.append(-e * weight)
            for i in range(n):
                c = cv2.Rodrigues(R[i] @ self.sensor[i].T)[0].ravel()
                J = np.zeros((3, 3 * n))
                J[:, 3 * i:3 * i + 3] = prior_w
                rows.append(J)
                rhs.append(-(prior_w @ c))
            step = np.linalg.lstsq(np.vstack(rows), np.concatenate(rhs), rcond=None)[0]
            for i in range(n):
                R[i] = cv2.Rodrigues(step[3 * i:3 * i + 3])[0] @ R[i]
            if np.abs(step).max() < 1e-7:
                break

        solved = [r.astype(np.float32) for r in R]
        tilts, yaws = [], []
        for i in range(n):
            t, y = _correction_tilt_yaw(R[i] @ self.sensor[i].T)
            tilts.append(t)
            yaws.append(y)
        if max(tilts) > MAX_TILT_CORRECTION_DEG or max(yaws) > MAX_YAW_CORRECTION_DEG:
            log.info("[orbit-worker] ray solve came apart (a camera tilted %.0f deg, "
                     "turned %.0f deg); not used", max(tilts), max(yaws))
            return None
        moved = [_angle(self.sensor[i], solved[i]) for i in range(n)]
        stats = {"hfov_deg": fov, "fov_score": fov_score, "pairs": len(rel),
                 "pair_keys": set(rel),
                 "pairs_matched": len(self.pairs),
                 "mean_correction_deg": float(np.mean(moved)),
                 "max_correction_deg": float(max(moved)),
                 "max_tilt_correction_deg": float(max(tilts)),
                 "max_yaw_correction_deg": float(max(yaws))}
        return solved, fov, stats


def _correction_tilt_yaw(C):
    """Split a world-frame correction into how far it tips the vertical (tilt)
    and how far it turns about it (heading), in degrees."""
    tilt = math.degrees(math.acos(np.clip(C @ _VERTICAL @ _VERTICAL, -1, 1)))
    # Heading: the turn of the horizontal forward axis about the vertical.
    f = np.array([0.0, 0.0, 1.0])
    g = C @ f
    g[1] = 0.0
    if np.linalg.norm(g) < 1e-9:
        return tilt, 0.0
    g /= np.linalg.norm(g)
    return tilt, math.degrees(math.acos(np.clip(g @ f, -1, 1)))


def _tilt_yaw_disagreement(Rab, Sa, Sb):
    """How far a photo-derived pair rotation disagrees with the sensor, split
    into tilt (where it sends 'up') and heading (the rest), in degrees."""
    up_a, up_b = Sa.T @ _VERTICAL, Sb.T @ _VERTICAL
    tilt = math.degrees(math.acos(np.clip((Rab @ up_a) @ up_b, -1, 1)))
    # The world rotation that turns the sensor's b onto the photos' b.
    C = Sa @ Rab.T @ Sb.T
    _, yaw = _correction_tilt_yaw(C)
    return tilt, yaw
