"""Stitching from KNOWN camera rotations instead of guessing them.

The ordinary stitcher works backwards: it matches features between photos and
solves for where each camera must have been. That fails whenever two photos
share no recognisable detail - a blank wall, a plain sky, a dim corridor - and
when it fails the photo is silently dropped.

If the phone recorded its rotation when the shutter fired, none of that guessing
is necessary. Each photo has a known direction, so it can be projected straight
onto the sphere. A featureless wall places just as reliably as a bookshelf.

This is the approach Street View capture uses, and it is why it copes with
surfaces that defeat pure feature matching.
"""
import logging
import gc
import math

import cv2
import numpy as np

from config import settings

log = logging.getLogger("orbit-worker")

# Typical rear-camera horizontal field of view. Phones vary (60-70 degrees).
DEFAULT_HFOV_DEG = 65.0

# Widest sphere we are willing to build, in pixels around the equator.
#
# This is the single most important limit in the file. The warp canvas is
# 2*pi*focal wide, and focal grows with the source photo's width, so full-size
# phone photos produce an enormous canvas: a 1600px-wide portrait shot gives a
# 7890px circumference and a 97 degree vertical span, and near the poles a
# spherical warp stretches without bound. Multi-band blending six of those
# allocated 19GB and got the worker killed by the kernel.
#
# The finished panorama is capped at 4096 wide anyway, so warping any larger
# throws the extra pixels away regardless.
MAX_CIRCUMFERENCE_PX = 4096

# Hard ceiling on the composite canvas. Anything past this means the geometry
# is wrong, not that the photo is detailed.
MAX_CANVAS_PX = 40_000_000

# Combined size of all warped tiles held in memory at once. Photos aimed at the
# sky or the floor warp toward a pole, where a spherical projection stretches
# without bound, so a handful of pole shots can dwarf the whole horizon ring.
MAX_TILE_PIXELS = 120_000_000


def quaternion_to_matrix(x, y, z, w):
    """Quaternion [x,y,z,w] -> 3x3 rotation matrix (device frame -> world)."""
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n == 0:
        return np.eye(3, dtype=np.float32)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ], dtype=np.float32)


# The phone reports rotation in the DEVICE frame: +X right, +Y up, +Z out of the
# screen towards the user, so the rear camera looks along -Z.
# OpenCV's camera frame is +X right, +Y DOWN, +Z FORWARD.
# This flips the two axes that disagree.
_DEVICE_TO_CV = np.diag([1.0, -1.0, -1.0]).astype(np.float32)

# One more frame to reconcile. The phone's world frame is gravity-aligned with
# +Z UP. OpenCV's spherical warper spins its sphere around the world +Y axis,
# so its vertical axis is Y, pointing DOWN.
#
# Without this the photos land on the poles, where a sphere stretches without
# limit: a single 65-degree view smears across the entire 360 circumference and
# the canvas explodes. With it, each view occupies the narrow slice it should.
# The warper's vertical axis points DOWN, and the sensor's +Z points UP, so the
# sign here decides whether the sky ends up at the top of the panorama or the
# bottom. Getting it backwards produced a vertically mirrored 360 - photos of
# the ceiling appeared underfoot.
#   X_warp = X_sensor,  Y_warp = +Z_sensor,  Z_warp = -Y_sensor
_SENSOR_TO_WARPER = np.array([[1, 0, 0],
                              [0, 0, 1],
                              [0, -1, 0]], dtype=np.float32)


# Half a turn about the sphere's own axis, applied last.
#
# Without it the reference direction - the way the user was facing for their
# first photo - lands exactly on the panorama's wrap seam, so that photo is
# sliced in half and shows up at BOTH edges of the finished 360. Rotating the
# whole world half a turn puts the starting view in the middle instead, where
# it belongs.
_HALF_TURN = np.array([[-1, 0, 0],
                       [0, 1, 0],
                       [0, 0, -1]], dtype=np.float32)


def camera_rotation(quat):
    """Device quaternion -> the world-to-camera matrix OpenCV's warper wants."""
    r_world_from_device = quaternion_to_matrix(*quat)
    m = _DEVICE_TO_CV @ r_world_from_device.T @ _SENSOR_TO_WARPER.T
    return (_HALF_TURN @ m).astype(np.float32)


def intrinsics(width, height, hfov_deg=DEFAULT_HFOV_DEG):
    """Pinhole camera matrix and focal length in pixels."""
    f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0, width / 2.0],
                     [0, f, height / 2.0],
                     [0, 0, 1]], dtype=np.float32), f


