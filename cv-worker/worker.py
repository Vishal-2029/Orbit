#!/usr/bin/env python3
"""Orbit CV worker.

Consumes Redis Stream "orbit:jobs" (consumer group "cv-workers"), processes
capture.frame.process and capture.finalize jobs, and reports results back to
the Go API via its internal HTTP callbacks. See README.md for details.
"""
import json
import logging
import signal
import sys
import time

import redis
import requests

from config import settings
from ops.align import sort_ring_by_yaw
from ops.normalize import (
    align_to_centroid,
    centroid,
    decode_with_exif_rotation,
    encode_jpeg,
    normalize_color,
    resize_to_width,
)
from ops.stitch import stitch_panorama
from ops.finish import finish_panorama
from ops.coverage import connected_groups, describe_leftovers, sphere_coverage
from ops.pose_stitch import stitch_with_poses
from ops.xmp import add_photosphere_metadata

try:
    from minio import Minio
except ImportError:  # pragma: no cover
    Minio = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("orbit-worker")
PREFIX = "[orbit-worker]"

_running = True


def _handle_sigterm(signum, frame):
    global _running
    log.info("%s received signal %s, shutting down after current job", PREFIX, signum)
    _running = False


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------

def make_redis():
    return redis.Redis(host=settings.redis_host, port=settings.redis_port, decode_responses=True)


def make_minio():
    if Minio is None:
        raise RuntimeError("minio package not installed")
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
    )


def ensure_group(rdb):
    try:
        rdb.xgroup_create(settings.stream_jobs, settings.group_workers, id="0", mkstream=True)
        log.info("%s created consumer group %s on %s", PREFIX, settings.group_workers, settings.stream_jobs)
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            pass
        else:
            raise


# --------------------------------------------------------------------------
# HTTP callbacks to the Go API
# --------------------------------------------------------------------------

def api_url(path: str) -> str:
    return f"{settings.api_base_url}{path}"


