"""Geometry checks for pose-based stitching.

These exercise the production code path directly rather than a synthetic
renderer, because the thing that must be right is where a given device rotation
places a photo on the sphere.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_pose_stitch.py
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ops.pose_stitch as ps  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


def upright():
    """Phone held vertically, camera pointing at the horizon."""
    h = math.radians(90) / 2
    return [math.sin(h), 0, 0, math.cos(h)]


def yawed(deg):
    h = math.radians(deg) / 2
    return qmul([0, 0, math.sin(h), math.cos(h)], upright())


def test_quaternion_matrix():
    check("identity quaternion -> identity matrix",
          np.allclose(ps.quaternion_to_matrix(0, 0, 0, 1), np.eye(3)))
    q = [0, 0, math.sin(math.radians(45)), math.cos(math.radians(45))]
    R = ps.quaternion_to_matrix(*q)
    check("90 deg about Z maps X to Y",
          np.allclose(R @ np.array([1, 0, 0]), [0, 1, 0], atol=1e-6))
    check("unnormalised quaternion is still handled",
          np.allclose(ps.quaternion_to_matrix(0, 0, 0, 5), np.eye(3)))
    check("zero quaternion does not divide by zero",
          np.allclose(ps.quaternion_to_matrix(0, 0, 0, 0), np.eye(3)))


def test_camera_rotation_is_a_rotation():
    for deg in (0, 37, 90, 180, 271):
        R = ps.camera_rotation(yawed(deg))
        check("camera rotation at %d deg is orthonormal" % deg,
              np.allclose(R @ R.T, np.eye(3), atol=1e-5))
        check("camera rotation at %d deg has det 1" % deg,
              abs(np.linalg.det(R) - 1) < 1e-5)


def test_intrinsics():
    K, f = ps.intrinsics(640, 480, 65)
    half = math.degrees(math.atan((640 / 2) / f))
    check("frame edge equals half the fov", abs(half - 32.5) < 0.01, "%.3f" % half)
    check("principal point is the image centre", K[0][2] == 320 and K[1][2] == 240)


def test_yaw_maps_to_even_horizontal_spacing():
    """The core guarantee: rotating the phone must slide the photo along the
    sphere by a proportional, predictable amount."""
    K, f = ps.intrinsics(640, 480, 65)
    warper = cv2.PyRotationWarper("spherical", f)
    px_per_deg = (2 * math.pi * f) / 360.0

    xs = []
    for deg in range(0, 181, 30):
        pt = warper.warpPoint((320.0, 240.0), K, ps.camera_rotation(yawed(deg)))
        xs.append(pt[0])

    check("yaw 0 lands at the origin", abs(xs[0]) < 1.0, "%.2f" % xs[0])
    for i in range(1, len(xs)):
        step = xs[i] - xs[i - 1]
        check("30 deg of yaw moves %.1f px (got %.1f)" % (30 * px_per_deg, step),
              abs(step - 30 * px_per_deg) < 1.0)


def test_pitch_moves_vertically_not_horizontally():
    K, f = ps.intrinsics(640, 480, 65)
    warper = cv2.PyRotationWarper("spherical", f)
    flat = warper.warpPoint((320.0, 240.0), K, ps.camera_rotation(upright()))
    h = math.radians(20) / 2
    tilt = qmul(upright(), [math.sin(h), 0, 0, math.cos(h)])
    up = warper.warpPoint((320.0, 240.0), K, ps.camera_rotation(tilt))
    check("tilting up does not shift horizontally", abs(up[0] - flat[0]) < 2.0,
          "dx=%.2f" % (up[0] - flat[0]))
    check("tilting up does shift vertically", abs(up[1] - flat[1]) > 100,
          "dy=%.2f" % (up[1] - flat[1]))


def test_single_view_is_not_mirrored():
    img = np.full((480, 640, 3), 40, np.uint8)
    img[10:80, 10:120] = 255                       # white block, top-left
    K, f = ps.intrinsics(640, 480, 65)
    warper = cv2.PyRotationWarper("spherical", f)
    _, w = warper.warp(img, K, ps.camera_rotation(upright()),
                       cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)
    g = cv2.cvtColor(w, cv2.COLOR_BGR2GRAY)
    ys, xs = np.where(g > 240)
    check("warped view is not flipped horizontally", xs.mean() < w.shape[1] / 2)
    check("warped view is not flipped vertically", ys.mean() < w.shape[0] / 2)
    check("a 65 deg view stays a narrow slice, not the whole sphere",
          w.shape[1] < 1.2 * (2 * math.pi * f) / 4, "width=%d" % w.shape[1])


def test_refuses_without_enough_poses():
    imgs = [np.zeros((64, 64, 3), np.uint8)] * 3
    ok, out, reason = ps.stitch_with_poses(imgs, [None, None, None])
    check("refuses when no photo has a pose", ok is False and out is None)
    check("gives a plain-english reason", bool(reason) and "rotation" in reason.lower(), str(reason))


def test_end_to_end_placement():
    """Eight distinct views placed by pose should tile a wide strip."""
    imgs, quats = [], []
    for i in range(8):
        img = np.full((240, 320, 3), 30, np.uint8)
        cv2.circle(img, (160, 120), 60,
                   (int(30 * i) % 255, 200, (255 - 30 * i) % 255), -1)
        imgs.append(img)
        quats.append(yawed(i * 45))
    ok, pano, reason = ps.stitch_with_poses(imgs, quats)
    check("eight posed views stitch", ok is True, str(reason))
    if ok:
        h, w = pano.shape[:2]
        check("result spans roughly the full circle", w > 4 * h, "%dx%d" % (w, h))


def test_large_portrait_photos_do_not_explode():
    """The regression that killed the worker.

    Real phone photos arrive tall (1600x2844). At full size that gives a
    7890px sphere circumference and a 97 degree vertical span, and multi-band
    blending six of them allocated 19GB before the kernel stepped in.
    """
    import resource

    w, h = 1600, 2844
    _, full_focal = ps.intrinsics(w, h, ps.DEFAULT_HFOV_DEG)
    full_circ = 2 * math.pi * full_focal
    check("full-size sources would exceed the budget", full_circ > ps.MAX_CIRCUMFERENCE_PX,
          "%.0f px" % full_circ)

    imgs, quats = [], []
    for i in range(6):
        img = np.full((h, w, 3), 40, np.uint8)
        cv2.circle(img, (w // 2, h // 2), 200, (0, 200, 255), -1)
        imgs.append(img)
        quats.append(yawed(i * 60))

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    ok, pano, reason = ps.stitch_with_poses(imgs, quats)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    check("tall phone photos stitch instead of dying", ok is True, str(reason))
    if ok:
        check("canvas stays within the circumference budget",
              pano.shape[1] <= ps.MAX_CIRCUMFERENCE_PX + 8, "width=%d" % pano.shape[1])
    check("peak memory stays reasonable (<2GB growth)", after - before < 2048,
          "grew %.0f MB" % (after - before))
    print("       peak RSS %.0f MB (was 8000+ MB before the cap)" % after)


if __name__ == "__main__":
    for fn in [test_quaternion_matrix, test_camera_rotation_is_a_rotation,
               test_intrinsics, test_yaw_maps_to_even_horizontal_spacing,
               test_pitch_moves_vertically_not_horizontally,
               test_single_view_is_not_mirrored,
               test_refuses_without_enough_poses, test_end_to_end_placement,
               test_large_portrait_photos_do_not_explode]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
