"""Turning a raw stitcher result into a clean, seam-free sphere texture.

cv2.Stitcher warps every photo onto a shared curved surface. The result is
almost never a neat rectangle: the edges are ragged, and everything outside the
warped region is filled with pure black. Two visible problems follow.

1. Those black wedges show up as bars along the top and bottom of the 360.
2. A sphere texture wraps around, so the LEFT edge sits directly against the
   RIGHT edge. If the capture does not close a perfect circle, that join is a
   hard black line exactly where the first and last photo meet.

This module fixes both: crop away the black, then make the two ends meet.

Everything here needs to know ONE thing the pixels cannot tell it: how many
degrees of the world the panorama actually spans. Without that, a 150-degree
strip and a full turn look identical - both are just wide images - and the old
version of this module treated every input as a full turn. It padded to 2:1 on
that assumption and the viewer wrapped the result around a whole sphere, so a
partial capture came out stretched by however much of the world was missing.

So the scale is passed in as `circumference_px`: how many pixels one full turn
occupies at the panorama's own resolution. The stitchers know it, because it
falls straight out of the focal length they warped with. From it everything
else follows - the true span, how much of the width is overshoot past 360, and
how tall a correct equirectangular image would be - and a capture that is not a
full turn can be refused instead of silently stretched.
"""
import collections
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Anything this dark is treated as "no image data here", not as real content.
BLACK_THRESHOLD = 10

# How far short of a full turn a capture may fall and still be called a sphere.
# A ring shot by hand rarely closes to the degree, and the wrap seam blend
# hides a small shortfall; much more than this and the stretch is visible.
FULL_TURN_TOLERANCE_DEG = 12.0


class PanoramaInfo(collections.namedtuple(
        "PanoramaInfo", "span_deg px_per_deg full_turn")):
    """What the finished panorama turned out to be.

    span_deg    how much of a turn it actually covers, in degrees - counted
                from the columns that hold a photo, not from the width
    px_per_deg  horizontal scale of the finished image
    full_turn   whether it is close enough to 360 to use as a sphere texture
    """
    __slots__ = ()


