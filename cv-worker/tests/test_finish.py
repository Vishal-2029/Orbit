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
    out, info = F.finish_panorama(ragged_panorama())
    elapsed = time.time() - t

    h, w = out.shape[:2]
    check("output is exactly 2:1", abs(w / float(h) - 2.0) < 0.01, "%dx%d" % (w, h))
    check("with no scale given it is assumed to be a full turn",
          info.full_turn and abs(info.span_deg - 360) < 0.01, str(info))
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



# --------------------------------------------------------------------------
# Knowing the scale: the two bugs that came from not knowing it
# --------------------------------------------------------------------------

def ring_panorama(circumference=3600, height=800, equator_y=400, marker_gap=200):
    """A pose-stitch shaped result: one full turn, horizon part way down.

    Two bright rows are drawn a known distance apart so a test can measure
    whether the vertical scale survived.
    """
    img = cv2.GaussianBlur(
        np.random.RandomState(5).randint(40, 220, (height, circumference, 3))
        .astype(np.uint8), (21, 21), 0)
    img[equator_y - 1:equator_y + 2] = 255
    img[equator_y + marker_gap - 1:equator_y + marker_gap + 2] = 255
    return img


def _bright_rows(img):
    rows = np.where((cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) > 250).mean(axis=1) > 0.9)[0]
    if len(rows) == 0:
        return []
    groups, run = [], [rows[0]]
    for r in rows[1:]:
        if r - run[-1] <= 2:
            run.append(r)
        else:
            groups.append(int(np.mean(run)))
            run = [r]
    groups.append(int(np.mean(run)))
    return groups


def test_a_known_full_turn_keeps_every_column():
    """The trim that ate 46 degrees of real view.

    _trim_wrap_overlap is a bare template match. On a repetitive scene it finds
    a "duplicate" that is not there, and the old code let it cut on that alone -
    on a 359.7-degree sphere, where the true overshoot was nothing.
    """
    pano = ring_panorama()
    out, info = F.finish_panorama(pano, circumference_px=3600, equator_y=400)

    check("a full turn is not trimmed at all",
          out.shape[1] == 3600, "%d columns, was 3600" % out.shape[1])
    check("it is recognised as a full turn",
          info.full_turn and abs(info.span_deg - 360) < 1.0, str(info))


def test_a_repetitive_scene_is_not_trimmed_on_a_false_match():
    """The same thing at unit level, with a scene built to fool the matcher."""
    tile = cv2.GaussianBlur(
        np.random.RandomState(9).randint(0, 255, (200, 300, 3)).astype(np.uint8),
        (11, 11), 0)
    repetitive = np.hstack([tile] * 12)          # every 300 px looks like the last

    loose, matched_loose = F._trim_wrap_overlap(repetitive)
    bounded, matched_bounded = F._trim_wrap_overlap(repetitive, max_trim_px=8)

    check("unbounded, the matcher does cut a repetitive scene", matched_loose,
          "this is the hazard the cap exists for")
    check("bounded by the known geometry, it refuses",
          not matched_bounded and bounded.shape[1] == repetitive.shape[1],
          "%d columns" % bounded.shape[1])


def test_the_vertical_scale_is_not_squashed():
    """A pixel must mean the same angle up-down as it does left-right.

    The height used to come from `width // 2`, which is only correct when the
    width is exactly one turn. Anything that shortened the width - a trim, a
    crop - left the target too short, and the strip was squashed into it.
    """
    pano = ring_panorama(circumference=3600, height=800, equator_y=400,
                         marker_gap=200)
    out, info = F.finish_panorama(pano, circumference_px=3600, equator_y=400)

    check("the finished image is exactly 2:1",
          abs(out.shape[1] / float(out.shape[0]) - 2.0) < 0.01,
          "%dx%d" % (out.shape[1], out.shape[0]))
    check("180 degrees tall at the image's own scale",
          out.shape[0] == int(round(180 * info.px_per_deg)),
          "%d rows, expected %d" % (out.shape[0], round(180 * info.px_per_deg)))

    rows = _bright_rows(out)
    check("both markers survive", len(rows) == 2, str(rows))
    if len(rows) == 2:
        check("the gap between them is unchanged (no squash)",
              abs((rows[1] - rows[0]) - 200) <= 2,
              "%d px apart, was 200" % (rows[1] - rows[0]))


