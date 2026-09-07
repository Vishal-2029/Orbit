"""The pose-less path: matching features, but keeping the geometry.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_feature_stitch.py
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import ops.pose_stitch as ps  # noqa: E402
from ops.feature_stitch import stitch_with_features  # noqa: E402
from ops.finish import finish_panorama  # noqa: E402
from test_pose_stitch import _equirect_with_landmarks, _view_at  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


def _landmark_bearings(pano):
    """Where each coloured landmark ended up, in degrees around the panorama."""
    width = pano.shape[1]
    out = {}
    for name, colour in (("F", (0, 0, 255)), ("R", (0, 255, 0)),
                         ("P", (255, 0, 0)), ("L", (0, 255, 255))):
        hit = np.abs(pano.astype(np.int16) - np.array(colour, np.int16)).sum(2) < 90
        if hit.sum() < 200:
            continue
        a = np.where(hit.any(0))[0] / float(width) * 2 * math.pi
        out[name] = math.degrees(
            math.atan2(np.sin(a).mean(), np.cos(a).mean()) % (2 * math.pi))
    return out


def _known_turn(n=12, w=600, h=800):
    eq = _equirect_with_landmarks()
    return [_view_at(eq, -k * (360.0 / n), w, h, ps.DEFAULT_HFOV_DEG)
            for k in range(n)]


def test_a_full_turn_is_stitched_and_measured():
    """The reason this module exists: cv2.Stitcher will not tell us the scale.

    Without it, finish_panorama has to assume a full turn, and a partial capture
    gets stretched around a whole sphere. Here the focal length comes back with
    the picture, so the span is measured instead.
    """
    ok, pano, reason, geom, kept = stitch_with_features(_known_turn())
    check("a clean full turn stitches", ok, str(reason))
    if not ok:
        return

    check("no photo is dropped", len(kept) == 12, "kept %d of 12" % len(kept))
    check("the geometry comes back with it", geom is not None)
    if not geom:
        return

    span = pano.shape[1] / (geom.circumference_px / 360.0)
    check("it measures as a full turn", abs(span - 360) < 15, "%.0f degrees" % span)
    check("the horizon is inside the picture",
          0 <= geom.equator_y <= pano.shape[0],
          "row %.0f of %d" % (geom.equator_y, pano.shape[0]))


def test_the_scene_is_not_mirrored():
    """Same guarantee the pose path carries: azimuth must run the right way."""
    ok, pano, reason, geom, kept = stitch_with_features(_known_turn())
    if not ok:
        check("a clean full turn stitches", False, str(reason))
        return

    bearings = _landmark_bearings(pano)
    check("all four landmarks survive", len(bearings) == 4, str(sorted(bearings)))
    if len(bearings) != 4:
        return
    order = "".join(n for n, _ in sorted(bearings.items(), key=lambda kv: kv[1]))
    check("landmarks keep their real order", order in "FRPL" * 2,
          "left to right got %s, wanted a rotation of FRPL" % order)


def test_it_finishes_into_a_clean_sphere():
    """End to end, the way the worker calls it."""
    ok, pano, reason, geom, kept = stitch_with_features(_known_turn())
    if not ok:
        check("a clean full turn stitches", False, str(reason))
        return

    out, info = finish_panorama(pano, circumference_px=geom.circumference_px,
                                equator_y=geom.equator_y)
    check("it is recognised as a full turn", info.full_turn, str(info))
    check("the finished texture is 2:1",
          abs(out.shape[1] / float(out.shape[0]) - 2.0) < 0.01,
          "%dx%d" % (out.shape[1], out.shape[0]))
    check("no black is left in it",
          (cv2.cvtColor(out, cv2.COLOR_BGR2GRAY) <= 10).mean() < 0.01)


def test_a_half_turn_is_reported_as_a_half_turn():
    """A partial capture must measure as partial, not be padded into a sphere."""
    ok, pano, reason, geom, kept = stitch_with_features(_known_turn(n=12)[:6])
    check("half a turn still stitches", ok, str(reason))
    if not ok:
        return

    out, info = finish_panorama(pano, circumference_px=geom.circumference_px,
                                equator_y=geom.equator_y)
    check("it is NOT offered as a sphere", not info.full_turn, str(info))
    check("the span is roughly the half turn it is",
          120 < info.span_deg < 240, "%.0f degrees" % info.span_deg)


def test_unrelated_photos_are_refused_with_a_reason():
    """Two rooms are not one panorama, and saying so is the useful answer."""
    rng = np.random.RandomState(4)
    imgs = [cv2.GaussianBlur(rng.randint(0, 255, (400, 300, 3)).astype(np.uint8),
                             (9, 9), 0) for _ in range(5)]
    ok, pano, reason, geom, kept = stitch_with_features(imgs)
    check("unrelated photos do not stitch", not ok)
    check("and the reason is plain English",
          bool(reason) and reason[0].isupper() and reason.endswith("."), str(reason))


if __name__ == "__main__":
    for fn in [test_a_full_turn_is_stitched_and_measured,
               test_the_scene_is_not_mirrored,
               test_it_finishes_into_a_clean_sphere,
               test_a_half_turn_is_reported_as_a_half_turn,
               test_unrelated_photos_are_refused_with_a_reason]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
