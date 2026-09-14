"""Painting posed photos straight onto a full 360 x 180 equirectangular canvas.

This is the approach the Android Photo Sphere app uses, and it replaces the
OpenCV spherical warper for pose stitching. The difference is where the
geometry lives.

The warper maps each photo forwards onto an open-ended strip. That strip has no
idea the world wraps around, so three things have to be patched up afterwards:

  * the wrap seam. The first and last photos never overlap on the strip - one
    sits at the far left, the other at the far right - so nothing blends them.
    The finish stage then measures a brightness "step" between the two edges
    and ramps it out, but the photos themselves still meet at a hard join.
  * the poles. A spherical warp of a shot aimed at the ceiling diverges, so pole
    shots become huge tiles and a starburst of stretched pixels.
  * the frame. Canvas size, horizon row and span all have to be inferred back
    from the warper's corners, then trimmed and padded into a 2:1 image.

Rendering the other way round removes all three. Every output pixel IS a
direction on the sphere; for each photo that direction is rotated into the
camera's axes and divided through by depth to find the pixel that saw it, and
`remap` samples it. The canvas is 2:1 and wraps by construction, a pole shot is
simply the rows near the top, and a row's latitude is fixed by its index.

The wrap is blended properly by giving the blender a canvas padded on both
sides, feeding every photo that touches one edge a second time on the other
side, and cutting the padding off afterwards. The seam between the last and
first photo is then a seam like any other.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Border samples per edge used to find the block of canvas a photo can reach.
_FOOTPRINT_SAMPLES = 48

# Rows remapped per slice, so the float coordinate maps for a pole shot (which
# reaches the full canvas width) never exist all at once.
_REMAP_BAND_ROWS = 256

# Exposure solve, as in OpenCV's GainCompensator: how much measurement noise to
# expect in a mean intensity, and how strongly gains are pulled back to 1.
_GAIN_SIGMA_N = 10.0
_GAIN_SIGMA_G = 0.1
_GAIN_WORK_WIDTH = 512

# Where in a canvas pixel its direction is taken from. 0 is OpenCV's spherical
# warper convention (column = focal * longitude, on the integer grid), kept so a
# photo lands on exactly the pixels the warper path gave it - hotspots saved
# against an older panorama stay where they were put.
PIXEL_CENTRE = 0.0


def _unit_direction(R, K_inv, px, py):
    """World directions (warper frame) for image points, shape (n, 3)."""
    pts = np.stack([px, py, np.ones_like(px)], axis=0).astype(np.float64)
    d = (R.astype(np.float64) @ (K_inv @ pts)).T
    return d / np.linalg.norm(d, axis=1, keepdims=True)


def _lon_theta(d):
    """Longitude in [-pi, pi) with 0 mid-canvas, and polar angle from the zenith.

    Matches OpenCV's spherical warper exactly - azimuth atan2(x, z), and the
    world's up is -Y - so a photo lands where the old path put it.
    """
    lon = np.arctan2(d[:, 0], d[:, 2])
    theta = np.arccos(np.clip(-d[:, 1], -1.0, 1.0))
    return lon, theta


def _sees(R, K, w, h, world_dir, cx_shift=0.0, cy_shift=0.0):
    """Does this camera's frame contain the given world direction?"""
    c = R.T.astype(np.float64) @ np.asarray(world_dir, dtype=np.float64)
    if c[2] <= 1e-9:
        return False
    u = K[0, 0] * c[0] / c[2] + K[0, 2] + cx_shift
    v = K[1, 1] * c[1] / c[2] + K[1, 2] + cy_shift
    return 0 <= u < w and 0 <= v < h