def stitch_with_poses(images, quats, hfov_deg=DEFAULT_HFOV_DEG):
    """Project photos onto a sphere using their recorded rotations.

    images: list of BGR arrays.
    quats:  list of [x,y,z,w] or None, one per image.

    Returns (ok, panorama_or_None, reason_or_None). Never raises.
    """
    usable = [i for i, q in enumerate(quats) if q is not None]
    if len(usable) < 2:
        return False, None, "Not enough photos carry camera-rotation data."

    try:
        h, w = images[usable[0]].shape[:2]

        # Shrink the sources so the sphere stays within budget. Done once, up
        # front, because every later buffer is sized off this.
        _, full_focal = intrinsics(w, h, hfov_deg)
        scale = min(1.0, settings.pose_circumference_px / (2 * math.pi * full_focal))
        if scale < 1.0:
            w, h = max(1, int(w * scale)), max(1, int(h * scale))
            log.info("[orbit-worker] scaling sources by %.2f to %dx%d "
                     "so the sphere stays within %d px around",
                     scale, w, h, settings.pose_circumference_px)

        K, focal = intrinsics(w, h, hfov_deg)
        warper = cv2.PyRotationWarper("spherical", focal)

        warped, masks, corners = [], [], []
        running_px = 0
        for i in usable:
            img = images[i]
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            # The warper lays each photo down rotated half a turn about its own
            # optical axis. That leaves the photo's CENTRE in the right place -
            # which is why measuring centres never caught it - while the
            # content inside is upside down and back to front. Rotating the
            # source by the same amount cancels it exactly.
            img = cv2.rotate(img, cv2.ROTATE_180)
            R = camera_rotation(quats[i])

            corner, wimg = warper.warp(img, K, R, cv2.INTER_LINEAR, cv2.BORDER_REFLECT)
            solid = np.full((h, w), 255, dtype=np.uint8)
            _, wmask = warper.warp(solid, K, R, cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)

            warped.append(wimg)
            masks.append(wmask)
            corners.append(corner)

            # Check as we go, not after the loop. Measuring the finished list
            # only reports how much memory was already taken - on a small
            # instance the process is killed partway through the warping and the
            # guard never runs at all. A photo aimed at the sky or the floor
            # warps toward a pole, where a spherical projection stretches
            # without bound, so a single shot can blow the budget on its own.
            running_px += wimg.shape[0] * wimg.shape[1]
            if running_px > settings.pose_tile_budget_px:
                log.warning("[orbit-worker] warped tiles reached %.0f Mpx after %d of "
                            "%d photos, over the %.0f Mpx budget; stopping here",
                            running_px / 1e6, len(warped), len(usable),
                            settings.pose_tile_budget_px / 1e6)
                del warped[-1], masks[-1], corners[-1]
                running_px -= wimg.shape[0] * wimg.shape[1]
                del wimg, wmask
                gc.collect()
                break

        if len(warped) < 2:
            return False, None, ("These photos are too large to place onto a sphere. "
                                 "Try again with fewer or smaller photos.")

        pano = _blend(warped, masks, corners)
        if pano is None:
            return False, None, "The photos could not be combined onto the sphere."

        log.info("[orbit-worker] pose stitch placed %d photo(s) from recorded rotations",
                 len(usable))
        return True, pano, None

    except MemoryError:
        log.warning("[orbit-worker] pose stitch ran out of memory")
        return False, None, "These photos are too large to place onto a sphere."
    except cv2.error as e:
        log.warning("[orbit-worker] pose stitch cv2 error: %s", e)
        return False, None, "The camera rotations recorded with these photos could not be used."
    except Exception as e:
        log.warning("[orbit-worker] pose stitch %s: %s", type(e).__name__, e)
        return False, None, "Something went wrong placing the photos onto the sphere."


def _compensate_exposure(warped, masks, corners):
    """Even out brightness between shots.

    Phones re-meter for every photo, so the sunlit side and the shaded side
    arrive at different exposures and every seam shows as a brightness step.

    One gain per photo, not per block. GAIN_BLOCKS builds a grid of gain maps
    across every tile at full resolution, which on a 13-photo sphere - where
    the shots aimed at the ceiling and floor warp into very large tiles -
    exhausted memory and got the worker killed. A single gain is also the right
    correction for the problem we actually have: whole-frame metering drift,
    not vignetting.

    Applied in place; failure here is cosmetic, so it is swallowed.
    """
    try:
        comp = cv2.detail.ExposureCompensator_createDefault(
            cv2.detail.ExposureCompensator_GAIN)
        comp.feed(corners, warped, masks)
        for i in range(len(warped)):
            comp.apply(i, corners[i], warped[i], masks[i])
    except Exception as e:
        log.debug("[orbit-worker] exposure compensation skipped: %s", e)


# Seam finding runs on tiny copies. OpenCV's own stitcher does the same, at
# about a tenth of a megapixel: the cut line only needs to be roughly right,
# and the finder is expensive enough that running it at full size is what
# pushed a 13-photo capture past 8GB.
# Resolution the seam is searched at, per photo. The seam mask is found here
# and scaled back up, so this number decides how finely the cut can follow real
# edges in the scene. At 0.1 the cut was found at roughly a fifth scale and
# arrived back as a visible staircase along building edges.
SEAM_WORK_MEGAPIX = settings.seam_work_megapix


