"""How much of the sphere a set of photos actually saw.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_coverage.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ops.coverage import sphere_coverage  # noqa: E402

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


def pose(yaw, pitch=0.0):
    hy = math.radians(yaw) / 2
    hx = math.radians(90 + pitch) / 2
    return qmul([0, 0, math.sin(hy), math.cos(hy)],
                [math.sin(hx), 0, 0, math.cos(hx)])


PORTRAIT = 2844 / 1600.0   # a phone photo held upright


def test_nothing_covers_nothing():
    check("no photos means no coverage", sphere_coverage([]) == 0.0)
    check("photos without rotations mean no coverage",
          sphere_coverage([None, None]) == 0.0)


def test_one_photo_covers_a_small_share():
    c = sphere_coverage([pose(0)], 65, PORTRAIT)
    check("a single photo covers a sliver, not the world", 0.02 < c < 0.25,
          "%.2f" % c)


def test_four_cardinals_leave_holes():
    """The capture that started all this.

    Four photos 90 degrees apart, from a camera that sees 65, cannot cover the
    way round - there is a 25 degree wedge between each pair.
    """
    c = sphere_coverage([pose(d) for d in (0, 90, 180, 270)], 65, PORTRAIT)
    check("four shots 90 deg apart cover barely half the sphere", c < 0.6,
          "%.0f%%" % (c * 100))
    print("       four cardinals: %.0f%%" % (c * 100))


def test_gapless_ring_beats_cardinals():
    cardinals = sphere_coverage([pose(d) for d in (0, 90, 180, 270)], 65, PORTRAIT)
    ring = sphere_coverage([pose(i * 40) for i in range(9)], 65, PORTRAIT)
    check("a 40 degree ring covers more than four cardinals", ring > cardinals,
          "%.0f%% vs %.0f%%" % (ring * 100, cardinals * 100))
    print("       9 shots at 40 deg: %.0f%%" % (ring * 100))


def test_ring_plus_poles_is_nearly_complete():
    qs = [pose(i * 40) for i in range(9)] + [pose(0, 89), pose(0, -89)]
    c = sphere_coverage(qs, 65, PORTRAIT)
    check("a full ring plus ceiling and floor covers nearly everything", c > 0.9,
          "%.0f%%" % (c * 100))
    print("       ring + up + down: %.0f%%" % (c * 100))


def test_more_photos_never_reduce_coverage():
    base = [pose(i * 60) for i in range(6)]
    more = base + [pose(30), pose(90)]
    a = sphere_coverage(base, 65, PORTRAIT)
    b = sphere_coverage(more, 65, PORTRAIT)
    check("adding photos cannot lower coverage", b >= a - 1e-9,
          "%.3f -> %.3f" % (a, b))


def test_result_is_a_fraction():
    for qs in ([pose(0)], [pose(i * 20) for i in range(18)]):
        c = sphere_coverage(qs, 65, PORTRAIT)
        check("coverage stays between 0 and 1", 0.0 <= c <= 1.0, "%.3f" % c)


if __name__ == "__main__":
    for fn in [test_nothing_covers_nothing, test_one_photo_covers_a_small_share,
               test_four_cardinals_leave_holes, test_gapless_ring_beats_cardinals,
               test_ring_plus_poles_is_nearly_complete,
               test_more_photos_never_reduce_coverage, test_result_is_a_fraction]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
