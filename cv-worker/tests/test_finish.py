"""Panorama finishing: black-border crop, wrap seam, equirect shaping.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_finish.py
"""
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ops.finish as F  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


def ragged_panorama(w=3600, h=1800):
    """A stitcher-shaped result: content in the middle, black wedges around."""
    img = np.random.RandomState(3).randint(40, 220, (h, w, 3)).astype(np.uint8)
    img = cv2.GaussianBlur(img, (31, 31), 0)
    img[:int(h * 0.18), :] = 0
    img[int(h * 0.86):, :] = 0
    img[:, :int(w * 0.05)] = 0
    for x in range(w):                       # a wavy bottom edge
        img[h - int(60 + 40 * np.sin(x / 200.0)):, x] = 0
    return img


def test_fast_crop_matches_exact_and_is_much_quicker():
    mask = F.content_mask(ragged_panorama())

    t = time.time()
    exact = F._largest_interior_rect(mask)
    t_exact = time.time() - t

    t = time.time()
    fast = F._largest_interior_rect_fast(mask)
    t_fast = time.time() - t

    check("both searches find a rectangle", exact is not None and fast is not None)
    if not (exact and fast):
        return

    x, y, w, h = fast
    check("the fast rectangle contains no black", bool(mask[y:y + h, x:x + w].all()))
    ratio = (w * h) / float(exact[2] * exact[3])
    check("the fast rectangle keeps nearly all the area (%.1f%%)" % (ratio * 100),
          ratio > 0.90)
    check("the fast search is at least 10x quicker (%.2fs vs %.2fs)" % (t_fast, t_exact),
          t_fast * 10 < t_exact)


def test_small_images_use_the_exact_search():
    mask = np.ones((40, 60), dtype=bool)
    mask[0:5, :] = False
    check("small masks fall through to the exact search",
          F._largest_interior_rect_fast(mask) == F._largest_interior_rect(mask))


def test_finish_produces_a_clean_sphere_texture():
    t = time.time()
    out = F.finish_panorama(ragged_panorama())
    elapsed = time.time() - t

    h, w = out.shape[:2]
    check("output is exactly 2:1", abs(w / float(h) - 2.0) < 0.01, "%dx%d" % (w, h))
    check("no pure black remains",
          (cv2.cvtColor(out, cv2.COLOR_BGR2GRAY) <= 10).mean() < 0.001)
    check("finishing is quick (%.2fs)" % elapsed, elapsed < 3.0)


def test_wrap_seam_is_closed():
    base = cv2.GaussianBlur(
        np.random.RandomState(11).randint(0, 255, (200, 1000, 3)).astype(np.uint8),
        (21, 21), 0)
    overshoot = np.hstack([base, base[:, :200]])     # last shot overlaps the first
    out = F.close_wrap_seam(overshoot)
    seam = np.abs(out[:, 0].astype(int) - out[:, -1].astype(int)).mean()
    neighbour = np.abs(out[:, 500].astype(int) - out[:, 501].astype(int)).mean()
    check("the duplicate overlap is trimmed", out.shape[1] < overshoot.shape[1])
    check("the join is as smooth as any other column pair (%.2f vs %.2f)"
          % (seam, neighbour), seam <= neighbour * 2 + 2)


if __name__ == "__main__":
    for fn in [test_fast_crop_matches_exact_and_is_much_quicker,
               test_small_images_use_the_exact_search,
               test_finish_produces_a_clean_sphere_texture,
               test_wrap_seam_is_closed]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
