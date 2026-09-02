"""Turning a raw stitcher result into a clean, seam-free sphere texture.

cv2.Stitcher warps every photo onto a shared curved surface. The result is
almost never a neat rectangle: the edges are ragged, and everything outside the
warped region is filled with pure black. Two visible problems follow.

1. Those black wedges show up as bars along the top and bottom of the 360.
2. A sphere texture wraps around, so the LEFT edge sits directly against the
   RIGHT edge. If the capture does not close a perfect circle, that join is a
   hard black line exactly where the first and last photo meet.

This module fixes both: crop away the black, then make the two ends meet.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Anything this dark is treated as "no image data here", not as real content.
BLACK_THRESHOLD = 10


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


def crop_black_borders(img, min_keep=0.35):
    """Crop to the largest rectangle containing no warp padding.

    min_keep guards against a pathological stitch where the biggest clean
    rectangle is a sliver: if we would throw away more than that fraction of
    the pixels, the crop is skipped and the caller keeps the full frame.
    """
    mask = content_mask(img)
    if mask.all():
        return img, False

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


def _trim_wrap_overlap(img, search_frac=0.25, strip_frac=0.05):
    """Remove the part of the right edge that repeats the left edge.

    A ring capture usually overshoots: the last photo overlaps the first, so the
    panorama covers a bit more than 360 degrees and the same wall appears at both
    ends. Wrapping that onto a sphere shows the overlap as a hard join.

    We take a strip from the left edge and look for where it recurs near the
    right edge. Everything after that match is the duplicate, and gets cut, so
    the image spans exactly one full turn.

    Returns (image, matched: bool).
    """
    h, w = img.shape[:2]
    strip_w = max(8, int(w * strip_frac))
    search_w = int(w * search_frac)
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


def pad_to_equirect(img):
    """Give the panorama the 2:1 shape a sphere texture must have.

    A full equirectangular image spans 360 horizontally and 180 vertically. A
    ring capture covers far less vertically, so rather than stretching the
    photos (which bows every straight line) the strip is placed at the horizon
    and the space above and below is filled by extending the edge rows.

    Only rows are added or removed, never columns, so the wrap stays intact.
    """
    h, w = img.shape[:2]
    target_h = w // 2
    if h >= target_h:
        top = (h - target_h) // 2
        return img[top:top + target_h]

    pad_total = target_h - h
    pad_top = pad_total // 2
    # BORDER_REPLICATE smears the top and bottom rows outward. It is not real
    # sky or floor, but it reads as a soft vignette rather than a black void.
    return cv2.copyMakeBorder(img, pad_top, pad_total - pad_top, 0, 0, cv2.BORDER_REPLICATE)


def finish_panorama(pano, wrap=True, equirect=True, max_width=4096):
    """Full clean-up: remove padding, size it, close the wrap, shape the sphere.

    Order matters. Cropping and resizing both disturb the edge columns, so the
    seam is closed after them and only row padding happens afterwards.
    """
    out, cropped = crop_black_borders(pano)
    if not cropped:
        out = fill_remaining_black(out)
    if equirect:
        out = limit_width(out, max_width)
    if wrap:
        out = close_wrap_seam(out)
    if equirect:
        out = pad_to_equirect(out)
    return out
