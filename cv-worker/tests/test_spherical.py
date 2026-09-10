"""Multi-row spherical capture: real polar pixels in, real polar pixels out.

The failure this guards against is subtle from the outside. The old pipeline
produced a 2:1 image with a full turn and no error, and it looked finished -
but the ceiling and floor in it were not photographs. They were the last
surviving row of the horizon ring, blurred and faded outwards, because the
finish stage kept only rows that were full nearly all the way across and a
multi-ring capture has none above the horizon.

So these tests do not check that the output is 2:1, or that stitching
succeeded. They check that detail which only exists near the poles SURVIVES,
and that a capture which genuinely lacks a ring is refused rather than padded.
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.finish import finish_panorama, measure_coverage
from ops.pose_stitch import (quaternion_from_heading, quaternion_to_matrix,
                             stitch_with_poses)

pass_n = fail_n = 0


def check(name, cond, extra=""):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print("  ok   %s" % name)
    else:
        fail_n += 1
        print("  FAIL %s %s" % (name, extra))


SRC_H, SRC_W = 800, 1600


def world():
    """A synthetic room with detail everywhere, poles included."""
    yy, xx = np.mgrid[0:SRC_H, 0:SRC_W]
    img = np.zeros((SRC_H, SRC_W, 3), np.uint8)
    img[..., 0] = np.sin(xx / 40.0) * 127 + 128
    img[..., 1] = np.sin(yy / 25.0) * 127 + 128
    img[..., 2] = ((xx // 80 + yy // 80) % 2) * 255
    return img


def shoot(src, yaw, pitch, fw=480, fh=640, hfov=65.0):
    """Re-project the world into one camera frame at a known rotation."""
    f = (fw / 2) / math.tan(math.radians(hfov) / 2)
    j, i = np.meshgrid(np.arange(fw), np.arange(fh))
    rays = np.stack([(j - fw / 2) / f, -(i - fh / 2) / f, -np.ones_like(j, float)], -1)
    quat = quaternion_from_heading(yaw, pitch)
    v = rays @ np.array(quaternion_to_matrix(*quat)).T
    v /= np.linalg.norm(v, axis=-1, keepdims=True)
    lat = np.arcsin(np.clip(v[..., 2], -1, 1))
    lon = np.arctan2(v[..., 1], v[..., 0])
    u = ((lon + math.pi) / (2 * math.pi) * SRC_W).astype(np.float32)
    vv = ((math.pi / 2 - lat) / math.pi * SRC_H).astype(np.float32)
    return cv2.remap(src, u, vv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP), quat


def capture(plan):
    src = world()
    imgs, quats = [], []
    for yaw, pitch in plan:
        im, q = shoot(src, yaw, pitch)
        imgs.append(im)
        quats.append(q)
    return imgs, quats


def build(plan, spherical=True):
    imgs, quats = capture(plan)
    ok, pano, reason, geom = stitch_with_poses(imgs, quats)
    assert ok, reason
    out, info = finish_panorama(pano, circumference_px=geom.circumference_px,
                                equator_y=geom.equator_y, coverage=geom.coverage,
                                spherical=spherical)
    return out, info


def band_detail(img, lat_lo, lat_hi):
    """Mean Laplacian variance of the rows covering a latitude band.

    Fabricated poles are a blurred, faded copy of one row, so they have almost
    no high-frequency content. Real photography of a textured scene has plenty.
    """
    h = img.shape[0]
    y0 = int((90 - lat_hi) / 180.0 * h)
    y1 = int((90 - lat_lo) / 180.0 * h)
    strip = img[max(0, y0):min(h, y1)]
    if strip.size == 0:
        return 0.0
    gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


THREE_RINGS = ([(y, 0) for y in range(0, 360, 40)]
               + [(y, 45) for y in range(0, 360, 60)]
               + [(y, -45) for y in range(0, 360, 60)]
               + [(0, 88), (0, -88)])


print("\ntest_a_multi_row_capture_keeps_its_ceiling_and_floor:")
out, info = build(THREE_RINGS)
check("the output is exactly 2:1",
      out.shape[1] == 2 * out.shape[0], "%dx%d" % (out.shape[1], out.shape[0]))
check("it is recognised as a full turn", info.full_turn)
check("the capture reaches both poles",
      info.pitch_max_deg >= 80 and info.pitch_min_deg <= -80,
      "%.0f..%.0f" % (info.pitch_min_deg, info.pitch_max_deg))
check("it reports near-complete sphere coverage",
      info.sphere_coverage >= 0.95, "%.2f" % info.sphere_coverage)
check("no holes are reported", len(info.holes) == 0, str(info.holes[:3]))

horizon = band_detail(out, -20, 20)
check("the zenith holds real detail, not a blurred fill",
      band_detail(out, 60, 88) > horizon * 0.25,
      "zenith %.0f vs horizon %.0f" % (band_detail(out, 60, 88), horizon))
check("the nadir holds real detail, not a blurred fill",
      band_detail(out, -88, -60) > horizon * 0.25,
      "nadir %.0f vs horizon %.0f" % (band_detail(out, -88, -60), horizon))


print("\ntest_a_capture_missing_its_upper_ring_is_not_a_sphere:")
no_hat = ([(y, 0) for y in range(0, 360, 40)]
          + [(y, -45) for y in range(0, 360, 60)] + [(0, -88)])
out, info = build(no_hat)
check("the missing ceiling is noticed",
      info.pitch_max_deg < 70, "%.0f" % info.pitch_max_deg)
check("coverage is reported short of a sphere",
      info.sphere_coverage < 0.92, "%.2f" % info.sphere_coverage)
check("the gap is located, not just counted", len(info.holes) > 0)
check("and nothing was invented to fill it",
      band_detail(out, 70, 89) < 5.0, "%.1f" % band_detail(out, 70, 89))


print("\ntest_a_horizontal_ring_still_works_and_is_still_filled:")
ring = [(y, 0) for y in range(0, 360, 40)]
out, info = build(ring, spherical=False)
check("a single ring still produces a 2:1 sphere texture",
      out.shape[1] == 2 * out.shape[0], "%dx%d" % (out.shape[1], out.shape[0]))
check("it is still a full turn", info.full_turn)
check("the unphotographed sky is still filled softly, as before",
      band_detail(out, 70, 89) > 0.0 and out[2].std() < 60,
      "std %.0f" % out[2].std())
check("and the horizon is untouched", band_detail(out, -20, 20) > 20)


print("\ntest_coverage_is_measured_by_solid_angle:")
# A band of rows near the pole is a much smaller piece of the world than the
# same band at the horizon. Weighting rows equally would flatter a capture that
# only shot the ceiling, so this checks the weighting is really there.
cover = np.zeros((180, 360), np.uint8)
cover[0:30] = 255                                    # +90 .. +60
pole_min, pole_max, pole_frac, _ = measure_coverage(cover, 90.0, 1.0)
cover = np.zeros((180, 360), np.uint8)
cover[75:105] = 255                                  # +15 .. -15
eq_min, eq_max, eq_frac, _ = measure_coverage(cover, 90.0, 1.0)
check("the same number of rows counts for less near the pole",
      pole_frac < eq_frac * 0.5, "%.3f vs %.3f" % (pole_frac, eq_frac))
check("an equatorial band of 30 degrees is about a quarter of the sphere",
      0.20 < eq_frac < 0.30, "%.3f" % eq_frac)

print("\n%d failure(s)" % fail_n)
sys.exit(1 if fail_n else 0)