def footprint(R, K, w, h, width, height):
    """The block of canvas a photo can reach: (col0, col1, row0, row1).

    col0 may be negative or col1 past `width`: a photo straddling the wrap gets
    one continuous column range, which the caller folds back later. A photo that
    contains a pole reaches every column, so it gets the whole width.
    """
    n = _FOOTPRINT_SAMPLES
    t = np.linspace(0.0, 1.0, n, endpoint=False)
    # Walk the border as one closed loop, so longitude can be unwrapped along it.
    px = np.concatenate([t * (w - 1), np.full(n, w - 1.0), (1 - t) * (w - 1), np.zeros(n)])
    py = np.concatenate([np.zeros(n), t * (h - 1), np.full(n, h - 1.0), (1 - t) * (h - 1)])
    d = _unit_direction(R, np.linalg.inv(K.astype(np.float64)), px, py)
    lon, theta = _lon_theta(d)

    px_per_rad_x = width / (2.0 * math.pi)
    px_per_rad_y = height / math.pi
    margin = 2

    row0 = int(math.floor(theta.min() * px_per_rad_y)) - margin
    row1 = int(math.ceil(theta.max() * px_per_rad_y)) + margin

    has_zenith = _sees(R, K, w, h, (0.0, -1.0, 0.0))
    has_nadir = _sees(R, K, w, h, (0.0, 1.0, 0.0))
    if has_zenith:
        row0 = 0
    if has_nadir:
        row1 = height

    if has_zenith or has_nadir:
        col0, col1 = 0, width
    else:
        unwrapped = np.unwrap(np.append(lon, lon[0]))
        lo, hi = unwrapped.min(), unwrapped.max()
        if hi - lo >= 2.0 * math.pi - 1e-6:
            col0, col1 = 0, width
        else:
            col0 = int(math.floor((lo + math.pi) * px_per_rad_x)) - margin
            col1 = int(math.ceil((hi + math.pi) * px_per_rad_x)) + margin
            # Put the start inside the canvas; the end may run past the right edge.
            shift = (col0 // width) * width
            col0 -= shift
            col1 -= shift

    return col0, col1, max(0, row0), min(height, row1)


def render_frame(img, R, K, width, height, shift=(0.0, 0.0), pivot_ratio=0.0):
    """Sample one photo into its canvas footprint.

    Returns (tile BGR, mask uint8, (col0, row0)), or None if it reaches nothing.

    `pivot_ratio` is lever arm over scene distance - how far the lens sat from
    the axis the user turned about, as a fraction of how far away the scene is.
    The camera sits that far ahead of the pivot along its own optical axis, so
    only the depth of the ray changes. Zero is a lens turned about itself.
    """
    h, w = img.shape[:2]
    col0, col1, row0, row1 = footprint(R, K, w, h, width, height)
    tw, th = col1 - col0, row1 - row0
    if tw <= 0 or th <= 0:
        return None

    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx = float(K[0, 2]) + float(shift[0])
    cy = float(K[1, 2]) + float(shift[1])
    Rt = R.T.astype(np.float64)

    lon = (np.arange(col0, col1, dtype=np.float64) + PIXEL_CENTRE) / width * 2.0 * math.pi - math.pi
    sin_lon, cos_lon = np.sin(lon), np.cos(lon)

    tile = np.zeros((th, tw, 3), dtype=np.uint8)
    mask = np.zeros((th, tw), dtype=np.uint8)
    for b0 in range(0, th, _REMAP_BAND_ROWS):
        b1 = min(th, b0 + _REMAP_BAND_ROWS)
        theta = (np.arange(row0 + b0, row0 + b1, dtype=np.float64) + PIXEL_CENTRE) / height * math.pi
        sin_t, cos_t = np.sin(theta)[:, None], np.cos(theta)[:, None]
        # World direction per pixel, expanded lazily: x = sin(t) sin(lon),
        # y = -cos(t), z = sin(t) cos(lon). Camera coords are Rt @ d.
        dx = sin_t * sin_lon[None, :]
        dz = sin_t * cos_lon[None, :]
        dy = -cos_t
        camx = Rt[0, 0] * dx + Rt[0, 1] * dy + Rt[0, 2] * dz
        camy = Rt[1, 0] * dx + Rt[1, 1] * dy + Rt[1, 2] * dz
        camz = Rt[2, 0] * dx + Rt[2, 1] * dy + Rt[2, 2] * dz - pivot_ratio
        del dx, dz
        ahead = camz > 1e-6
        safe_z = np.where(ahead, camz, 1.0)
        u = fx * camx / safe_z + cx
        v = fy * camy / safe_z + cy
        del camx, camy, camz, safe_z
        valid = ahead & (u >= 0) & (u <= w - 1) & (v >= 0) & (v <= h - 1)
        if not valid.any():
            continue
        # Outside the photo the tile is filled with REFLECTED photo content,
        # not black, and only the mask says what is real. The multi-band
        # blender builds a Gaussian pyramid of every tile; black beyond the
        # edge bleeds into its coarse bands and leaves a dark halo along every
        # seam. OpenCV's warper path warps with BORDER_REFLECT for exactly this.
        mapx = np.where(ahead, u, -10.0).astype(np.float32)
        mapy = np.where(ahead, v, -10.0).astype(np.float32)
        tile[b0:b1] = cv2.remap(img, mapx, mapy, cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_REFLECT)
        mask[b0:b1] = valid.astype(np.uint8) * 255

    rows = mask.any(axis=1)
    cols = mask.any(axis=0)
    if not rows.any():
        return None
    # Tighten to what was actually painted; the footprint is conservative.
    r_lo, r_hi = int(np.argmax(rows)), th - int(np.argmax(rows[::-1]))
    c_lo, c_hi = int(np.argmax(cols)), tw - int(np.argmax(cols[::-1]))
    tile = np.ascontiguousarray(tile[r_lo:r_hi, c_lo:c_hi])
    mask = np.ascontiguousarray(mask[r_lo:r_hi, c_lo:c_hi])
    return tile, mask, (col0 + c_lo, row0 + r_lo)


def _paste_wrapped(canvas, piece, col, row, op):
    """Combine `piece` into `canvas` at (col, row), wrapping columns around."""
    width = canvas.shape[1]
    ph, pw = piece.shape[:2]
    c = 0
    while c < pw:
        dst = (col + c) % width
        n = min(pw - c, width - dst)
        region = canvas[row:row + ph, dst:dst + n]
        op(region, piece[:, c:c + n])
        c += n


def solve_gains(tiles, masks, corners, width, height):
    """One brightness gain per photo, from how their overlaps disagree.

    Solved on the wrapped canvas, so the first and last photo of a ring are
    compared with each other - the pair the warper path never saw together.
    Runs at low resolution: a mean intensity does not need detail.
    """
    n = len(tiles)
    if n < 2:
        return np.ones(n, dtype=np.float64)
    s = min(1.0, _GAIN_WORK_WIDTH / float(width))
    sw, sh = max(8, int(round(width * s))), max(4, int(round(height * s)))

    grays, smasks, scorners = [], [], []
    for tile, mask, (cx, cy) in zip(tiles, masks, corners):
        tw = max(1, int(round(tile.shape[1] * s)))
        th = max(1, int(round(tile.shape[0] * s)))
        g = cv2.resize(cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY), (tw, th),
                       interpolation=cv2.INTER_AREA).astype(np.float64)
        m = cv2.resize(mask, (tw, th), interpolation=cv2.INTER_AREA) >= 255
        full_g = np.zeros((sh, sw), np.float64)
        full_m = np.zeros((sh, sw), bool)
        r = min(sh - th, max(0, int(round(cy * s))))

        def put(region, src):
            region[...] = src
        _paste_wrapped(full_g, g, int(round(cx * s)), r, put)
        _paste_wrapped(full_m, m, int(round(cx * s)), r, put)
        grays.append(full_g)
        smasks.append(full_m)

    A = np.zeros((n, n))
    b = np.zeros(n)
    for i in range(n):
        for j in range(i, n):
            both = smasks[i] & smasks[j]
            N = float(both.sum())
            if i == j or N < 16:
                continue
            Ii = grays[i][both].mean()
            Ij = grays[j][both].mean()
            k = 2.0 * N / (_GAIN_SIGMA_N ** 2)
            A[i, i] += k * Ii * Ii
            A[j, j] += k * Ij * Ij
            A[i, j] -= k * Ii * Ij
            A[j, i] -= k * Ii * Ij
            reg = 2.0 * N / (_GAIN_SIGMA_G ** 2)
            A[i, i] += reg
            A[j, j] += reg
            b[i] += reg
            b[j] += reg
    for i in range(n):
        if A[i, i] == 0:            # overlaps nothing: leave it as shot
            A[i, i] = 1.0
            b[i] = 1.0
    try:
        gains = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return np.ones(n, dtype=np.float64)
    return np.clip(gains, 0.5, 2.0)