def post_json(path, body, timeout=15):
    url = api_url(path)
    r = requests.post(url, json=body, timeout=timeout)
    if r.status_code >= 400:
        log.error("%s POST %s -> %s: %s", PREFIX, url, r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json() if r.content else {}


def get_json(path, timeout=15):
    url = api_url(path)
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def report_frame_done(capture_id, frame_id, index, width, height):
    post_json(f"/api/v1/internal/captures/{capture_id}/frames/{frame_id}/done",
              {"index": index, "width": width, "height": height})


def report_frame_failed(capture_id, frame_id, index, reason):
    post_json(f"/api/v1/internal/captures/{capture_id}/frames/{frame_id}/failed",
              {"index": index, "reason": reason})


def report_finalize(capture_id, body):
    post_json(f"/api/v1/internal/captures/{capture_id}/finalize", body)


# --------------------------------------------------------------------------
# Object storage helpers
# --------------------------------------------------------------------------

def original_key(capture_id, idx):
    return f"captures/{capture_id}/original/{idx:03d}.jpg"


def processed_key(capture_id, idx):
    return f"captures/{capture_id}/processed/{idx:03d}.jpg"


def thumb_key(capture_id, idx):
    return f"captures/{capture_id}/thumb/{idx:03d}.jpg"


def panorama_key(capture_id):
    return f"captures/{capture_id}/panorama.jpg"


def get_object_bytes(mc, bucket, key):
    resp = mc.get_object(bucket, key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def put_object_bytes(mc, bucket, key, data: bytes, content_type="image/jpeg"):
    import io
    mc.put_object(bucket, key, io.BytesIO(data), length=len(data), content_type=content_type)


# --------------------------------------------------------------------------
# Job handlers
# --------------------------------------------------------------------------

class PermanentJobError(Exception):
    """Raised for errors that should be reported to the API as a plain-English
    failure rather than retried (e.g. corrupt image, bad payload)."""


def handle_frame_job(rdb, mc, job):
    payload = job["payload"]
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    capture_id = job["capture_id"]
    frame_id = payload["frame_id"]
    index = payload["index"]
    original_key_ = payload["original_key"]
    settings_ = payload.get("settings") or {}
    mode = payload.get("mode", "pano")
    target_width = settings_.get("target_width") or settings.target_width_default
    align = bool(settings_.get("align")) and mode == "spin"

    log.info("%s frame job capture=%s frame=%s index=%s", PREFIX, capture_id, frame_id, index)

    try:
        raw = get_object_bytes(mc, settings.bucket_private, original_key_)
    except Exception as e:
        raise PermanentJobError(f"Could not read the uploaded photo (index {index}): {e}") from e

    try:
        img = decode_with_exif_rotation(raw)
    except Exception as e:
        raise PermanentJobError(
            f"Photo {index} appears to be corrupted or is not a valid image."
        ) from e

    try:
        img = resize_to_width(img, target_width)
        img = normalize_color(img)
        if align:
            cx, cy = centroid(img)
            img = align_to_centroid(img, cx, cy)
        h, w = img.shape[:2]
        full_bytes = encode_jpeg(img, settings.jpeg_quality)

        thumb_img = resize_to_width(img, min(settings.thumb_width, w))
        thumb_bytes = encode_jpeg(thumb_img, settings.jpeg_quality)
    except Exception as e:
        raise PermanentJobError(f"Failed to process photo {index}: {e}") from e

    try:
        put_object_bytes(mc, settings.bucket_public, processed_key(capture_id, index), full_bytes)
        put_object_bytes(mc, settings.bucket_public, thumb_key(capture_id, index), thumb_bytes)
    except Exception as e:
        # storage failure is transient - let the retry wrapper handle it
        raise RuntimeError(f"Failed to upload processed photo {index} to storage: {e}") from e

    report_frame_done(capture_id, frame_id, index, w, h)
    log.info("%s frame done capture=%s index=%s size=%sx%s", PREFIX, capture_id, index, w, h)


def _true_north_heading(frames):
    """Compass bearing of the panorama centre, or None if we cannot know it.

    PoseHeadingDegrees must be measured clockwise from TRUE NORTH. A
    gyroscope-only sensor has no idea where north is - it only tracks change
    from wherever it started - so a heading is only reported when the reading
    came from a magnetometer-backed source. Writing a guess would send Google
    Maps to the wrong bearing, which is worse than writing nothing.
    """
    for f in frames:
        src = (f.get("orientation_source") or "").lower()
        if src in ("absolute", "deviceorientation"):
            yaw = f.get("yaw")
            if yaw is not None:
                return float(yaw)
    return None


def _quat_of(frame):
    """Pull [x,y,z,w] out of a frame record, or None if it has no rotation."""
    q = frame.get("quat")
    if not q:
        return None
    try:
        vals = [float(q["X"]), float(q["Y"]), float(q["Z"]), float(q["W"])]
    except (KeyError, TypeError, ValueError):
        return None
    # An all-zero quaternion carries no rotation and would divide by zero.
    return vals if any(abs(v) > 1e-9 for v in vals) else None


def wait_for_frames(capture_id):
    """Poll GET /api/v1/captures/{id} until processed_count + failed == frame_count,
    or until the timeout elapses. Returns the last capture dict seen."""
    deadline = time.time() + settings.finalize_timeout_seconds
    last = None
    while time.time() < deadline:
        try:
            data = get_json(f"/api/v1/captures/{capture_id}")
        except Exception as e:
            log.warning("%s finalize poll failed for capture=%s: %s", PREFIX, capture_id, e)
            time.sleep(settings.finalize_poll_interval)
            continue
        cap = data.get("capture", {})
        last = cap
        frame_count = cap.get("frame_count", 0)
        processed_count = cap.get("processed_count", 0)
        # frames endpoint tells us failed count precisely
        try:
            frames_data = get_json(f"/api/v1/captures/{capture_id}/frames")
            frames = frames_data.get("frames", [])
            failed = sum(1 for f in frames if f.get("status") == "failed")
            done = sum(1 for f in frames if f.get("status") == "done")
        except Exception as e:
            log.warning("%s could not list frames for capture=%s: %s", PREFIX, capture_id, e)
            time.sleep(settings.finalize_poll_interval)
            continue

        if frame_count and (done + failed) >= frame_count:
            return cap, frames
        time.sleep(settings.finalize_poll_interval)

    log.warning("%s finalize wait timed out for capture=%s", PREFIX, capture_id)
    try:
        frames_data = get_json(f"/api/v1/captures/{capture_id}/frames")
        frames = frames_data.get("frames", [])
    except Exception:
        frames = []
    return last or {}, frames


def handle_finalize_job(mc, job):
    capture_id = job["capture_id"]
    log.info("%s finalize job capture=%s: waiting for frames", PREFIX, capture_id)

    cap, frames = wait_for_frames(capture_id)
    mode = cap.get("mode", "pano")

    done_frames = [f for f in frames if f.get("status") == "done"]

    if mode == "spin":
        log.info("%s spin mode capture=%s -> frame renderer, no stitch attempted", PREFIX, capture_id)
        report_finalize(capture_id, {"stitched": False})
        return

    if len(done_frames) < 2:
        reason = "Too few photos processed successfully to attempt a stitch."
        log.warning("%s capture=%s: %s", PREFIX, capture_id, reason)
        report_finalize(capture_id, {"stitched": False, "failure_cause": reason})
        return

    if mode == "auto":
        # Free-upload mode: the photos carry no compass headings, so there is
        # no ring to sort. cv2.Stitcher matches features pairwise and works out
        # the arrangement itself, which is exactly what this mode relies on.
        log.info("%s auto mode capture=%s: %d photos, letting the stitcher "
                 "determine the order", PREFIX, capture_id, len(done_frames))
        ring = done_frames
    else:
        ring = sort_ring_by_yaw(done_frames)

    # Loaded images and their source frames are kept in lockstep. If a frame
    # fails to load, dropping it from one list but not the other would attach
    # every subsequent rotation to the wrong photo.
    images = []
    loaded = []
    for f in ring:
        try:
            raw = get_object_bytes(mc, settings.bucket_public, processed_key(capture_id, f["index"]))
            img = decode_bytes_to_bgr(raw)
        except Exception as e:
            log.warning("%s capture=%s: could not load processed frame %s for stitching: %s",
                        PREFIX, capture_id, f.get("index"), e)
            continue
        images.append(img)
        loaded.append(f)
    ring = loaded

    if len(images) < 2:
        reason = "Too few processed photos could be loaded to attempt a stitch."
        report_finalize(capture_id, {"stitched": False, "failure_cause": reason})
        return

    # If the phone recorded its rotation for each shot, use that geometry
    # directly instead of rediscovering it from pixels. This is the whole point
    # of storing the quaternions: a blank wall places just as well as a
    # bookshelf, because placement no longer depends on matchable detail.
    quats = [_quat_of(f) for f in ring]
    posed = sum(1 for q in quats if q is not None)

    if posed >= 2 and posed >= len(ring) * 0.8:
        log.info("%s capture=%s: %d of %d photos carry camera rotations; "
                 "stitching from known poses", PREFIX, capture_id, posed, len(ring))
        ok, pano, reason = stitch_with_poses(images, quats)
        if ok and pano is not None:
            try:
                pano = finish_panorama(pano)
            except Exception as e:
                log.warning("%s panorama clean-up failed (%s: %s); using raw",
                            PREFIX, type(e).__name__, e)
            h, w = pano.shape[:2]
            # How much of the world these photos actually saw. Anything they
            # missed shows up as a soft patch, and only more photos can fix it.
            src_h, src_w = images[0].shape[:2]
            coverage = sphere_coverage(
                [q for q in quats if q is not None],
                hfov_deg=65.0, aspect=src_h / float(src_w))
            log.info("%s capture=%s covers %.0f%% of the sphere",
                     PREFIX, capture_id, coverage * 100)

            pano_bytes = add_photosphere_metadata(
                encode_jpeg(pano, settings.jpeg_quality), w, h,
                source_count=posed, heading_deg=_true_north_heading(ring))
            try:
                put_object_bytes(mc, settings.bucket_public,
                                 panorama_key(capture_id), pano_bytes)
            except Exception as e:
                log.warning("%s could not store posed panorama: %s", PREFIX, e)
            else:
                report_finalize(capture_id, {
                    "stitched": True,
                    "panorama_key": panorama_key(capture_id),
                    "width": w, "height": h,
                    "photos_used": posed, "photos_total": len(ring),
                    "coverage_note": describe_leftovers(len(ring), posed),
                    "sphere_coverage": coverage,
                })
                log.info("%s pose stitch succeeded capture=%s size=%sx%s using %d of %d",
                         PREFIX, capture_id, w, h, posed, len(ring))
                return
        log.warning("%s capture=%s: pose stitch unusable (%s); "
                    "falling back to feature matching", PREFIX, capture_id, reason)

    # Find out which photos actually overlap each other BEFORE stitching.
    # The stitcher would otherwise quietly use the largest matching group and
    # report plain success, leaving the user with a narrow strip presented as a
    # full 360.
    total = len(images)
    try:
        groups = connected_groups(images)
    except Exception as e:
        log.warning("%s coverage check failed (%s: %s); stitching everything",
                    PREFIX, type(e).__name__, e)
        groups = [list(range(total))]

    best = groups[0] if groups else list(range(total))
    used = len(best)
    coverage_note = describe_leftovers(total, used)

    if used < 2:
        reason = (
            f"None of your {total} photos overlap each other, so they cannot be "
            "joined into a 360. Take them from one spot in a single continuous "
            "turn, with each photo overlapping the last by about a third."
        )
        log.warning("%s capture=%s: no connected group", PREFIX, capture_id)
        report_finalize(capture_id, {
            "stitched": False, "failure_cause": reason,
            "photos_used": used, "photos_total": total,
            "coverage_note": reason,
        })
        return

    if used < total:
        log.warning("%s capture=%s: only %d of %d photos are connected; "
                    "stitching that group only", PREFIX, capture_id, used, total)

    stitch_images = [images[i] for i in best]
    ok, pano, reason = stitch_panorama(stitch_images)
    if ok and pano is not None:
        # The raw stitcher result is ragged and black-padded, and its two ends
        # do not meet. Clean it up before it ever becomes a sphere texture.
        try:
            pano = finish_panorama(pano)
        except Exception as e:
            # A cosmetic step must never cost us a successful stitch.
            log.warning("[orbit-worker] panorama clean-up failed (%s: %s); "
                        "using the raw stitch", type(e).__name__, e)
    if not ok:
        log.warning("%s stitch failed for capture=%s: %s", PREFIX, capture_id, reason)
        report_finalize(capture_id, {
            "stitched": False, "failure_cause": reason,
            "photos_used": used, "photos_total": total,
            "coverage_note": coverage_note,
        })
        return

    h, w = pano.shape[:2]
    # Mark it as a photo sphere so Google Maps, Google Photos and any
    # photo-sphere viewer open it as a draggable 360 rather than a wide photo.
    pano_bytes = add_photosphere_metadata(
        encode_jpeg(pano, settings.jpeg_quality), w, h,
        source_count=used, heading_deg=_true_north_heading(ring))
    try:
        put_object_bytes(mc, settings.bucket_public, panorama_key(capture_id), pano_bytes)
    except Exception as e:
        reason = "The panorama was stitched but could not be saved to storage."
        log.error("%s capture=%s: %s (%s)", PREFIX, capture_id, reason, e)
        report_finalize(capture_id, {"stitched": False, "failure_cause": reason})
        return

    report_finalize(capture_id, {
        "stitched": True,
        "panorama_key": panorama_key(capture_id),
        "width": w,
        "height": h,
        "photos_used": used,
        "photos_total": total,
        "coverage_note": coverage_note,
    })
    log.info("%s stitch succeeded capture=%s size=%sx%s using %d of %d photos",
             PREFIX, capture_id, w, h, used, total)


def decode_bytes_to_bgr(raw: bytes):
    import cv2
    import numpy as np
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode processed frame bytes")
    return img


# --------------------------------------------------------------------------
# Retry / DLQ wrapper
# --------------------------------------------------------------------------

def send_to_dlq(rdb, job_raw, error_reason):
    rdb.xadd(settings.stream_dlq, {
        "job": job_raw,
        "error": error_reason,
        "ts": str(int(time.time())),
    })
    log.error("%s job sent to DLQ: %s", PREFIX, error_reason)


def process_job_with_retry(rdb, mc, job):
    """job: dict with id/type/capture_id/payload(raw json string).
    Retries up to settings.max_attempts with exponential backoff.
    Returns True if the job should be XACKed (succeeded, or permanently
    failed but user-facing failure already reported), False if it should
    remain unacked (transient issue exhausted retries and callback failed too -
    in practice this function always resolves to a terminal state)."""
    job_type = job["type"]
    capture_id = job.get("capture_id", "")
    last_err = None

    for attempt in range(1, settings.max_attempts + 1):
        try:
            if job_type == "capture.frame.process":
                handle_frame_job(rdb, mc, job)
            elif job_type == "capture.finalize":
                handle_finalize_job(mc, job)
            else:
                log.warning("%s unknown job type %s, dropping", PREFIX, job_type)
                return True
            return True
        except PermanentJobError as e:
            # Not worth retrying - report failure straight away for frame jobs.
            last_err = str(e)
            log.error("%s permanent error on attempt %s/%s capture=%s: %s",
                      PREFIX, attempt, settings.max_attempts, capture_id, last_err)
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning("%s transient error on attempt %s/%s capture=%s type=%s: %s",
                        PREFIX, attempt, settings.max_attempts, capture_id, job_type, last_err)
            if attempt < settings.max_attempts:
                backoff = settings.backoff_base_seconds * (2 ** (attempt - 1))
                time.sleep(backoff)

    # All attempts exhausted (or permanent error). Report to API + DLQ.
    plain_reason = _plain_english(last_err)
    if job_type == "capture.frame.process":
        try:
            payload = job["payload"]
            if isinstance(payload, (str, bytes)):
                payload = json.loads(payload)
            report_frame_failed(capture_id, payload.get("frame_id", ""), payload.get("index", -1), plain_reason)
        except Exception as e:
            log.error("%s could not report frame failure to API: %s", PREFIX, e)
    elif job_type == "capture.finalize":
        try:
            report_finalize(capture_id, {"stitched": False, "failure_cause": plain_reason})
        except Exception as e:
            log.error("%s could not report finalize failure to API: %s", PREFIX, e)

    send_to_dlq(rdb, job.get("_raw", json.dumps(job)), plain_reason)
    return True  # ack: we've done everything we can, don't loop forever


def _plain_english(err):
    if not err:
        return "An unknown error occurred while processing this photo."
    # Strip Python exception-class noise for anything that already reads plainly.
    if ": " in err and err.split(":", 1)[0].isidentifier():
        return err.split(":", 1)[1].strip() or err
    return err


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def parse_stream_entry(fields):
    job_raw = fields.get("job")
    job = json.loads(job_raw)
    job["_raw"] = job_raw
    return job


def reclaim_abandoned(rdb, mc):
    """Take over jobs left half-finished by a worker that died.

    A job is acknowledged only after it completes. If the process is killed
    part-way through - the kernel's OOM killer has done exactly this - the entry
    stays pending forever, because XREADGROUP with ">" only ever returns
    messages nobody has seen. Without this the capture sits at "processing" for
    good.

    Anything idle longer than reclaim_idle_ms is reassigned to this worker.
    A job that has already been handed out too many times is poison: it is
    dropped to the dead-letter stream instead of being allowed to take down
    another worker.
    """
    try:
        _, messages, _ = rdb.xautoclaim(
            settings.stream_jobs, settings.group_workers, settings.consumer_name,
            min_idle_time=settings.reclaim_idle_ms, start_id="0-0", count=10,
        )
    except redis.exceptions.ResponseError as e:
        log.debug("%s xautoclaim unavailable: %s", PREFIX, e)
        return
    except redis.exceptions.ConnectionError as e:
        log.warning("%s could not reclaim abandoned jobs: %s", PREFIX, e)
        return

    if not messages:
        return

    # How many times each of these has been delivered already.
    delivered = {}
    try:
        for row in rdb.xpending_range(settings.stream_jobs, settings.group_workers,
                                      min="-", max="+", count=100):
            delivered[row["message_id"]] = row["times_delivered"]
    except Exception as e:
        log.debug("%s could not read delivery counts: %s", PREFIX, e)

    for entry_id, fields in messages:
        if fields is None:
            rdb.xack(settings.stream_jobs, settings.group_workers, entry_id)
            continue

        count = delivered.get(entry_id, 1)
        try:
            job = parse_stream_entry(fields)
        except Exception as e:
            log.error("%s reclaimed job %s is unparseable (%s); dropping", PREFIX, entry_id, e)
            rdb.xack(settings.stream_jobs, settings.group_workers, entry_id)
            continue

        capture_id = job.get("capture_id")

        if count > settings.max_deliveries:
            reason = (
                "This 360 could not be built: the job stopped part-way through "
                "several times, usually because the photos were too large to "
                "process. Try again with fewer or smaller photos."
            )
            log.error("%s job %s capture=%s delivered %d times; giving up",
                      PREFIX, entry_id, capture_id, count)
            try:
                send_to_dlq(rdb, job.get("_raw", json.dumps(job)),
                            "worker died part-way through %d times" % count)
            except Exception as e:
                log.error("%s could not DLQ %s: %s", PREFIX, entry_id, e)
            try:
                report_finalize(capture_id, {"stitched": False, "failure_cause": reason})
            except Exception as e:
                log.error("%s could not report failure for %s: %s", PREFIX, capture_id, e)
            rdb.xack(settings.stream_jobs, settings.group_workers, entry_id)
            continue

        log.warning("%s reclaiming abandoned job %s type=%s capture=%s (delivery %d)",
                    PREFIX, entry_id, job.get("type"), capture_id, count)
        try:
            process_job_with_retry(rdb, mc, job)
        finally:
            rdb.xack(settings.stream_jobs, settings.group_workers, entry_id)


def main():
    log.info("%s starting, redis=%s:%s minio=%s api=%s",
              PREFIX, settings.redis_host, settings.redis_port,
              settings.minio_endpoint, settings.api_base_url)
    rdb = make_redis()
    mc = make_minio()

    ensure_group(rdb)

    # Sweep once at startup: if this worker is replacing one that just died,
    # its unfinished job is waiting right now.
    reclaim_abandoned(rdb, mc)
    next_reclaim = time.time() + settings.reclaim_every_seconds

    while _running:
        if time.time() >= next_reclaim:
            reclaim_abandoned(rdb, mc)
            next_reclaim = time.time() + settings.reclaim_every_seconds

        try:
            resp = rdb.xreadgroup(
                settings.group_workers, settings.consumer_name,
                {settings.stream_jobs: ">"}, count=1, block=settings.block_ms,
            )
        except redis.exceptions.ConnectionError as e:
            log.error("%s redis connection error: %s, retrying in 2s", PREFIX, e)
            time.sleep(2)
            continue

        if not resp:
            continue

        for stream_name, entries in resp:
            for entry_id, fields in entries:
                try:
                    job = parse_stream_entry(fields)
                except Exception as e:
                    log.error("%s could not parse job entry %s: %s, acking to drop it", PREFIX, entry_id, e)
                    rdb.xack(settings.stream_jobs, settings.group_workers, entry_id)
                    continue

                log.info("%s received job id=%s type=%s capture=%s",
                          PREFIX, entry_id, job.get("type"), job.get("capture_id"))
                try:
                    process_job_with_retry(rdb, mc, job)
                finally:
                    rdb.xack(settings.stream_jobs, settings.group_workers, entry_id)
                    log.info("%s acked job id=%s", PREFIX, entry_id)

    log.info("%s stopped", PREFIX)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
