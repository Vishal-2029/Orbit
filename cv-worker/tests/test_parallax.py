"""Movement check: does it tell a camera that turned from one that travelled?

Two rings are rendered from the same made-up room: one taken turning on the
spot, one swinging the phone round at arm's length, the way a person turns
their body. The room has a far wall and, closer in, a screen of patches, so a
moving viewpoint shifts the near patches against the far wall exactly as a
real corridor does. Success is flagging none of the first ring's joins and
most of the second's - with the lens width given, and measured.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_parallax.py
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.parallax import find_moved_joins  # noqa: E402
from ops.pose_stitch import quaternion_from_heading, quaternion_to_matrix  # noqa: E402

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
NEAR_M = 1.5               # radius of the near screen round the photographer


def texture(seed, grain=8):
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (SRC_H // grain, SRC_W // grain, 3), dtype=np.uint8)
    img = cv2.resize(img, (SRC_W, SRC_H), interpolation=cv2.INTER_CUBIC)
    return cv2.GaussianBlur(img, (0, 0), 1.0)


FAR = texture(11)
# Finer, because it is nearer: a near thing fills more of the frame with the
# same detail, and a photo of it offers as many points to match as the wall.
NEAR = texture(23, grain=4)
# The near screen is patchy: through its gaps the far wall shows, so near and
# far sit side by side in every photo.
_gaps = np.random.default_rng(5).random((12, 24)) < 0.5
NEAR_MASK = cv2.resize(_gaps.astype(np.uint8), (SRC_W, SRC_H), interpolation=cv2.INTER_NEAREST)


def _lookup(v):
    lat = np.arcsin(np.clip(v[..., 2], -1, 1))
    lon = np.arctan2(v[..., 1], v[..., 0])
    u = ((lon + math.pi) / (2 * math.pi) * SRC_W).astype(np.float32)
    w = ((math.pi / 2 - lat) / math.pi * SRC_H).astype(np.float32)
    return u, w


def shoot(yaw, arm_m=0.0):
    """A photo facing `yaw`, taken `arm_m` in front of the turning point."""
    f = (FW / 2) / math.tan(math.radians(TRUE_FOV) / 2)
    j, i = np.meshgrid(np.arange(FW), np.arange(FH))
    rays = np.stack([(j - FW / 2) / f, -(i - FH / 2) / f, -np.ones_like(j, float)], -1)
    R = np.array(quaternion_to_matrix(*quaternion_from_heading(yaw, 0.0)))
    v = rays @ R.T
    v /= np.linalg.norm(v, axis=-1, keepdims=True)
    # The lens sits along the direction it faces, arm_m out from the pivot.
    fwd = np.array([0.0, 0.0, -1.0]) @ R.T
    c = fwd * arm_m
    # Where the ray meets the near screen, a sphere round the pivot.
    b = v @ c
    t = -b + np.sqrt(b * b - (c @ c - NEAR_M ** 2))
    p = c + v * t[..., None]
    p /= np.linalg.norm(p, axis=-1, keepdims=True)
    fu, fv = _lookup(v)     # the far wall is far enough that only direction counts
    nu, nv = _lookup(p)
    far = cv2.remap(FAR, fu, fv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
    near = cv2.remap(NEAR, nu, nv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
    solid = cv2.remap(NEAR_MASK, nu, nv, cv2.INTER_NEAREST, borderMode=cv2.BORDER_WRAP)
    return np.where(solid[..., None] > 0, near, far)


def ring(arm_m):
    return [shoot(yaw, arm_m) for yaw in range(0, 360, 30)]


def test_turning_on_the_spot_is_never_flagged():
    photos = ring(0.0)
    labels = list(range(len(photos)))
    given = find_moved_joins(photos, labels, TRUE_FOV)
    check("no join flagged with the lens width given", not given, given)
    measured = find_moved_joins(photos, labels, None)
    check("no join flagged with the lens width measured", not measured, measured)


def test_swinging_round_the_body_is_flagged():
    photos = ring(0.5)
    labels = list(range(len(photos)))
    given = find_moved_joins(photos, labels, TRUE_FOV)
    check("most joins flagged with the lens width given", len(given) >= 9,
          "%d of 12" % len(given))
    # Measured, the lens comes out a few degrees narrow (69 for 74 here): a
    # turn through a slightly wrong lens soaks up some of the shift, so the
    # borderline joins drop out. Two thirds still names the stretch to reshoot.
    measured = find_moved_joins(photos, labels, None)
    check("most joins flagged with the lens width measured", len(measured) >= 8,
          "%d of 12" % len(measured))
    check("each flag names its neighbours", all(m["j"] == (m["i"] + 1) % 12 for m in given))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("\n%s:" % name)
            fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
