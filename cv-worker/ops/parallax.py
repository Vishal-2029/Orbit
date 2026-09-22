"""Did the camera move between two photos, or only turn?

A 360 is built on the assumption that every photo was taken from the same
point: the camera turns, it does not travel. Hold a phone at arm's length and
turn your body, and the lens swings round a circle about half a metre across -
it travels. Nothing in the room moves, but the viewpoint does, and close things
then shift against far things between one photo and the next. That is the
difference between two eyes: close one, then the other, and your thumb jumps
against the wall.

No rotation can line up both the near and the far part of two such photos, so
the join tears exactly where something is close - a staircase, a shutter door, a
corridor wall. It cannot be fixed afterwards by any stitcher that places photos
by turning them. It CAN be fixed by reshooting those photos turning on the spot,
which is why this runs while the photographer is still standing there.

The test fits two explanations to the same matched points: the camera only
TURNED (a rotation), or it turned and MOVED (an essential matrix, which allows
the viewpoint to shift). When the photos were taken from one point both explain
the matches equally, because a rotation is all there is. When the camera moved,
the second explains far more of them.

Calibrated on a real 32-photo corridor capture, against what was visibly wrong in
its finished 360:

    clean joins            turned 86 / moved 84, turned 61 / moved 61   (ratio ~1.0)
    staircase jog          turned 67 / moved 159                         (2.4)
    torn shutter doors     turned 23 / moved 44, 18 / 37                 (1.9, 2.1)
    doubled corridor wall  turned 1  / moved 18,  2 / 21                 (very high)

Every flagged join matched a fault that could be seen, and no clean one was
flagged.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Width photos are compared at. Enough detail to match a plain wall, small
# enough that a ring of twelve checks in a few seconds.
WORK_WIDTH = 1024

# A matched point within this many pixels of where the turn puts it agrees.
TURN_INLIER_PX = 4.0

# How many more points "turned and moved" must explain than "only turned" before
# the camera is said to have moved. The clean joins on the calibration capture
# ran 0.98-1.03; the faulty ones 1.9 and up. A join at 1.56 there showed only a
# faint seam, so the line sits clear of it rather than on it.
MOVED_RATIO = 1.6

# And the "moved" explanation must rest on enough points to mean anything; a
# pair with a dozen matches in total says nothing either way.
MIN_MOVED_INLIERS = 15

# A second way to be flagged, for photos with plenty of detail. The ratio says
# what SHARE of the matches is near enough to shift; in a busy scene the far
# wall can hold most of the matches and keep the ratio low while dozens of near
# points still refuse to line up. A turn and a move explained the same number
# of points to within a couple at every clean join measured, so forty more is
# no accident of the looser model.
MOVED_EXTRA = 40
MOVED_EXTRA_RATIO = 1.3


def _k(w, h, hfov_deg):
    f = (w / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1.0]])


def _rays(pts, k_inv):
    r = np.c_[pts, np.ones(len(pts))] @ k_inv.T
    return r / np.linalg.norm(r, axis=1, keepdims=True)


def _kabsch(a, b):
    u, _, vt = np.linalg.svd(b.T @ a)
    return u @ np.diag([1.0, 1.0, np.linalg.det(u @ vt)]) @ vt


def _turn_inliers(pa, pb, k, iters=1200):
    """How many matched points a single pure rotation can explain."""
    k_inv = np.linalg.inv(k)
    ra, rb = _rays(pa, k_inv), _rays(pb, k_inv)
    rng = np.random.default_rng(0)
    best = 0
    for _ in range(iters):
        s = rng.choice(len(ra), 3, replace=False)
        r = _kabsch(ra[s], rb[s])
        p = ra @ r.T @ k.T
        p = p[:, :2] / p[:, 2:3]
        best = max(best, int((np.linalg.norm(p - pb, axis=1) < TURN_INLIER_PX).sum()))
    return best


# Lens widths tried when the photos do not say how wide they are. The turn test
# is only fair with the right one: a pure rotation seen through the wrong lens
# model does not line up, and every join then looks as though the camera moved.
# Measured: without EXIF, a ring of photos from a 78-degree lens checked at the
# 63-degree default flagged 10 of 12 joins, most of them clean.
HFOV_SCAN_DEG = tuple(range(45, 116, 5))


def _features(img, sift):
    return sift.detectAndCompute(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), None)


def _match(fa, fb, matcher):
    ka, da = fa
    kb, db = fb
    if da is None or db is None or len(da) < 2 or len(db) < 2:
        return None
    good = [m for m, s in matcher.knnMatch(da, db, k=2) if m.distance < 0.75 * s.distance]
    if len(good) < 20:
        return None
    pa = np.float32([ka[m.queryIdx].pt for m in good])
    pb = np.float32([kb[m.trainIdx].pt for m in good])
    return pa, pb


def _measure(pa, pb, w, h, hfov_deg):
    k = _k(w, h, hfov_deg)
    turned = _turn_inliers(pa, pb, k)
    e, mask = cv2.findEssentialMat(pa, pb, k, cv2.RANSAC, 0.999, 1.0)
    moved = int(mask.sum()) if e is not None and mask is not None else 0
    return turned, moved


def estimate_hfov(pairs, w, h):
    """The lens width under which the most matched points are a pure turn.

    A ring shares one lens, so every pair votes on the same number. Coarse
    first, then to the nearest degree around the winner.
    """
    def score(fov):
        return sum(_turn_inliers(pa, pb, _k(w, h, fov), iters=300) for pa, pb in pairs)
    best = max(HFOV_SCAN_DEG, key=score)
    return max(range(best - 4, best + 5), key=score)


def check_pair(img_a, img_b, hfov_deg, sift=None, matcher=None):
    """(turned, moved, matches): points explained by a turn alone, by a turn
    and a move, and how many were matched in all. None if they barely match."""
    sift = sift or cv2.SIFT_create(nfeatures=8000, contrastThreshold=0.02)
    matcher = matcher or cv2.BFMatcher()
    m = _match(_features(img_a, sift), _features(img_b, sift), matcher)
    if m is None:
        return None
    h, w = img_a.shape[:2]
    turned, moved = _measure(m[0], m[1], w, h, hfov_deg)
    return turned, moved, len(m[0])


def find_moved_joins(images, labels, hfov_deg=None, closed=True):
    """Which neighbouring photos in a ring were taken from different points.

    images   the ring's photos in the order they sit round it
    labels   one identifier per photo, handed back to name the joins
    hfov_deg the lens's width when the photos say it (EXIF); None to measure it
    closed   whether the last photo joins the first (a full ring does)

    Returns [{"a": label, "b": label, "i": pos, "j": pos, "turned": n,
    "moved": n, "ratio": r}] for the joins where the camera moved, i and j
    being positions in `images`. Never raises: a check that fails is a check
    that found nothing, and a preview must never be lost to it.
    """
    moved = []
    n = len(images)
    if n < 2:
        return moved
    try:
        small = []
        for img in images:
            h, w = img.shape[:2]
            s = min(1.0, WORK_WIDTH / float(w))
            small.append(cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                                    interpolation=cv2.INTER_AREA) if s < 1 else img)
        sift = cv2.SIFT_create(nfeatures=8000, contrastThreshold=0.02)
        matcher = cv2.BFMatcher()
        # Each photo is in two joins; find its features once, not twice.
        feats = [_features(img, sift) for img in small]
        h, w = small[0].shape[:2]
        last = n if closed and n > 2 else n - 1
        joins = []
        for i in range(last):
            j = (i + 1) % n
            if small[j].shape[:2] != (h, w):
                continue
            m = _match(feats[i], feats[j], matcher)
            if m is not None:
                joins.append((i, j, m[0], m[1]))
        if not joins:
            return moved
        if not hfov_deg:
            hfov_deg = estimate_hfov([(pa, pb) for _, _, pa, pb in joins], w, h)
            log.info("[orbit-worker] movement check: no lens width in the photos; "
                     "measured %d degrees", hfov_deg)
        for i, j, pa, pb in joins:
            turned, moved_n = _measure(pa, pb, w, h, hfov_deg)
            ratio = moved_n / float(max(1, turned))
            if moved_n >= MIN_MOVED_INLIERS and (
                    ratio >= MOVED_RATIO or
                    (moved_n - turned >= MOVED_EXTRA and ratio >= MOVED_EXTRA_RATIO)):
                moved.append({"a": labels[i], "b": labels[j], "i": i, "j": j,
                              "turned": turned, "moved": moved_n,
                              "ratio": round(ratio, 2)})
    except Exception as e:
        log.warning("[orbit-worker] movement check skipped (%s: %s)", type(e).__name__, e)
    return moved
