"""Feature-matched stitching that still knows where the sphere is.

This is the path taken when a capture has no usable camera rotations: photos
uploaded from a gallery, or a phone that would not give up its sensor.

`cv2.Stitcher` already does this, and ops/stitch.py still calls it as a last
resort. The trouble with it is not the quality of the stitch - it is that it
hands back a bare image. It will not say what focal length it warped with, so
nothing downstream can tell whether the result is a full turn or a third of
one, and the finishing stage then has no choice but to assume. Assuming is what
produced panoramas stretched around a whole sphere.

Running the same pipeline out of `cv2.detail` fixes that: the camera parameters
stay in our hands, so the focal length - and therefore the exact circumference
and horizon row - come out with the picture. Two things improve on the way past:

  * BundleAdjusterRay refines ROTATIONS only. That is precisely the motion this
    app asks for, someone pivoting on the spot, and constraining the solve to it
    is more stable than letting the general solver wander.

  * Wave correction levels the horizon. A handheld turn drifts in roll, and
    without this the finished 360 rises and falls as you look around it.

It shares the warping, exposure and seam work with pose_stitch rather than
growing a second copy: past the point where the rotations are known, the two
paths are doing the identical job.
"""
import gc
import logging
import math

import cv2
import numpy as np

from config import settings
from ops.pose_stitch import (
    DEFAULT_HFOV_DEG,
    SphereGeometry,
    _blend,
    intrinsics,
)

log = logging.getLogger("orbit-worker")

# Below this, a pair of photos is not considered to overlap at all. This is
# OpenCV's own default and it is deliberately forgiving; the connected-component
# step afterwards is what actually decides which photos belong together.
MATCH_CONF = 0.3

# How sure the bundle adjuster must be about a photo before it keeps it.
BUNDLE_CONF = 1.0


def _find_features(images):
    """SIFT where it exists, ORB otherwise.

    SIFT is much the better detector for this: it survives the scale and
    exposure changes between handheld shots, where ORB starts dropping pairs.
    It has been freely usable since its patent expired and ships in the default
    OpenCV build, but the fallback costs three lines and means a slimmed-down
    build still works.
    """
    try:
        finder = cv2.SIFT_create()
    except Exception:
        finder = cv2.ORB_create(nfeatures=2000)
    return [cv2.detail.computeImageFeatures2(finder, img) for img in images]


def _match(features):
    matcher = cv2.detail.BestOf2NearestMatcher_create(False, MATCH_CONF)
    pairwise = matcher.apply2(features)
    matcher.collectGarbage()
    return pairwise


def _estimate_cameras(features, pairwise):
    """Solve for one rotation and focal length per photo.

    Returns (cameras, reason). cameras is None when the solve failed.
    """
    estimator = cv2.detail_HomographyBasedEstimator()
    ok, cameras = estimator.apply(features, pairwise, None)
    if not ok or not cameras:
        return None, ("The photos overlap, but their angles could not be worked "
                      "out. Retake them standing still, overlapping each shot "
                      "with the last by about a third.")

    for cam in cameras:
        cam.R = cam.R.astype(np.float32)

    adjuster = cv2.detail_BundleAdjusterRay()
    adjuster.setConfThresh(BUNDLE_CONF)
    # Refine focal length and both principal-point offsets, which is the mask
    # OpenCV's own stitcher uses. The aspect ratio is left alone: phone pixels
    # are square, and freeing it only gives the solver a way to go wrong.
    mask = np.zeros((3, 3), np.uint8)
    mask[0, 0] = mask[0, 1] = mask[0, 2] = mask[1, 1] = mask[1, 2] = 1
    adjuster.setRefinementMask(mask)

    ok, cameras = adjuster.apply(features, pairwise, cameras)
    if not ok or not cameras:
        return None, ("The camera angles could not be reconciled into one sphere "
                      "- try keeping the phone level and at the same height for "
                      "every shot.")
    return cameras, None


def _level_horizon(cameras):
    """Wave correction: undo the roll drift of a handheld turn.

    Purely cosmetic if it fails, so a failure is swallowed.
    """
    try:
        rmats = [np.copy(cam.R) for cam in cameras]
        rmats = cv2.detail.waveCorrect(rmats, cv2.detail.WAVE_CORRECT_HORIZ)
        for cam, R in zip(cameras, rmats):
            cam.R = R
        return True
    except Exception as e:
        log.debug("[orbit-worker] wave correction skipped: %s", e)
        return False


def _biggest_overlapping_group(images):
    """Drop photos that do not connect to the main group, and return the rest.

    Returns (images, features, pairwise, kept_indices, reason).
    """
    features = _find_features(images)
    pairwise = _match(features)

    try:
        keep = list(np.array(cv2.detail.leaveBiggestComponent(
            features, pairwise, MATCH_CONF)).ravel())
    except Exception as e:
        log.debug("[orbit-worker] component search skipped: %s", e)
        keep = list(range(len(images)))

    if len(keep) < 2:
        return None, None, None, keep, (
            "None of these photos overlap each other, so they cannot be joined "
            "into a 360. Take them from one spot in a single continuous turn, "
            "with each photo overlapping the last by about a third.")

    if len(keep) < len(images):
        # leaveBiggestComponent tells us WHICH photos to keep but hands back the
        # matches for all of them, and the pairwise list is indexed by the old
        # numbering. Re-running on the subset is far less error-prone than
        # trying to re-index it, and only happens when photos were dropped.
        log.info("[orbit-worker] %d of %d photos form the largest overlapping "
                 "group; stitching those", len(keep), len(images))
        images = [images[i] for i in keep]
        features = _find_features(images)
        pairwise = _match(features)

    return images, features, pairwise, keep, None