def test_the_horizon_lands_in_the_middle():
    """A capture with pole shots is not symmetric about the horizon.

    Centring the strip is only right when it is. equator_y says where the
    horizon really is, and it belongs at the centre of the finished sphere.
    """
    # Horizon high up: far more was shot below it than above, as happens when
    # someone photographs the floor.
    pano = ring_panorama(circumference=3600, height=900, equator_y=200,
                         marker_gap=300)
    out, info = F.finish_panorama(pano, circumference_px=3600, equator_y=200)

    rows = _bright_rows(out)
    check("the horizon marker survives", len(rows) >= 1, str(rows))
    if rows:
        middle = out.shape[0] / 2.0
        check("the horizon sits at the vertical centre",
              abs(rows[0] - middle) <= 3,
              "row %d of %d, centre is %.0f" % (rows[0], out.shape[0], middle))


def test_a_partial_capture_is_refused():
    """The failure this whole signature exists to prevent.

    150 degrees of view stretched around a full sphere is what a partial
    capture used to become. Now it is reported as what it is.
    """
    partial = ring_panorama(circumference=3600, height=800, equator_y=400)[:, :1500]
    out, info = F.finish_panorama(partial, circumference_px=3600, equator_y=400)

    check("the span is measured, not assumed",
          abs(info.span_deg - 150) < 1.0, "%.1f degrees" % info.span_deg)
    check("it is not offered as a sphere", not info.full_turn, str(info))
    check("and it was not padded out to 2:1",
          abs(out.shape[1] / float(out.shape[0]) - 2.0) > 0.1,
          "%dx%d" % (out.shape[1], out.shape[0]))


def test_a_real_overshoot_is_still_trimmed():
    """Capping the trim must not stop it happening when it should."""
    base = ring_panorama(circumference=3600, height=400, equator_y=200)
    overshot = np.hstack([base, base[:, :300]])      # 30 degrees of overlap

    out, info = F.finish_panorama(overshot, circumference_px=3600, equator_y=200)
    check("the duplicate columns are cut back to one turn",
          abs(out.shape[1] - 3600) <= 40, "%d columns" % out.shape[1])
    check("and it is a full turn again", info.full_turn, str(info))


def test_a_capture_straddling_the_seam_is_measured_by_coverage():
    """Width is not coverage, and this is the case that proves it.

    Half a turn that happens to begin near the wrap puts photos hard against
    both edges of the canvas, with nothing in between. The canvas is then the
    full width of a turn while most of it is empty - so measuring the width
    called it a complete sphere and stretched it onto one.
    """
    pano = np.zeros((400, 3600, 3), np.uint8)
    content = ring_panorama(circumference=3600, height=400, equator_y=200)
    pano[:, :600] = content[:, :600]            # 60 degrees at the left edge
    pano[:, -600:] = content[:, -600:]          # 60 degrees at the right edge

    out, info = F.finish_panorama(pano, circumference_px=3600, equator_y=200)
    check("only the photographed columns count",
          abs(info.span_deg - 120) < 6, "%.0f degrees" % info.span_deg)
    check("a wide but mostly empty canvas is not a sphere",
          not info.full_turn, str(info))


def test_a_gap_in_the_middle_is_not_a_full_turn():
    """The user who skipped a direction.

    Photos at both ends and nothing facing one way. No measure of width can see
    this; counting occupied columns can.
    """
    pano = ring_panorama(circumference=3600, height=400, equator_y=200).copy()
    pano[:, 1200:2100] = 0                      # 90 degrees never photographed

    out, info = F.finish_panorama(pano, circumference_px=3600, equator_y=200)
    check("the missing 90 degrees are noticed",
          abs(info.span_deg - 270) < 6, "%.0f degrees" % info.span_deg)
    check("and it is not offered as a sphere", not info.full_turn, str(info))


if __name__ == "__main__":
    for fn in [test_fast_crop_matches_exact_and_is_much_quicker,
               test_small_images_use_the_exact_search,
               test_finish_produces_a_clean_sphere_texture,
               test_wrap_seam_is_closed,
               test_a_known_full_turn_keeps_every_column,
               test_a_repetitive_scene_is_not_trimmed_on_a_false_match,
               test_the_vertical_scale_is_not_squashed,
               test_the_horizon_lands_in_the_middle,
               test_a_partial_capture_is_refused,
               test_a_real_overshoot_is_still_trimmed,
               test_a_capture_straddling_the_seam_is_measured_by_coverage,
               test_a_gap_in_the_middle_is_not_a_full_turn]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