def _find_seams(warped, masks, corners):
    """Choose where each pair of overlapping photos should hand over.

    Without this, every overlap is a wide cross-fade of two photos. Anything
    that does not line up perfectly - and handheld photos never do - is
    averaged into a soft double image, which reads as a blurred band wherever
    two photos meet. A seam finder instead picks a cut through the overlap
    where the two photos agree most closely, so the join is a clean handover.

    Masks are edited in place. Failure is non-fatal and simply leaves the
    plain cross-fade behaviour.
    """
    try:
        total_px = sum(im.shape[0] * im.shape[1] for im in warped)
        scale = min(1.0, math.sqrt(SEAM_WORK_MEGAPIX * 1e6 * len(warped) / max(1, total_px)))

        small_imgs, small_masks, small_corners = [], [], []
        for im, mk, c in zip(warped, masks, corners):
            sw = max(8, int(im.shape[1] * scale))
            sh = max(8, int(im.shape[0] * scale))
            small_imgs.append(cv2.resize(im, (sw, sh), interpolation=cv2.INTER_AREA)
                              .astype(np.float32))
            small_masks.append(cv2.UMat(
                cv2.resize(mk, (sw, sh), interpolation=cv2.INTER_NEAREST)))
            small_corners.append((int(c[0] * scale), int(c[1] * scale)))

        cv2.detail_DpSeamFinder("COLOR_GRAD").find(
            small_imgs, small_corners, small_masks)

        # Scale each seam mask back up and intersect with the real coverage, so
        # the seam can only ever remove pixels, never invent them.
        #
        # The upscaled mask is kept soft rather than thresholded back to black
        # and white. Rounding it to a hard edge re-quantised the boundary onto
        # the low-resolution grid it was found on, which is what put a visible
        # staircase along the join. MultiBandBlender normalises by total weight,
        # so a soft mask simply hands over gradually across those few pixels.
        for i, um in enumerate(small_masks):
            grown = cv2.resize(um.get(), (masks[i].shape[1], masks[i].shape[0]),
                               interpolation=cv2.INTER_LINEAR)
            # Blur by roughly the size of one low-resolution pixel, so the
            # handover spans the uncertainty in where the seam actually is.
            k = max(1, int(round(1.0 / max(scale, 1e-6))))
            if k > 1:
                grown = cv2.GaussianBlur(grown, (0, 0), sigmaX=k / 2.0)
            masks[i] = np.minimum(masks[i], grown).astype(np.uint8)
        return True
    except Exception as e:
        log.debug("[orbit-worker] seam finding skipped: %s", e)
        return False


def _blend(warped, masks, corners):
    """Multi-band blend the warped images onto one canvas."""
    sizes = [(im.shape[1], im.shape[0]) for im in warped]
    x0 = min(c[0] for c in corners)
    y0 = min(c[1] for c in corners)
    x1 = max(c[0] + s[0] for c, s in zip(corners, sizes))
    y1 = max(c[1] + s[1] for c, s in zip(corners, sizes))
    if x1 <= x0 or y1 <= y0:
        return None
    # A canvas this large means the geometry is wrong, not that the photo is
    # detailed. Refuse rather than trying to allocate gigabytes.
    if (x1 - x0) * (y1 - y0) > MAX_CANVAS_PX:
        log.warning("[orbit-worker] pose stitch canvas %dx%d is implausible; refusing",
                    x1 - x0, y1 - y0)
        return None

    _compensate_exposure(warped, masks, corners)
    seamed = _find_seams(warped, masks, corners)

    blender = cv2.detail_MultiBandBlender()
    # The number of bands sets how wide the handover between two photos is, in
    # pixels, and it has to scale with the canvas: a fixed 3 gave roughly an
    # eight-pixel transition on a four-thousand-pixel panorama, far too abrupt
    # to hide a join, so every seam stayed visible as a hard line.
    #
    # This is the rule OpenCV's own Stitcher uses - a blend width of 5% of the
    # square root of the canvas area - which lands around 6 bands here.
    blend_width = math.sqrt(float((x1 - x0) * (y1 - y0))) * 0.05
    bands = int(math.ceil(math.log(max(blend_width, 2.0)) / math.log(2.0))) - 1
    bands = max(3, min(7, bands))
    if not seamed:
        # No seam was chosen, so the overlap is a plain cross-fade and wants a
        # wider one to avoid a visible edge.
        bands = min(7, bands + 1)
    log.info("[orbit-worker] blending %d photos onto %dx%d with %d bands",
             len(warped), x1 - x0, y1 - y0, bands)
    blender.setNumBands(bands)
    blender.prepare((x0, y0, x1 - x0, y1 - y0))
    for im, mask, corner in zip(warped, masks, corners):
        blender.feed(im.astype(np.int16), mask, corner)
    result, _ = blender.blend(None, None)
    return cv2.convertScaleAbs(result)
