"""Cutting a finished panorama into cube tiles.

A single equirectangular JPEG is the simplest thing that works, and it is what
the viewer falls back to. It has two problems that only show up on real devices.

The first is the GPU. An equirect has to be uploaded as ONE texture, and older
phones refuse anything wider than 4096 - the panorama simply never appears,
black sphere, no error. Even where it is accepted, a 4096x2048 texture is 32MB
of video memory held for as long as the view is open.

The second is that the whole image is downloaded before anything is drawn, and
most of it is behind you.

Cube tiles fix both. The sphere becomes six flat faces, each cut into small
squares at several resolutions, and the viewer fetches only the squares it can
actually see at the resolution it needs. Nothing is ever a single large texture.

The face size is deliberately NOT larger than the source deserves. An
equirectangular image 4096 wide carries 4096/360 = 11.4 pixels per degree at the
equator, and a cube face spans 90 degrees, so 1024 is the size at which the tiles
match the panorama. Going higher would upsample: more tiles, more storage, more
requests, and not one extra pixel of detail.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Face letters, and how each is rotated relative to the front face. This is the
# viewer's own cube convention: the front face looks along -Z, +Y is up, and a
# face is reached by rotating that base direction about X and then about Y.
FACES = "fudlrb"
FACE_ROTATION = {
    "f": (0.0, 0.0),
    "b": (0.0, math.pi),
    "l": (0.0, math.pi / 2),
    "r": (0.0, -math.pi / 2),
    "u": (math.pi / 2, 0.0),
    "d": (-math.pi / 2, 0.0),
}

# One tile per face per level, so a tile IS a face at that resolution.
#
# The obvious alternative is to cut each face into a grid - a 1024 face into
# four 512 tiles - and that is what tiling usually means. It is not used here,
# for one measured reason: with any level where the tile size is smaller than
# the face, the viewer draws the face you are looking AT and leaves its
# neighbours black. At an ordinary field of view that was two black bars down
# the sides of the frame, 16% of it. Every un-subdivided pyramid renders the
# whole view correctly; every subdivided one did not, across all four
# combinations of fallbackOnly and pinFirstLevel.
#
# Nothing is lost by it at this resolution. The panorama is capped at 4096
# wide, which makes a face 1024 square - about 150KB of JPEG - and splitting
# that into four does not meaningfully change what a phone downloads. The
# benefits that matter are still there: the GPU never receives one enormous
# texture, faces load only when looked at, and the small base level means
# something is drawn immediately.
#
# Revisit if face sizes ever grow past ~2048, where a single tile does start to
# be worth splitting.
MAX_FACE_SIZE = 1024


def face_size_for(equirect_width):
    """The cube face size that matches an equirectangular image's detail.

    Rounded to a power of two so the pyramid halves cleanly, and capped: past
    MAX_FACE_SIZE a single tile per face stops being a sensible download.
    """
    ideal = equirect_width / 4.0                      # 90 degrees of 360
    size = 1 << max(8, int(round(math.log2(max(ideal, 256)))))
    return min(size, MAX_FACE_SIZE)


# Rows of a cube face rendered at a time.
#
# Building the projection for a whole face at once needs about a dozen float32
# arrays the size of the face - the direction vectors, the radius, the two
# angles, the two coordinate maps - which for a 1024 face is roughly 48MB of
# temporaries. That lands on top of the panorama and the source photos, on a
# 512MB instance, immediately after the stitch, which is already the high-water
# mark of the whole worker.
#
# In strips the same arrays are a fraction of the size and the peak stops
# depending on the face size at all. The output is identical: each row's
# projection is independent of every other.
FACE_STRIP_ROWS = 128


def _strip_directions(face, size, y0, y1):
    """Unit vectors for the pixel centres of rows [y0, y1) of one cube face."""
    # Pixel centres, spread across [-1, 1].
    cols = (np.arange(size, dtype=np.float32) + 0.5) * (2.0 / size) - 1.0
    rows = (np.arange(y0, y1, dtype=np.float32) + 0.5) * (2.0 / size) - 1.0
    u, v = np.meshgrid(cols, rows)

    # The front face looks along -Z. Screen right is +X; screen DOWN is -Y,
    # because +Y is up, hence the minus on v.
    x = u
    y = -v
    z = np.full_like(u, -1.0)

    rx, ry = FACE_ROTATION[face]
    if rx:
        c, s = math.cos(rx), math.sin(rx)
        y, z = y * c - z * s, y * s + z * c
    if ry:
        c, s = math.cos(ry), math.sin(ry)
        x, z = z * s + x * c, z * c - x * s

    return x, y, z


def cube_face(equirect, face, size, strip_rows=FACE_STRIP_ROWS):
    """Render one cube face out of an equirectangular image.

    The mapping is the viewer's own, so that a tiled panorama and the equirect
    it came from show the same thing in the same place:

        theta = atan2(x, -z)        yaw, zero at the centre column
        phi   = acos(y / r)         angle from straight up, zero at the top row

    Rendered in horizontal strips to bound the working set; see
    FACE_STRIP_ROWS.
    """
    h, w = equirect.shape[:2]
    out = np.empty((size, size, equirect.shape[2]), dtype=equirect.dtype)

    for y0 in range(0, size, strip_rows):
        y1 = min(y0 + strip_rows, size)
        x, y, z = _strip_directions(face, size, y0, y1)

        r = np.sqrt(x * x + y * y + z * z)
        theta = np.arctan2(x, -z)
        phi = np.arccos(np.clip(y / r, -1.0, 1.0))
        del x, z, r

        map_x = ((0.5 + 0.5 * theta / math.pi) * w).astype(np.float32)
        map_y = ((phi / math.pi) * h).astype(np.float32)
        del theta, phi, y

        # BORDER_WRAP horizontally is what makes the seam invisible on the faces
        # that straddle it: a panorama is cyclic, so sampling past the right edge
        # must come back in on the left.
        out[y0:y1] = cv2.remap(equirect, map_x, map_y, cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_WRAP)
    return out


# The smallest level, which the viewer can draw immediately while the sharp
# faces are still arriving. Six small JPEGs.
BASE_SIZE = 256


def levels_for(face_size):
    """The resolution pyramid, in the shape the viewer's CubeGeometry wants.

    Halving from the full face down to BASE_SIZE, smallest first, one tile per
    face at every level.
    """
    sizes = []
    s = face_size
    while s >= BASE_SIZE:
        sizes.append(s)
        s //= 2
    sizes.reverse()
    return [{"tileSize": s, "size": s} for s in sizes]


def cut_tiles(equirect, face_size=None):
    """Everything the viewer needs, as (z, face, x, y, image) tuples.

    x and y are always 0 - one tile per face - but they stay in the signature
    and in the storage key because that is the shape the viewer's URL template
    expects, and because a future subdivided pyramid would fill them in.

    Yields rather than returning a list: holding a whole pyramid in memory
    alongside the panorama is pointless when the caller uploads one at a time.
    """
    h, w = equirect.shape[:2]
    if face_size is None:
        face_size = face_size_for(w)
    levels = levels_for(face_size)

    for face in FACES:
        full = cube_face(equirect, face, face_size)
        for z, level in enumerate(levels):
            size = level["size"]
            yield z, face, 0, 0, (full if size == face_size else cv2.resize(
                full, (size, size), interpolation=cv2.INTER_AREA))
        del full

    log.info("[orbit-worker] cut %d cube faces into %d levels (face %dpx)",
             len(FACES), len(levels), face_size)
