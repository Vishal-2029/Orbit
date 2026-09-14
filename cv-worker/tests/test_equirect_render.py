"""The equirectangular renderer: a sphere that wraps, and poles that stay put.

Run directly: python tests/test_equirect_render.py
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ops.equirect_render as er  # noqa: E402
import ops.pose_stitch as ps  # noqa: E402
from ops.finish import finish_panorama  # noqa: E402

FAILURES = []
HFOV = 63.0


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


def _scene(w=2048, h=1024, seed=4):
    rng = np.random.default_rng(seed)
    eq = np.full((h, w, 3), 100, np.uint8)
    for _ in range(4000):
        cv2.circle(eq, (int(rng.integers(0, w)), int(rng.integers(0, h))),
                   int(rng.integers(2, 9)), tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
    return eq


def _shoot(eq, R, w=480, h=640, hfov=HFOV, gain=1.0):
    """The photo a camera with camera-to-world R takes of the scene."""
    f = (w / 2.0) / math.tan(math.radians(hfov) / 2.0)
    j, i = np.meshgrid(np.arange(w, dtype=np.float64), np.arange(h, dtype=np.float64))
    p = np.stack([(j - w / 2) / f, (i - h / 2) / f, np.ones_like(j)], -1)
    d = p @ R.T.astype(np.float64)
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    lon = np.arctan2(d[..., 0], d[..., 2])
    lat = np.arcsin(np.clip(d[..., 1], -1, 1))
    H, W = eq.shape[:2]
    out = cv2.remap(eq, ((lon / (2 * math.pi) + .5) * W).astype(np.float32),
                    ((lat / math.pi + .5) * H).astype(np.float32),
                    cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
    return np.clip(out.astype(np.float32) * gain, 0, 255).astype(np.uint8)


def test_footprint_of_a_seam_straddling_photo_is_one_range():
    K, _ = ps.intrinsics(480, 640, HFOV)
    R = ps.camera_rotation(ps.quaternion_from_heading(180.0))
    c0, c1, r0, r1 = er.footprint(R, K, 480, 640, 2000, 1000)
    check("a photo facing the wrap gets one continuous column range",
          0 <= c0 < 2000 and c1 > 2000, "cols %d..%d" % (c0, c1))
    check("and it is about one field of view wide",
          abs((c1 - c0) - 2000 * HFOV / 360.0) < 60, "%d px" % (c1 - c0))


def test_a_pole_shot_reaches_every_column():
    K, _ = ps.intrinsics(480, 640, HFOV)
    R = ps.camera_rotation(ps.quaternion_from_heading(0.0, 89.0))
    c0, c1, r0, r1 = er.footprint(R, K, 480, 640, 2000, 1000)
    check("a shot straight up spans the whole width", (c0, c1) == (0, 2000), "%d..%d" % (c0, c1))
    check("and starts at the top row", r0 == 0, "row %d" % r0)
    check("and stays in the upper half", r1 < 500, "row %d" % r1)


def test_the_sphere_wraps_and_the_seam_is_blended():
    """The last and first photo of a ring must be blended together.

    Each photo gets its own exposure, so a join nothing blended would show as a
    brightness step between the right edge and the left edge.
    """
    eq = _scene()
    rng = np.random.default_rng(1)
    headings = [k * 30.0 for k in range(12)]
    quats = [ps.quaternion_from_heading(hd) for hd in headings]
    imgs = [_shoot(eq, ps.camera_rotation(q), gain=float(rng.uniform(0.8, 1.2)))
            for q in quats]

    ok, pano, reason, geom = ps.stitch_with_poses(imgs, quats, hfov_deg=HFOV)
    check("a posed ring renders", ok, str(reason))
    if not ok:
        return
    h, w = pano.shape[:2]
    check("the canvas is exactly one turn", w == int(geom.circumference_px), "%d vs %.0f" % (w, geom.circumference_px))
    check("the canvas is 2:1", w == 2 * h, "%dx%d" % (w, h))
    check("the geometry says it wraps", geom.wrapped is True)
    check("the horizon is mid-canvas", geom.equator_y == h / 2.0)

    band = slice(int(h * 0.42), int(h * 0.58))
    # Blurred the way the viewer sees it - around the wrap. A plain GaussianBlur
    # reflects at the image edges, so the two edge columns would be smoothed
    # with different content and show a step that is not in the panorama.
    gray = cv2.cvtColor(pano, cv2.COLOR_BGR2GRAY).astype(np.float32)
    pad = 64
    g = cv2.GaussianBlur(np.hstack([gray[:, -pad:], gray, gray[:, :pad]]),
                         (0, 0), 6)[:, pad:pad + w]
    seam_step = float(np.abs(g[band, 0] - g[band, -1]).mean())
    typical = float(np.abs(np.diff(g[band], axis=1)).mean())
    check("no brightness step where the sphere joins itself",
          seam_step < typical * 4 + 1.5, "step %.2f, typical neighbour %.2f" % (seam_step, typical))

    out, info = finish_panorama(pano, circumference_px=geom.circumference_px,
                                equator_y=geom.equator_y, coverage=geom.coverage,
                                spherical=False, wrap=not geom.wrapped)
    check("finishing keeps every column", out.shape[1] == w, "%d -> %d" % (w, out.shape[1]))
    check("and calls it a full turn", info.full_turn, "%.0f deg" % info.span_deg)


def test_pole_shots_land_at_the_poles():
    eq = _scene()
    quats = [ps.quaternion_from_heading(k * 45.0) for k in range(8)]
    quats += [ps.quaternion_from_heading(0.0, 89.0), ps.quaternion_from_heading(0.0, -89.0)]
    imgs = [_shoot(eq, ps.camera_rotation(q)) for q in quats]
    ok, pano, reason, geom = ps.stitch_with_poses(imgs, quats, hfov_deg=HFOV)
    check("a ring with both poles renders", ok, str(reason))
    if not ok:
        return
    h = pano.shape[0]
    cover = geom.coverage > 0
    check("the top row is photographed all the way round", cover[1].mean() > 0.99, "%.2f" % cover[1].mean())
    check("the bottom row is photographed all the way round", cover[h - 2].mean() > 0.99, "%.2f" % cover[h - 2].mean())

    ref = cv2.resize(eq, (pano.shape[1], h), interpolation=cv2.INTER_AREA)
    top = slice(0, h // 10)
    err = float(np.abs(pano[top].astype(np.int16) - ref[top].astype(np.int16)).mean())
    check("the sky matches the scene instead of a starburst", err < 25, "mean error %.1f" % err)


if __name__ == "__main__":
    for fn in [test_footprint_of_a_seam_straddling_photo_is_one_range,
               test_a_pole_shot_reaches_every_column,
               test_the_sphere_wraps_and_the_seam_is_blended,
               test_pole_shots_land_at_the_poles]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
