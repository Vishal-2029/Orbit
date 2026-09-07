"""Sizing the source photos to the memory that actually exists.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_budget.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ops.budget as B  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


def resident_mb(count, width):
    return count * width * width * B.ASSUMED_ASPECT * 3 / (1024 * 1024)


def test_an_unconstrained_host_is_left_alone():
    """The common case must not change. Most hosts have room, and quietly
    softening every panorama to guard against a limit that is not there would
    be a worse bug than the one this fixes."""
    for count in (2, 8, 16, 31, 60):
        check("%d photos on a memory-unlimited host stay at full width" % count,
              B.source_width(count, 1600, None) == 1600)


def test_a_small_capture_on_a_small_host_is_left_alone():
    """Eight photos at 1600px is 82MB. That already fits; shrinking it would
    cost detail for nothing."""
    check("8 photos still load at full width on 512MB",
          B.source_width(8, 1600, 512) == 1600,
          str(B.source_width(8, 1600, 512)))


def test_the_capture_that_actually_failed():
    """31 photos at 1600px on a 512MB instance - the real one, which was
    OOM-killed, retried, killed again, and reported to the user as 'the photos
    were too large to process'."""
    width = B.source_width(31, 1600, 512)
    check("it is shrunk", width < 1600, str(width))
    check("but stays big enough to stitch", width >= B.MIN_WIDTH, str(width))

    fits = B.source_budget_bytes(512) / (1024 * 1024)
    check("and the result fits the budget (%.0f MB of %.0f MB)"
          % (resident_mb(31, width), fits),
          resident_mb(31, width) <= fits + 1)
    check("where full width would not have (%.0f MB)" % resident_mb(31, 1600),
          resident_mb(31, 1600) > fits)


def test_more_photos_means_smaller_photos():
    """The whole point: the limit is count TIMES size, so the two trade off."""
    widths = [B.source_width(n, 1600, 512) for n in (8, 16, 24, 31, 48)]
    check("width never increases as the photo count does",
          all(a >= b for a, b in zip(widths, widths[1:])), str(widths))
    check("and the total stays inside the budget at every count",
          all(resident_mb(n, w) <= B.source_budget_bytes(512) / (1024 * 1024) + 1
              for n, w in zip((8, 16, 24, 31, 48), widths) if w < 1600),
          str(widths))


def test_a_bigger_host_keeps_more_detail():
    small = B.source_width(31, 1600, 512)
    medium = B.source_width(31, 1600, 1024)
    large = B.source_width(31, 1600, 4096)
    check("more memory means a sharper panorama (%d < %d <= %d)"
          % (small, medium, large),
          small < medium <= large, "%s %s %s" % (small, medium, large))


def test_a_retry_is_actually_different():
    """A retry at the same size fails the same way, three times, slowly. Each
    attempt after a memory failure gets half the room."""
    ladder = [B.source_width(31, 1600, 512, attempt=a) for a in (1, 2, 3, 4)]
    check("each attempt is no larger than the last",
          all(a >= b for a, b in zip(ladder, ladder[1:])), str(ladder))
    check("and the first two genuinely differ", ladder[0] > ladder[1], str(ladder))
    check("but it never falls below the useful minimum",
          all(w >= B.MIN_WIDTH for w in ladder), str(ladder))


def test_it_never_upscales():
    """A capture stored at 900px must not be loaded at 1600."""
    check("a smaller stored width is respected",
          B.source_width(4, 900, None) == 900)
    check("even with plenty of memory",
          B.source_width(4, 900, 8192) == 900)


def test_degenerate_input_does_not_explode():
    check("zero photos returns the asked-for width",
          B.source_width(0, 1600, 512) == 1600)
    check("a tiny limit still returns something usable",
          B.source_width(31, 1600, 64) == B.MIN_WIDTH,
          str(B.source_width(31, 1600, 64)))


def test_plan_explains_itself_only_when_it_changes_something():
    width, note = B.plan(8, 1600, None)
    check("no note when nothing was changed", note is None)
    width, note = B.plan(31, 1600, 512)
    check("a note when the width was reduced", bool(note))
    check("and it names the numbers a reader would want",
          note and "31 photos" in note and "512" in note and str(width) in note,
          str(note))


if __name__ == "__main__":
    for fn in [test_an_unconstrained_host_is_left_alone,
               test_a_small_capture_on_a_small_host_is_left_alone,
               test_the_capture_that_actually_failed,
               test_more_photos_means_smaller_photos,
               test_a_bigger_host_keeps_more_detail,
               test_a_retry_is_actually_different,
               test_it_never_upscales,
               test_degenerate_input_does_not_explode,
               test_plan_explains_itself_only_when_it_changes_something]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