def _bands_for(width, height):
    blend_width = math.sqrt(float(width * height)) * 0.05
    bands = int(math.ceil(math.log(max(blend_width, 2.0)) / math.log(2.0))) - 1
    return max(3, min(7, bands))


def blend_wrapped(tiles, masks, corners, width, height, find_seams=None):
    """Seam and multi-band blend on a canvas that wraps left to right.

    Returns (panorama BGR, coverage uint8), both exactly width x height.
    """
    bands = _bands_for(width, height)
    # The blender's widest band reaches about 2^bands pixels; pad by more than
    # that so the blend at column 0 sees the photos on the far right.
    pad = min(width // 2, (1 << (bands + 1)) + 16)

    cover = np.zeros((height, width), dtype=np.uint8)
    for mask, (cx, cy) in zip(masks, corners):
        _paste_wrapped(cover, mask, cx, cy,
                       lambda region, src: np.maximum(region, src, out=region))

    pieces, piece_masks, piece_corners = [], [], []
    for tile, mask, (cx, cy) in zip(tiles, masks, corners):
        tw = tile.shape[1]
        for offset in (-width, 0, width):
            x0 = cx + offset
            lo = max(x0, -pad)
            hi = min(x0 + tw, width + pad)
            if hi <= lo:
                continue
            m = mask[:, lo - x0:hi - x0]
            if not m.any():
                continue
            pieces.append(np.ascontiguousarray(tile[:, lo - x0:hi - x0]))
            piece_masks.append(np.ascontiguousarray(m))
            piece_corners.append((int(lo), int(cy)))

    if find_seams is not None:
        find_seams(pieces, piece_masks, piece_corners)

    log.info("[orbit-worker] blending %d photos (%d pieces across the wrap) onto "
             "a %dx%d sphere with %d bands", len(tiles), len(pieces), width, height, bands)
    blender = cv2.detail_MultiBandBlender()
    blender.setNumBands(bands)
    blender.prepare((-pad, 0, width + 2 * pad, height))
    for im, m, c in zip(pieces, piece_masks, piece_corners):
        blender.feed(im.astype(np.int16), m, c)
    result, _ = blender.blend(None, None)
    pano = cv2.convertScaleAbs(result)[:, pad:pad + width]
    pano = np.ascontiguousarray(pano)
    pano[cover == 0] = 0
    return pano, cover
