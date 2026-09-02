"""Panorama stitching with plain-English failure reporting.

cv2.Stitcher status codes:
  OK = 0
  ERR_NEED_MORE_IMGS = 1
  ERR_HOMOGRAPHY_EST_FAIL = 2
  ERR_CAMERA_PARAMS_ADJUST_FAIL = 3
"""
import logging

import cv2

log = logging.getLogger("orbit-worker")

STATUS_MESSAGES = {
    1: "Not enough overlap between photos - try shooting 12 photos instead of 8, "
       "overlapping each one by about a third with the last.",
    2: "The photos don't line up - some may be blurry, too dark, or shot while moving. "
       "Retake them standing still with steady, well-lit shots.",
    3: "The camera angles couldn't be reconciled into one sphere - try keeping the "
       "phone level and at the same height for every shot.",
}


def _status_message(status: int) -> str:
    return STATUS_MESSAGES.get(
        status, f"The photos could not be stitched together (stitcher status {status})."
    )


def stitch_panorama(images):
    """images: list of BGR ndarrays, already sorted by yaw.

    Returns (success: bool, result_img_or_None, reason_or_None).
    Tries SCANS mode first (better for ring/rotation captures with
    similar camera positions), falls back to PANORAMA mode.
    Never raises - all OpenCV/runtime errors are converted to a plain
    English failure reason so the caller can report a graceful fallback.
    """
    if len(images) < 2:
        return False, None, "Need at least 2 processed photos to attempt a stitch."

    modes = []
    if hasattr(cv2, "Stitcher_SCANS"):
        modes.append(("SCANS", cv2.Stitcher_SCANS))
    if hasattr(cv2, "Stitcher_PANORAMA"):
        modes.append(("PANORAMA", cv2.Stitcher_PANORAMA))

    last_reason = "Stitching failed for an unknown reason."
    for name, mode in modes:
        try:
            stitcher = cv2.Stitcher_create(mode)
            status, pano = stitcher.stitch(images)
        except cv2.error as e:
            log.warning("[orbit-worker] stitcher mode=%s raised cv2.error: %s", name, e)
            last_reason = "The stitching engine hit an internal error processing these photos."
            continue
        except Exception as e:  # never let a stitch attempt crash the worker
            log.warning("[orbit-worker] stitcher mode=%s raised %s: %s", name, type(e).__name__, e)
            last_reason = "The stitching engine hit an unexpected error processing these photos."
            continue

        if status == 0 and pano is not None:
            log.info("[orbit-worker] stitch succeeded using mode=%s", name)
            return True, pano, None

        last_reason = _status_message(status)
        log.warning("[orbit-worker] stitcher mode=%s failed status=%s (%s)", name, status, last_reason)

    return False, None, last_reason
