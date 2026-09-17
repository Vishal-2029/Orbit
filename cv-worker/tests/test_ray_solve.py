"""Ray solve: does it recover the lens and the poses when refinement cannot?

Built the way the failing real capture was: a ring of photos tilted 45 degrees
down, a compass that drifts partway round, and a field of view assumed at 63
when the lens really sees 74. Success is measured against the true rotations
and the true lens, not against whether the result looks plausible.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_ray_solve.py
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.pose_stitch import (camera_rotation, quaternion_from_heading,  # noqa: E402
                             quaternion_to_matrix)
from ops.ray_solve import RaySolver, _angle  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


SRC_H, SRC_W = 1200, 2400
FW, FH = 480, 640          # portrait, like a phone held upright
TRUE_FOV = 74.0


def world(seed=11):
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (SRC_H // 8, SRC_W // 8, 3), dtype=np.uint8)
    img = cv2.resize(img, (SRC_W, SRC_H), interpolation=cv2.INTER_CUBIC)
    return cv2.GaussianBlur(img, (0, 0), 1.0)


def shoot(src, yaw, pitch, hfov=TRUE_FOV):
    f = (FW / 2) / math.tan(math.radians(hfov) / 2)
    j, i = np.meshgrid(np.arange(FW), np.arange(FH))
    rays = np.stack([(j - FW / 2) / f, -(i - FH / 2) / f, -np.ones_like(j, float)], -1)
    quat = quaternion_from_heading(yaw, pitch)
    v = rays @ np.array(quaternion_to_matrix(*quat)).T
    v /= np.linalg.norm(v, axis=-1, keepdims=True)
    lat = np.arcsin(np.clip(v[..., 2], -1, 1))
    lon = np.arctan2(v[..., 1], v[..., 0])
    u = ((lon + math.pi) / (2 * math.pi) * SRC_W).astype(np.float32)
    vv = ((math.pi / 2 - lat) / math.pi * SRC_H).astype(np.float32)
    return cv2.remap(src, u, vv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP), quat


def drifted(R, yaw_err_deg, roll_err_deg):
    """A compass error about the vertical, plus roll about the lens axis."""
    cy, sy = math.cos(math.radians(yaw_err_deg)), math.sin(math.radians(yaw_err_deg))
    cr, sr = math.cos(math.radians(roll_err_deg)), math.sin(math.radians(roll_err_deg))
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    return (Ry @ np.asarray(R, float) @ Rz).astype(np.float32)


def test_a_tilted_ring_with_a_drifting_compass_and_the_wrong_lens():
    src = world()
    yaws = [k * 40 for k in range(9)]
    imgs, truth = [], []
    for y in yaws:
        im, q = shoot(src, y, -45)
        imgs.append(im)
        truth.append(camera_rotation(q))
    # Drift that builds up partway round and comes back, as a gyro's does.
    sensor = [drifted(R, 6 * math.sin(math.pi * k / 8), 1 + 0.5 * k)
              for k, R in enumerate(truth)]

    solver = RaySolver(imgs, sensor)
    result = solver.solve(63.0)
    check("the photos give a solve", result is not None)
    if result is None:
        return
    solved, fov, stats = result

    check("the lens is measured, not assumed", abs(fov - TRUE_FOV) <= 3,
          "%.0f deg, truly %.0f" % (fov, TRUE_FOV))

    def relative_error(est):
        # Cameras wrong RELATIVE to each other are what break seams; a shared
        # rotation of the whole set only turns the finished sphere.
        errs = []
        for a in range(len(est)):
            b = (a + 1) % len(est)
            e = np.asarray(est[b], float).T @ np.asarray(est[a], float)
            t = np.asarray(truth[b], float).T @ np.asarray(truth[a], float)
            errs.append(_angle(e, t))
        return float(np.mean(errs))

    before, after = relative_error(sensor), relative_error(solved)
    print("       neighbour error: sensor %.2f deg, solved %.2f deg" % (before, after))
    check("neighbouring cameras agree far better than the sensor did",
          after < before * 0.4, "%.2f -> %.2f" % (before, after))

    keys = stats["pair_keys"]
    e_sensor = solver.error(sensor, 63.0, None, keys)
    e_solved = solver.error(solved, fov, None, keys)
    check("and matched points land together, which is what the caller checks",
          e_solved < 0.5 * e_sensor, "%.1f px -> %.1f px" % (e_sensor, e_solved))


def test_photos_with_nothing_in_common_are_left_alone():
    rng = np.random.default_rng(3)
    blank = [np.full((FH, FW, 3), 128, np.uint8) + rng.integers(0, 3, (FH, FW, 3), dtype=np.uint8)
             for _ in range(6)]
    sensor = [camera_rotation(quaternion_from_heading(k * 60, -45)) for k in range(6)]
    check("featureless photos give no solve rather than a guess",
          RaySolver(blank, sensor).solve(63.0) is None)


if __name__ == "__main__":
    for fn in [test_a_tilted_ring_with_a_drifting_compass_and_the_wrong_lens,
               test_photos_with_nothing_in_common_are_left_alone]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
