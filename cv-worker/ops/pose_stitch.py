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
import math

import cv2
import numpy as np

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
#   X_warp = X_sensor,  Y_warp = -Z_sensor,  Z_warp = Y_sensor
_SENSOR_TO_WARPER = np.array([[1, 0, 0],
                              [0, 0, -1],
                              [0, 1, 0]], dtype=np.float32)


def camera_rotation(quat):
    """Device quaternion -> the world-to-camera matrix OpenCV's warper wants."""
    r_world_from_device = quaternion_to_matrix(*quat)
    return (_DEVICE_TO_CV @ r_world_from_device.T @ _SENSOR_TO_WARPER.T).astype(np.float32)


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
        scale = min(1.0, MAX_CIRCUMFERENCE_PX / (2 * math.pi * full_focal))
        if scale < 1.0:
            w, h = max(1, int(w * scale)), max(1, int(h * scale))
            log.info("[orbit-worker] scaling sources by %.2f to %dx%d "
                     "so the sphere stays within %d px around",
                     scale, w, h, MAX_CIRCUMFERENCE_PX)

        K, focal = intrinsics(w, h, hfov_deg)
        warper = cv2.PyRotationWarper("spherical", focal)

        warped, masks, corners = [], [], []
        for i in usable:
            img = images[i]
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            R = camera_rotation(quats[i])

            corner, wimg = warper.warp(img, K, R, cv2.INTER_LINEAR, cv2.BORDER_REFLECT)
            solid = np.full((h, w), 255, dtype=np.uint8)
            _, wmask = warper.warp(solid, K, R, cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)

            warped.append(wimg)
            masks.append(wmask)
            corners.append(corner)

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
    Applied in place; failure here is cosmetic, so it is swallowed.
    """
    try:
        comp = cv2.detail.ExposureCompensator_createDefault(
            cv2.detail.ExposureCompensator_GAIN_BLOCKS)
        comp.feed(corners, warped, masks)
        for i in range(len(warped)):
            comp.apply(i, corners[i], warped[i], masks[i])
    except Exception as e:
        log.debug("[orbit-worker] exposure compensation skipped: %s", e)


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

    blender = cv2.detail_MultiBandBlender()
    blender.setNumBands(5)
    blender.prepare((x0, y0, x1 - x0, y1 - y0))
    for im, mask, corner in zip(warped, masks, corners):
        blender.feed(im.astype(np.int16), mask, corner)
    result, _ = blender.blend(None, None)
    return cv2.convertScaleAbs(result)
