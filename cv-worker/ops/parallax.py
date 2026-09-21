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


def check_pair(img_a, img_b, hfov_deg, sift=None, matcher=None):
    """(turned, moved, matches): points explained by a turn alone, by a turn
    and a move, and how many were matched in all. None if they barely match."""
    sift = sift or cv2.SIFT_create(nfeatures=8000, contrastThreshold=0.02)
    matcher = matcher or cv2.BFMatcher()
    ka, da = sift.detectAndCompute(cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY), None)
    kb, db = sift.detectAndCompute(cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY), None)
    if da is None or db is None or len(da) < 2 or len(db) < 2:
        return None
    good = [m for m, s in matcher.knnMatch(da, db, k=2) if m.distance < 0.75 * s.distance]
    if len(good) < 20:
        return None
    pa = np.float32([ka[m.queryIdx].pt for m in good])
    pb = np.float32([kb[m.trainIdx].pt for m in good])
    h, w = img_a.shape[:2]
    k = _k(w, h, hfov_deg)
    turned = _turn_inliers(pa, pb, k)
    e, mask = cv2.findEssentialMat(pa, pb, k, cv2.RANSAC, 0.999, 1.0)
    moved = int(mask.sum()) if e is not None and mask is not None else 0
    return turned, moved, len(good)


def find_moved_joins(images, labels, hfov_deg, closed=True):
    """Which neighbouring photos in a ring were taken from different points.

    images  the ring's photos in the order they sit round it
    labels  one identifier per photo, handed back to name the joins
    closed  whether the last photo joins the first (a full ring does)

    Returns [{"a": label, "b": label, "turned": n, "moved": n, "ratio": r}] for
    the joins where the camera moved. Never raises: a check that fails is a
    check that found nothing, and a preview must never be lost to it.
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
        last = n if closed and n > 2 else n - 1
        for i in range(last):
            j = (i + 1) % n
            res = check_pair(small[i], small[j], hfov_deg, sift, matcher)
            if res is None:
                continue
            turned, moved_n, _ = res
            ratio = moved_n / float(max(1, turned))
            if moved_n >= MIN_MOVED_INLIERS and ratio >= MOVED_RATIO:
                moved.append({"a": labels[i], "b": labels[j], "turned": turned,
                              "moved": moved_n, "ratio": round(ratio, 2)})
    except Exception as e:
        log.warning("[orbit-worker] movement check skipped (%s: %s)", type(e).__name__, e)
    return moved
