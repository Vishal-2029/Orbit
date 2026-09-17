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

# A pair rotation this far from what the sensor predicted is a false match -
# repeated tiles, a railing matched one bay along - not a compass error.
MAX_PAIR_DISAGREEMENT_DEG = 20.0

# How firmly each camera is held to its sensor rotation, against the pairs.
# Weak enough that a matched pair corrects real drift; strong enough that a
# camera no pair constrains stays where the sensor put it instead of wandering.
SENSOR_PRIOR = 1.0

# Any single correction beyond this means the solve has come apart.
MAX_CORRECTION_DEG = 25.0

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

        sift = cv2.SIFT_create(nfeatures=5000)
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
            if _angle(R, self.sensor[b].T @ self.sensor[a]) > MAX_PAIR_DISAGREEMENT_DEG:
                continue
            rel[(a, b)] = (R, int(inl.sum()))
        if len(rel) < max(2, self.n // 3):
            return None

        # Gauss-Newton on R_i = S_i exp(w_i), residual log(Rab^T R_b^T R_a).
        # Right-perturbing R_a adds w_a to the residual; R_b enters through
        # exp(-Rab^T w_b) on the left, which becomes -E^T Rab^T w_b on the right.
        n = self.n
        W = np.zeros((n, 3))

        def rot(i):
            return self.sensor[i] @ cv2.Rodrigues(W[i])[0]

        for _ in range(40):
            rows, rhs = [], []
            for (a, b), (Rab, count) in rel.items():
                E = Rab.T @ rot(b).T @ rot(a)
                e = cv2.Rodrigues(E)[0].ravel()
                weight = math.sqrt(count)
                J = np.zeros((3, 3 * n))
                J[:, 3 * a:3 * a + 3] = np.eye(3)
                J[:, 3 * b:3 * b + 3] = -E.T @ Rab.T
                rows.append(J * weight)
                rhs.append(-e * weight)
            prior = np.eye(3 * n) * SENSOR_PRIOR
            rows.append(prior)
            rhs.append(-W.ravel() * SENSOR_PRIOR)
            step = np.linalg.lstsq(np.vstack(rows), np.concatenate(rhs), rcond=None)[0]
            W += step.reshape(n, 3)
            if np.abs(step).max() < 1e-7:
                break

        solved = [rot(i).astype(np.float32) for i in range(n)]
        moved = [_angle(self.sensor[i], solved[i]) for i in range(n)]
        if max(moved) > MAX_CORRECTION_DEG:
            log.info("[orbit-worker] ray solve came apart (a camera moved %.0f deg); "
                     "not used", max(moved))
            return None
        stats = {"hfov_deg": fov, "fov_score": fov_score, "pairs": len(rel),
                 "pair_keys": set(rel),
                 "pairs_matched": len(self.pairs),
                 "mean_correction_deg": float(np.mean(moved)),
                 "max_correction_deg": float(max(moved))}
        return solved, fov, stats
