"""Rotation refinement: does it recover poses the sensor got wrong?

The test builds a world, photographs it from EXACTLY known rotations, then
hands the refiner those rotations with a few degrees of drift added - the way a
phone compass actually fails, accumulating over the capture rather than jumping
about. Success is measured against the true rotations, not against whether the
result looks plausible.
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.pose_stitch import (camera_rotation, intrinsics, quaternion_from_heading)
from ops.rotation_refine import _angle_between, _project_to_so3, refine, refine_poses

pass_n = fail_n = 0


def check(name, cond, extra=""):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print("  ok   %s" % name)
    else:
        fail_n += 1
        print("  FAIL %s %s" % (name, extra))


SRC_H, SRC_W = 900, 1800
FW, FH, HFOV = 400, 520, 65.0


def world(seed=7):
    """Rich, NON-repetitive texture - repetition is tested separately."""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (SRC_H // 6, SRC_W // 6, 3), dtype=np.uint8)
    img = cv2.resize(img, (SRC_W, SRC_H), interpolation=cv2.INTER_CUBIC)
    return cv2.GaussianBlur(img, (0, 0), 1.2)


def shoot(src, yaw, pitch, dx=0.0, dy=0.0, hfov=HFOV):
    """One photo. dx, dy put its optical centre off the middle of the frame,
    the way video stabilisation's wandering crop does."""
    f = (FW / 2) / math.tan(math.radians(hfov) / 2)
    j, i = np.meshgrid(np.arange(FW), np.arange(FH))
    rays = np.stack([(j - FW / 2 - dx) / f, -(i - FH / 2 - dy) / f,
                     -np.ones_like(j, float)], -1)
    quat = quaternion_from_heading(yaw, pitch)
    from ops.pose_stitch import quaternion_to_matrix
    v = rays @ np.array(quaternion_to_matrix(*quat)).T
    v /= np.linalg.norm(v, axis=-1, keepdims=True)
    lat = np.arcsin(np.clip(v[..., 2], -1, 1))
    lon = np.arctan2(v[..., 1], v[..., 0])
    u = ((lon + math.pi) / (2 * math.pi) * SRC_W).astype(np.float32)
    vv = ((math.pi / 2 - lat) / math.pi * SRC_H).astype(np.float32)
    return cv2.remap(src, u, vv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP), quat


def drift(R, yaw_err_deg, pitch_err_deg):
    """Rotate a camera by a small error, the way a drifting compass would."""
    cy, sy = math.cos(math.radians(yaw_err_deg)), math.sin(math.radians(yaw_err_deg))
    cp, sp = math.cos(math.radians(pitch_err_deg)), math.sin(math.radians(pitch_err_deg))
    # In the warper's frame +Y is down, so yaw is about Y and pitch about X.
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    return (Ry @ Rx @ R).astype(np.float32)


def scene(plan):
    src = world()
    imgs, truth = [], []
    for yaw, pitch in plan:
        im, q = shoot(src, yaw, pitch)
        imgs.append(im)
        truth.append(camera_rotation(q))
    return imgs, truth


PLAN = ([(y, 0) for y in range(0, 360, 40)]
        + [(y, 40) for y in range(0, 360, 50)]
        + [(y, -40) for y in range(0, 360, 50)])

imgs, truth = scene(PLAN)
K, _ = intrinsics(FW, FH, HFOV)

# Compass drift: a slow accumulating yaw error plus per-shot tilt noise.
rng = np.random.default_rng(3)
sensor = [drift(R, 0.35 * i, float(rng.normal(0, 1.2))) for i, R in enumerate(truth)]

def residuals(truth, est):
    """Per-camera error AFTER removing one global rotation.

    This is the number that decides whether the panorama has doubled edges. A
    rotation applied to every camera equally just turns the finished sphere on
    its axis - the seams do not care, and the viewer opens facing somewhere
    slightly different. What shows up as a ghosted window frame is cameras
    being wrong RELATIVE to each other, so the shared part is divided out
    before measuring.

    It also has to be divided out to be fair to the refiner: the drift in this
    test accumulates in one direction, so its average is a global offset that
    the sensor anchor deliberately preserves.
    """
    acc = np.zeros((3, 3))
    for t_i, e_i in zip(truth, est):
        acc += np.asarray(t_i, float) @ np.asarray(e_i, float).T
    align = _project_to_so3(acc)
    return [_angle_between(t_i, _project_to_so3(align @ np.asarray(e_i, float)))
            for t_i, e_i in zip(truth, est)]


before = residuals(truth, sensor)
refined, stats = refine(imgs, sensor, K, HFOV)
after = residuals(truth, refined)

print("\ntest_it_recovers_poses_a_drifting_compass_got_wrong:")
print("       relative to each other, the shared global rotation removed:")
print("       sensor error  mean %.2f deg, worst %.2f" % (np.mean(before), max(before)))
print("       refined error mean %.2f deg, worst %.2f" % (np.mean(after), max(after)))
check("pairs were actually matched", stats["pairs_used"] >= len(PLAN),
      str(stats))