def stitch_with_features(images, hfov_deg=DEFAULT_HFOV_DEG):
    """Match, solve, warp and blend - keeping the geometry.

    Returns (ok, panorama_or_None, reason_or_None, geometry_or_None,
             kept_indices). Never raises.
    """
    if len(images) < 2:
        return False, None, "Need at least 2 processed photos to attempt a stitch.", None, []

    try:
        # Shrink first, exactly as the pose path does, so every buffer after
        # this is sized off a sphere we can afford. Feature finding is also the
        # second-largest allocation in the worker, so this pays twice.
        h, w = images[0].shape[:2]
        _, full_focal = intrinsics(w, h, hfov_deg)
        scale = min(1.0, settings.pose_circumference_px / (2 * math.pi * full_focal))
        if scale < 1.0:
            tw, th = max(1, int(w * scale)), max(1, int(h * scale))
            log.info("[orbit-worker] scaling sources by %.2f to %dx%d before matching",
                     scale, tw, th)
            images = [cv2.resize(im, (tw, th), interpolation=cv2.INTER_AREA)
                      if im.shape[:2] != (th, tw) else im for im in images]

        images, features, pairwise, kept, reason = _biggest_overlapping_group(images)
        if reason:
            return False, None, reason, None, kept

        cameras, reason = _estimate_cameras(features, pairwise)
        if reason:
            return False, None, reason, None, kept
        _level_horizon(cameras)

        # The median is the robust choice: one badly-solved photo can come back
        # with a wild focal length, and a mean would drag the whole sphere with
        # it.
        focal = float(np.median([cam.focal for cam in cameras]))
        if not np.isfinite(focal) or focal <= 1:
            return False, None, ("The photos did not give a usable camera angle."), None, kept

        # Hold the sphere to the same budget the pose path uses.
        warp_scale = min(1.0, settings.pose_circumference_px / (2 * math.pi * focal))
        sphere_focal = focal * warp_scale
        warper = cv2.PyRotationWarper("spherical", sphere_focal)

        warped, masks, corners = [], [], []
        running_px = 0
        for img, cam in zip(images, cameras):
            K = cam.K().astype(np.float32)
            if warp_scale != 1.0:
                K[0, 0] *= warp_scale
                K[1, 1] *= warp_scale
                K[0, 2] *= warp_scale
                K[1, 2] *= warp_scale
                img = cv2.resize(img, (max(1, int(img.shape[1] * warp_scale)),
                                       max(1, int(img.shape[0] * warp_scale))),
                                 interpolation=cv2.INTER_AREA)
            R = cam.R.astype(np.float32)

            corner, wimg = warper.warp(img, K, R, cv2.INTER_LINEAR, cv2.BORDER_REFLECT)
            solid = np.full(img.shape[:2], 255, dtype=np.uint8)
            _, wmask = warper.warp(solid, K, R, cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)

            warped.append(wimg)
            masks.append(wmask)
            corners.append(corner)

            running_px += wimg.shape[0] * wimg.shape[1]
            if running_px > settings.pose_tile_budget_px:
                log.warning("[orbit-worker] warped tiles reached %.0f Mpx after %d "
                            "photos, over budget; stopping here",
                            running_px / 1e6, len(warped))
                del warped[-1], masks[-1], corners[-1]
                del wimg, wmask
                gc.collect()
                break

        if len(warped) < 2:
            return False, None, ("These photos are too large to place onto a sphere. "
                                 "Try again with fewer or smaller photos."), None, kept

        pano, _, y0 = _blend(warped, masks, corners)
        if pano is None:
            return False, None, "The photos could not be combined onto the sphere.", None, kept

        geom = SphereGeometry(circumference_px=2.0 * math.pi * sphere_focal,
                              equator_y=sphere_focal * math.pi / 2.0 - y0)
        log.info("[orbit-worker] feature stitch placed %d of %d photos; one turn "
                 "is %.0f px, horizon at row %.0f",
                 len(warped), len(kept), geom.circumference_px, geom.equator_y)
        return True, pano, None, geom, kept

    except MemoryError:
        log.warning("[orbit-worker] feature stitch ran out of memory")
        return False, None, "These photos are too large to stitch together.", None, []
    except cv2.error as e:
        log.warning("[orbit-worker] feature stitch cv2 error: %s", e)
        return False, None, "The stitching engine hit an internal error on these photos.", None, []
    except Exception as e:
        log.warning("[orbit-worker] feature stitch %s: %s", type(e).__name__, e)
        return False, None, "Something went wrong stitching these photos.", None, []