def content_mask(img):
    """True where the image has real content rather than warp padding."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return gray > BLACK_THRESHOLD


def _largest_interior_rect(mask):
    """Largest all-True axis-aligned rectangle in a boolean mask.

    Classic maximal-rectangle-in-a-histogram sweep: for each row, height[c] is
    how many unbroken True pixels sit above that column, then the widest bar
    span is found with a monotonic stack. O(rows x cols).

    Returns (x, y, w, h), or None if the mask is empty.
    """
    rows, cols = mask.shape
    height = np.zeros(cols + 1, dtype=np.int32)  # +1 sentinel to flush the stack
    best = None
    best_area = 0

    for y in range(rows):
        row = mask[y]
        height[:cols] = np.where(row, height[:cols] + 1, 0)

        stack = []  # indices of bars in increasing height order
        for x in range(cols + 1):
            start = x
            while stack and stack[-1][1] > height[x]:
                idx, h = stack.pop()
                area = h * (x - idx)
                if area > best_area:
                    best_area = area
                    best = (idx, y - h + 1, x - idx, h)
                start = idx
            stack.append((start, height[x]))

    return best


# Resolution the crop search runs at. The rectangle only needs to be accurate
# to a few pixels, but the search is an inherently sequential scan, so running
# it on a full-size panorama costs ~10 seconds of pure-Python looping. On a
# downscaled mask it costs milliseconds, and the result is scaled back up.
_CROP_SEARCH_WIDTH = 480


def _largest_interior_rect_fast(mask):
    """Same search, run on a downscaled mask and scaled back.

    The mask is shrunk with INTER_AREA and then thresholded at full white, so a
    cell only counts as content when every pixel under it was content. That
    makes the small-scale answer conservative: the rectangle it finds is always
    inside real content, never straddling the ragged edge.
    """
    h, w = mask.shape
    if w <= _CROP_SEARCH_WIDTH:
        return _largest_interior_rect(mask)

    scale = _CROP_SEARCH_WIDTH / float(w)
    sw, sh = _CROP_SEARCH_WIDTH, max(1, int(round(h * scale)))
    small = cv2.resize(mask.astype(np.uint8) * 255, (sw, sh), interpolation=cv2.INTER_AREA)
    rect = _largest_interior_rect(small >= 255)
    if rect is None:
        return None

    x, y, rw, rh = rect
    fx, fy = w / float(sw), h / float(sh)
    # Round inwards so scaling back up cannot walk over the edge.
    x0 = int(math.ceil(x * fx))
    y0 = int(math.ceil(y * fy))
    x1 = int(math.floor((x + rw) * fx))
    y1 = int(math.floor((y + rh) * fy))
    if x1 - x0 < 16 or y1 - y0 < 16:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


# A row counts as "full" when this fraction of its columns carry content. Not
# 100%: a single stray dark pixel anywhere along a 4000-pixel row would
# otherwise disqualify it, and a handful of them would leave nothing to crop to.
_ROW_CONTENT_FRACTION = 0.99


def _content_row_band(mask, min_fraction=_ROW_CONTENT_FRACTION):
    """Longest run of rows that are content nearly all the way across.

    Returns (y, height), or None if no row qualifies.
    """
    full = mask.mean(axis=1) >= min_fraction
    best_y = best_h = 0
    run_start = None
    for y, ok in enumerate(full):
        if ok and run_start is None:
            run_start = y
        elif not ok and run_start is not None:
            if y - run_start > best_h:
                best_y, best_h = run_start, y - run_start
            run_start = None
    if run_start is not None and len(full) - run_start > best_h:
        best_y, best_h = run_start, len(full) - run_start
    return (best_y, best_h) if best_h >= 16 else None


def crop_black_borders(img, min_keep=0.35, rows_only=False):
    """Crop to the largest rectangle containing no warp padding.

    min_keep guards against a pathological stitch where the biggest clean
    rectangle is a sliver: if we would throw away more than that fraction of
    the pixels, the crop is skipped and the caller keeps the full frame.

    rows_only trims the top and bottom but never the sides. Use it whenever the
    panorama's horizontal extent is meaningful - which it is the moment we know
    the scale. Removing columns there would quietly shorten the turn the image
    covers and break the wrap, and it is the top and bottom that are ragged
    anyway: the ring closes on itself horizontally, so the sides have nothing
    to trim.
    """
    mask = content_mask(img)
    if mask.all():
        return img, False

    if rows_only:
        band = _content_row_band(mask)
        if band is None:
            return img, False
        y, h = band
        if h / float(img.shape[0]) < min_keep:
            log.warning("[orbit-worker] the only full-width rows are %.0f%% of the "
                        "panorama; keeping every row and filling the gaps instead",
                        h * 100.0 / img.shape[0])
            return img, False
        log.info("[orbit-worker] cropped ragged rows: %d -> %d rows (kept %.0f%%)",
                 img.shape[0], h, h * 100.0 / img.shape[0])
        return img[y:y + h], True

    rect = _largest_interior_rect_fast(mask)
    if rect is None:
        return img, False

    x, y, w, h = rect
    if w < 16 or h < 16:
        return img, False

    kept = (w * h) / float(img.shape[0] * img.shape[1])
    if kept < min_keep:
        log.warning(
            "[orbit-worker] clean crop would keep only %.0f%% of the panorama; "
            "keeping the full frame and filling the gaps instead", kept * 100
        )
        return img, False

    log.info("[orbit-worker] cropped black borders: %dx%d -> %dx%d (kept %.0f%%)",
             img.shape[1], img.shape[0], w, h, kept * 100)
    return img[y:y + h, x:x + w], True


def fill_remaining_black(img):
    """Inpaint any leftover padding so nothing reads as a black hole.

    Used when cropping was refused because it would have cost too much of the
    image. Nearest-neighbour inpainting is not clever, but a smeared edge is far
    less distracting in a 360 than a black wedge.
    """
    holes = ~content_mask(img)
    if not holes.any():
        return img
    mask = cv2.dilate(holes.astype(np.uint8), np.ones((5, 5), np.uint8), iterations=1)

    # Inpainting is designed for scratches, not for the wide bands left where
    # the user simply did not point the camera. Run over a large area it
    # produces long directional streaks - the smearing that makes a sparse
    # capture look melted.
    #
    # So: small holes get real inpainting, and large ones get a soft blurred
    # fill instead. A gentle wash reads as "nothing was photographed here",
    # which is honest, rather than as damaged photo.
    hole_fraction = holes.mean()
    if hole_fraction < 0.02:
        return cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)

    small = cv2.resize(img, (max(1, img.shape[1] // 8), max(1, img.shape[0] // 8)),
                       interpolation=cv2.INTER_AREA)
    small_mask = cv2.resize(mask, (small.shape[1], small.shape[0]),
                            interpolation=cv2.INTER_NEAREST)
    filled_small = cv2.inpaint(small, small_mask, 3, cv2.INPAINT_TELEA)
    filled = cv2.resize(filled_small, (img.shape[1], img.shape[0]),
                        interpolation=cv2.INTER_LINEAR)
    filled = cv2.GaussianBlur(filled, (0, 0), sigmaX=img.shape[1] / 220.0)

    soft = cv2.GaussianBlur(mask.astype(np.float32) * 255, (0, 0), sigmaX=9) / 255.0
    soft = np.clip(soft, 0, 1)[..., None]
    out = img.astype(np.float32) * (1 - soft) + filled.astype(np.float32) * soft
    log.info("[orbit-worker] %.0f%% of the sphere had no photo covering it; "
             "filled softly rather than inpainted", hole_fraction * 100)
    return np.clip(out, 0, 255).astype(np.uint8)


def _trim_wrap_overlap(img, search_frac=0.25, strip_frac=0.05, max_trim_px=None):
    """Remove the part of the right edge that repeats the left edge.

    A ring capture usually overshoots: the last photo overlaps the first, so the
    panorama covers a bit more than 360 degrees and the same wall appears at both
    ends. Wrapping that onto a sphere shows the overlap as a hard join.

    We take a strip from the left edge and look for where it recurs near the
    right edge. Everything after that match is the duplicate, and gets cut, so
    the image spans exactly one full turn.

    max_trim_px caps how much may be cut. Pass it whenever the scale is known,
    because then the overshoot is arithmetic rather than guesswork and the
    search only has to confirm it. Without the cap this is a bare template
    match on a repetitive scene, and it will happily "find" a duplicate that
    is not there: on a pose-stitched sphere measuring 359.7 degrees - where the
    true overshoot is nothing at all - it matched at 0.4 confidence and cut 46
    degrees of real view off the end. That missing wedge was then padded back
    as blank sphere, which is where the vertical squash came from.

    Returns (image, matched: bool).
    """
    h, w = img.shape[:2]
    strip_w = max(8, int(w * strip_frac))
    if max_trim_px is None:
        search_w = int(w * search_frac)
    else:
        if max_trim_px < 8:
            return img, False           # nothing worth cutting
        search_w = min(int(w * search_frac), int(max_trim_px) + strip_w)
    if w < strip_w * 4 or search_w < strip_w * 2:
        return img, False

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    template = gray[:, :strip_w]
    region = gray[:, w - search_w:]

    try:
        res = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    except cv2.error:
        return img, False

    _, score, _, loc = cv2.minMaxLoc(res)
    # Below this the "match" is noise, and cropping on it would delete real view.
    if score < 0.35:
        return img, False

    cut = (w - search_w) + loc[0]
    if cut <= w * 0.5 or cut >= w:
        return img, False
    if max_trim_px is not None and (w - cut) > max_trim_px:
        log.info("[orbit-worker] ignoring a wrap match that would cut %d px; "
                 "the geometry allows at most %d", w - cut, int(max_trim_px))
        return img, False

    log.info("[orbit-worker] trimmed %d duplicate columns at the wrap seam "
             "(match %.2f)", w - cut, score)
    return img[:, :cut], True


def close_wrap_seam(img, blend_frac=0.05):
    """Make the left and right edges join invisibly on the sphere.

    A sphere texture is cyclic: the last column sits directly against the first.
    After trimming the duplicate overlap there is still a small step at the join,
    so the final columns are cross-faded into the opening columns and those
    opening columns are then dropped.

    The result is that the new last column is the old column (blend - 1) and the
    new first column is the old column (blend) - genuinely adjacent pixels, so
    the wrap is continuous rather than merely softened.

    This DISCARDS columns, so it is only right when the panorama's horizontal
    extent means nothing in particular - which is the case only when no stitcher
    told us the scale. When the scale is known, feather_wrap_seam does the same
    job without shortening the turn.
    """
    img, _ = _trim_wrap_overlap(img)

    h, w = img.shape[:2]
    blend = int(w * blend_frac)
    if blend < 4 or w < blend * 4:
        return img

    out = img.astype(np.float32)
    head = out[:, :blend].copy()          # the columns we will fade towards
    tail = out[:, w - blend:].copy()      # the columns being faded

    ramp = np.linspace(0.0, 1.0, blend, dtype=np.float32)[None, :, None]
    out[:, w - blend:] = tail * (1.0 - ramp) + head * ramp

    # Dropping the head is what makes the join continuous instead of duplicated.
    out = out[:, blend:]
    return np.clip(out, 0, 255).astype(np.uint8)


def feather_wrap_seam(img, band_frac=0.04):
    """Soften the join without removing a single column.

    When the scale is known and the panorama really does span one turn, its last
    column and its first column are already neighbours on the sphere - the
    geometry put them there. Nothing needs moving. What is usually left is a
    brightness STEP, because the first and last photo were metered separately.

    So rather than cross-fading (which would need columns to spare, and would
    shorten the turn), the step is measured once and then ramped out across a
    band either side of the join: the right edge is walked half way towards the
    left edge and the left edge half way back. The correction decays to nothing
    within the band, so the rest of the panorama is untouched.
    """
    h, w = img.shape[:2]
    band = int(w * band_frac)
    if band < 4 or w < band * 4:
        return img

    edge = max(1, band // 4)
    out = img.astype(np.float32)
    step = out[:, :edge].mean(axis=(0, 1)) - out[:, w - edge:].mean(axis=(0, 1))
    if float(np.abs(step).max()) < 1.0:
        return img                         # already continuous; leave it alone

    ramp = np.linspace(0.0, 1.0, band, dtype=np.float32)[None, :, None]
    half = step[None, None, :] / 2.0
    out[:, w - band:] += half * ramp               # right edge rises to meet it
    out[:, :band] -= half * (1.0 - ramp)           # left edge comes down to it
    log.info("[orbit-worker] levelled a wrap-seam brightness step of %s",
             np.round(step, 1).tolist())
    return np.clip(out, 0, 255).astype(np.uint8)


def limit_width(img, max_width=4096):
    """Downscale a very wide panorama before any seam work.

    Resizing resamples the edge columns, which would reintroduce a small step at
    the wrap. Doing it first means the seam fix is the last thing to touch those
    columns.
    """
    h, w = img.shape[:2]
    if w <= max_width:
        return img
    return cv2.resize(img, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA)


def pad_to_equirect(img, px_per_deg, equator_y=None):
    """Give the panorama the 2:1 shape a sphere texture must have.

    A full equirectangular image spans 360 horizontally and 180 vertically. A
    ring capture covers far less vertically, so rather than stretching the
    photos (which bows every straight line) the strip is placed at the horizon
    and the space above and below is filled by extending the edge rows.

    The target height comes from px_per_deg - 180 degrees at the image's own
    scale - and NOT from half the width. Those agree only when the image spans
    exactly 360, which is the assumption that used to go unchecked: after
    anything trimmed the width, half of it was too short, and the strip got
    squashed into a canvas where a pixel no longer meant the same angle
    vertically as it did horizontally.

    equator_y says which row is the horizon. Without it the strip is assumed to
    be centred on the horizon, which is true of a level ring but not of a
    capture that includes shots of the ceiling or floor - those extend the
    canvas one way only, and centring such a strip tilts the whole world.

    Only rows are added or removed, never columns, so the wrap stays intact.
    """
    h, w = img.shape[:2]
    target_h = int(round(180.0 * px_per_deg))
    if target_h < 2:
        return img

    # A 360 file must be EXACTLY two by one, and rounding alone is enough to
    # miss it: 4093 columns against a computed 2046 rows is off by half a pixel
    # and reads as 2.0005:1. Viewers that check - Google Photos, Facebook, a
    # headset gallery, Photo Sphere Viewer - either refuse such a file or map it
    # onto the sphere slightly wrong, which is what a downloaded panorama
    # looked like. So when the computed height is within a whisker of half the
    # width, we take half the width; an odd column is dropped first, since a
    # 2:1 image needs an even one. A real disagreement (the width does not
    # actually span a turn) is left alone rather than papered over.
    if w % 2:
        img = img[:, :w - 1]
        w -= 1
    if abs(target_h - w / 2.0) <= max(2.0, 0.02 * w / 2.0):
        target_h = w // 2
    if equator_y is None:
        equator_y = h / 2.0

    # Put the horizon at the middle of the finished image.
    top = int(round(equator_y - target_h / 2.0))

    if top >= 0 and top + target_h <= h:
        return img[top:top + target_h]

    pad_top = max(0, -top)
    pad_bottom = max(0, (top + target_h) - h)
    out = _pad_towards_pole(img, pad_top, pad_bottom)
    top += pad_top
    return out[top:top + target_h]


def _pad_towards_pole(img, pad_top, pad_bottom):
    """Fill the unphotographed sky and floor, converging to one colour.

    This used to be cv2.BORDER_REPLICATE, which copies the edge row outward
    unchanged. On a flat image that reads as a reasonable vignette, and it is
    what the comment here used to claim. On a SPHERE it is badly wrong.

    Every column of an equirectangular image converges to the same point at the
    pole. So a replicated row - full of different colours, a dark railing next
    to bright sky next to a wall - becomes hundreds of coloured wedges all
    meeting at one point. In the viewer that is a radial starburst filling the
    whole screen the moment anyone looks up, and it is far more distracting
    than the missing photograph it stands in for.

    The row at the pole has to be a single colour, because it IS a single
    point. So the edge row is faded to its own average as it approaches the
    pole. The result is a soft vignette that settles into one flat tone
    overhead - honest about there being no photograph, and quiet enough to
    ignore.

    The fade is eased rather than linear so the join at the edge of the real
    photo stays invisible: the first few rows barely change, and most of the
    convergence happens out where nobody is looking.
    """
    if pad_top <= 0 and pad_bottom <= 0:
        return img

    h, w = img.shape[:2]
    parts = []

    def band(row, n):
        """n rows running from `row` to a single flat colour at the pole.

        Two things happen together, and both are needed.

        Fading to the average alone still leaves streaks: halfway through the
        band the row is half its original self, so every colour difference
        along it is still there, just fainter - and on the sphere those
        differences are still wedges converging on a point.

        So the row is also BLURRED horizontally, by more and more as it
        approaches the pole. A streak is horizontal variation by definition, so
        blurring it away is the direct fix rather than a cosmetic one. By the
        last row the blur is wider than the image and nothing survives but the
        average, which is exactly what a single point should look like.
        """
        # A trimmed mean, so one bright window or dark doorway in the edge row
        # does not drag the whole sky towards it.
        flat = np.sort(row.reshape(-1, row.shape[-1]), axis=0)
        lo, hi = int(len(flat) * 0.1), int(len(flat) * 0.9)
        target = (flat[lo:hi].mean(axis=0) if hi > lo
                  else row.reshape(-1, row.shape[-1]).mean(axis=0))

        out = np.empty((n, w, row.shape[-1]), np.float32)
        base = row.astype(np.float32).reshape(1, w, -1)

        # Blur is applied at a handful of widths and interpolated between,
        # because blurring every row separately on a 4096-wide panorama is
        # thousands of convolutions for a part of the image nobody studies.
        steps = 12
        widths = np.linspace(0, 1, steps) ** 1.6      # gentle at first
        # The blur has to wrap, or the two ends of the panorama drift apart and
        # reintroduce the seam this module spends its time closing. OpenCV
        # refuses BORDER_WRAP on a column filter, so the row is tiled three
        # times and the middle copy taken back afterwards.
        wide = np.hstack([base, base, base])
        cache = []
        for f in widths:
            sigma = f * w / 6.0
            if sigma < 0.6:
                cache.append(base)
                continue
            b = cv2.GaussianBlur(wide, (0, 0), sigmaX=sigma, sigmaY=0,
                                 borderType=cv2.BORDER_REPLICATE)
            cache.append(b[:, w:2 * w])

        for i in range(n):
            t = i / max(1, n - 1)
            k = t * (steps - 1)
            a, b = int(k), min(steps - 1, int(k) + 1)
            blurred = cache[a] * (1 - (k - a)) + cache[b] * (k - a)
            # And fade what is left towards the flat colour.
            m = t * t * (3.0 - 2.0 * t)
            out[i] = blurred[0] * (1 - m) + target.reshape(1, -1) * m

        return np.clip(out, 0, 255).astype(img.dtype)

    if pad_top > 0:
        # Reversed: the row nearest the photo is closest to it, and the row at
        # the top of the canvas - the zenith - is the flat colour.
        parts.append(band(img[0], pad_top)[::-1])
    parts.append(img)
    if pad_bottom > 0:
        parts.append(band(img[-1], pad_bottom))

    return np.vstack(parts)


def finish_panorama(pano, circumference_px=None, equator_y=None,
                    wrap=True, equirect=True, max_width=4096):
    """Full clean-up: remove padding, size it, close the wrap, shape the sphere.

    Order matters. Cropping and resizing both disturb the edge columns, so the
    seam is closed after them and only row padding happens afterwards.

    circumference_px is how many pixels one full turn spans in `pano`. Pass it
    whenever a stitcher knows - both of ours do - and this can then work out the
    real geometry instead of assuming a full turn. equator_y is the row the
    horizon falls on, in `pano`.

    Returns (image, PanoramaInfo). Check `info.full_turn` before using the
    result as a sphere texture: when it is False the capture did not go all the
    way round, and stretching it over a sphere is exactly the failure this
    signature exists to prevent.
    """
    known = circumference_px is not None and circumference_px > 0

    out, cropped = crop_black_borders(pano, rows_only=known)
    if known and equator_y is not None and cropped:
        # Rows came off the top; the horizon moved up with them.
        equator_y -= _rows_removed(pano, out)

    # How much of the turn was actually photographed, counted in columns that
    # hold something.
    #
    # NOT the width of the canvas. The two come apart whenever the capture
    # straddles the panorama's wrap: half a turn that happens to start near the
    # seam puts tiles hard against both edges, and the canvas between them
    # stretches the whole way round while most of it stays empty. Measuring the
    # canvas called that a complete sphere. Counting occupied columns calls it
    # what it is, and it catches a capture with a hole in the middle too - the
    # user who skipped a direction - which no measure of width ever could.
    #
    # It has to happen here, before fill_remaining_black paints into the gaps
    # and makes every column look occupied.
    covered_px = float(content_mask(out).any(axis=0).sum()) if known else None

    if not cropped:
        out = fill_remaining_black(out)

    px_per_deg = (circumference_px / 360.0) if known else None

    if equirect:
        before_w = out.shape[1]
        out = limit_width(out, max_width)
        if out.shape[1] != before_w:
            scale = out.shape[1] / float(before_w)
            if px_per_deg is not None:
                px_per_deg *= scale
            if equator_y is not None:
                equator_y *= scale
            if covered_px is not None:
                covered_px *= scale

    if px_per_deg is not None:
        # The scale is known, so the overshoot past a full turn is arithmetic.
        overshoot = out.shape[1] - 360.0 * px_per_deg
        if wrap:
            if overshoot > 8:
                # Allow a little slack around the computed figure: the stitch is
                # not exact to the pixel.
                out, _ = _trim_wrap_overlap(
                    out, max_trim_px=int(overshoot + 0.02 * out.shape[1]))
            out = feather_wrap_seam(out)
        # Trimming only ever removes columns that duplicated others, so the
        # coverage measured before it still stands. A capture that overshot a
        # full turn cannot cover more than one.
        span = min(360.0, covered_px / px_per_deg)
    else:
        if wrap:
            out = close_wrap_seam(out)
        # Nothing measured the scale. The only assumption left is the one this
        # module used to make everywhere: that what we have is one full turn.
        px_per_deg = out.shape[1] / 360.0
        span = 360.0

    full_turn = abs(span - 360.0) <= FULL_TURN_TOLERANCE_DEG
    if not full_turn:
        log.warning("[orbit-worker] this panorama covers %.0f degrees, not a full "
                    "turn; it is not usable as a sphere texture", span)

    if equirect and full_turn:
        out = pad_to_equirect(out, px_per_deg, equator_y)

    return out, PanoramaInfo(span_deg=span, px_per_deg=px_per_deg, full_turn=full_turn)


def _rows_removed(before, after):
    """How many rows crop_black_borders took off the TOP.

    It only ever removes a contiguous band, so matching the first surviving row
    against the original is enough to locate it.
    """
    if after.shape[0] >= before.shape[0]:
        return 0
    first = after[0]
    for y in range(before.shape[0] - after.shape[0] + 1):
        if np.array_equal(before[y], first):
            return y
    return (before.shape[0] - after.shape[0]) // 2