check("the average camera is closer to the truth than the sensor was",
      np.mean(after) < np.mean(before) * 0.5,
      "%.2f -> %.2f" % (np.mean(before), np.mean(after)))
check("the worst camera improves too",
      max(after) < max(before) * 0.6, "%.2f -> %.2f" % (max(before), max(after)))
check("residual error is under a degree, i.e. under a pixel at 4096 around",
      np.mean(after) < 1.0, "%.2f" % np.mean(after))

print("\ntest_the_world_is_not_left_tilted:")
# Rotation averaging is free to rotate the whole set as one, and the photographs
# cannot object. Only the sensor knows which way is up, so the global frame must
# come back to it.
up_true = np.mean([R[:, 1] for R in truth], axis=0)
up_ref = np.mean([R[:, 1] for R in refined], axis=0)
tilt = math.degrees(math.acos(float(np.clip(
    np.dot(up_true, up_ref) / (np.linalg.norm(up_true) * np.linalg.norm(up_ref)), -1, 1))))
check("the horizon stays level", tilt < 2.0, "%.2f deg" % tilt)

print("\ntest_a_wrong_match_cannot_wreck_the_panorama:")
# One photo replaced by an unrelated scene. Its matches are meaningless, and the
# failure mode being guarded against is that one bad camera drags its neighbours
# with it through the averaging.
imgs2 = list(imgs)
imgs2[5] = world(seed=99)[:FH, :FW]
refined2, stats2 = refine(imgs2, sensor, K, HFOV)
moved = [_angle_between(a, b) for a, b in zip(sensor, refined2)]
check("no camera is moved further than the clamp allows",
      max(moved) <= 10.0 + 1e-6, "%.2f" % max(moved))
others = [i for i in range(len(truth)) if i != 5]
after2 = residuals([truth[i] for i in others], [refined2[i] for i in others])
base2 = residuals([truth[i] for i in others], [sensor[i] for i in others])
check("the other cameras are still improved, not dragged along",
      np.mean(after2) < np.mean(base2) * 0.5,
      "%.2f -> %.2f" % (np.mean(base2), np.mean(after2)))

print("\ntest_it_gives_up_rather_than_guess:")
# Featureless frames: nothing can be matched, so the sensor must be handed back
# untouched rather than a set of rotations invented from noise.
blank = [np.full((FH, FW, 3), 128, np.uint8) for _ in PLAN]
kept, stats3 = refine(blank, sensor, K, HFOV)
# The tolerance is 0.05 rather than 0, because the metric itself cannot do
# better: _angle_between takes an acos near 1, where float32 orthonormality
# error of 1e-7 comes out as three hundredths of a degree. Anything under that
# is the measurement's noise floor, not a change to the rotations.
deltas = [_angle_between(a, b) for a, b in zip(sensor, kept)]
check("blank photos leave the sensor rotations alone",
      max(deltas) < 0.05, "worst %.4f deg" % max(deltas))
check("and it says so rather than claiming success", stats3["pairs_used"] < 3, str(stats3))

print("\ntest_it_finds_where_stabilisation_moved_each_photo:")
# Every photo is a stabilised video frame whose centre has been cropped off to
# one side by a different amount. A rotation cannot explain a shift, so the
# solve has to find it - and the rotations must still come out right.
src = world()
rs = np.random.default_rng(11)
true_shift = [tuple(rs.uniform(-40, 40, 2)) for _ in PLAN]
imgs_s = [shoot(src, y, p, dx, dy)[0] for (y, p), (dx, dy) in zip(PLAN, true_shift)]
rots_s, shifts_s, _, stats_s = refine_poses(imgs_s, sensor, HFOV, measure_fov=False)
est, tru = np.array(shifts_s), np.array(true_shift)
# A shift shared by every photo trades against a small global rotation, and
# neither changes a single seam, so it is divided out before comparing.
serr = np.linalg.norm((est - est.mean(0)) - (tru - tru.mean(0)), axis=1)
rerr = residuals(truth, rots_s)
print("       shift error mean %.2f px, worst %.2f; rotation error mean %.2f deg"
      % (serr.mean(), serr.max(), np.mean(rerr)))
check("every photo's shift is found to within 2 px", serr.max() < 2.0,
      "worst %.2f px" % serr.max())
check("and the rotations are still right", np.mean(rerr) < 0.3, "%.2f deg" % np.mean(rerr))
check("no photo had to be clamped back to the sensor", stats_s["clamped"] == 0, str(stats_s))

print("\ntest_it_measures_the_field_of_view:")
# Photographed at 58 degrees, solved starting from the 63 the code assumes.
imgs_f = [shoot(src, y, p, hfov=58.0)[0] for y, p in PLAN]
_, _, fov, stats_f = refine_poses(imgs_f, truth, 63.0)
check("the field of view is measured, not assumed", abs(fov - 58.0) <= 1.0,
      "%.1f deg from %d pairs" % (fov, stats_f["fov_pairs"]))

print("\n%d failure(s)" % fail_n)
sys.exit(1 if fail_n else 0)
