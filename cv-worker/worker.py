#!/usr/bin/env python3
"""Orbit CV worker.

Consumes Redis Stream "orbit:jobs" (consumer group "cv-workers"), processes
capture.frame.process and capture.finalize jobs, and reports results back to
the Go API via its internal HTTP callbacks. See README.md for details.
"""
import gc
import json
import math
import logging
import os
import signal
import sys
import time

import cv2
import redis
import requests

from config import settings, _cgroup_memory_limit_mb
from ops.align import sort_ring_by_yaw
from ops.budget import plan as plan_source_width
from ops.normalize import (
    align_to_centroid,
    centroid,
    decode_with_exif_rotation,
    encode_jpeg,
    normalize_color,
    resize_to_width,
)
from ops.stitch import stitch_panorama
from ops.tiles import cut_tiles, face_size_for, levels_for
from ops.feature_stitch import stitch_with_features
from ops.finish import MIN_SPHERE_COVERAGE, finish_panorama
from ops.coverage import describe_leftovers, sphere_coverage
from ops.pose_stitch import (quaternion_from_heading, quaternion_to_matrix,
                             stitch_with_poses)
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
    if settings.redis_url:
        return redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return redis.Redis(host=settings.redis_host, port=settings.redis_port, decode_responses=True)


def start_health_server():
    """Serve /health on HEALTH_PORT so port-requiring hosts keep us alive.

    Runs on a daemon thread; the job loop stays the only real work.
    """
    if not settings.health_port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status":"ok","service":"orbit-cv-worker"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep job logs readable
            pass

    server = HTTPServer(("0.0.0.0", settings.health_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("%s health server listening on :%s", PREFIX, settings.health_port)


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


def tile_key(capture_id, z, face, x, y):
    """One cube tile. Must match storage.TileKey on the Go side and the URL
    template the manifest hands the viewer - z, face, y, then x."""
    return f"captures/{capture_id}/tiles/{z}/{face}/{y}/{x}.jpg"


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


def _pose_of(frame):
    """The best rotation available for a frame, or None.

    The sensor quaternion is preferred: it is the full rotation, roll included.
    Failing that, a guided capture still knows where it pointed the camera -
    the whole flow is built on planning yaw and pitch and telling the user to
    aim there - and a rotation built from those two places the photo almost as
    well.

    That fallback matters more than it looks. Pose stitching is the path that
    copes with blank walls, and without it a phone that would not report a
    quaternion dropped the entire capture onto feature matching, which is
    exactly the path a blank wall defeats. The photos always carried enough to
    avoid that; nothing was asking them for it.
    """
    quat = _quat_of(frame)
    if quat is not None:
        return quat

    yaw = frame.get("yaw")
    if yaw is None:
        return None
    pitch = frame.get("pitch")
    return quaternion_from_heading(yaw, pitch if pitch is not None else 0.0)


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



def _release(images):
    """Drop the decoded source photos once a stitch has produced a panorama.

    They are the single largest thing the finalize job holds - twelve normalised
    photos is over 120MB - and nothing after the stitch reads them. Python keeps
    a list alive to the end of the enclosing function, so without this they sit
    there through the clean-up, the JPEG encode, the upload and the tiling, all
    of which happen at the worker's high-water mark. On a 512MB instance sharing
    a container with the API, that margin is the difference between finishing
    and being restarted by the platform.
    """
    freed = sum(im.nbytes for im in images) if images else 0
    del images[:]
    gc.collect()
    if freed:
        log.info("%s released %.0f MB of source photos before publishing",
                 PREFIX, freed / (1024 * 1024))


def _upload_tiles(mc, capture_id, pano):
    """Cut the panorama into cube tiles and store them.

    Entirely optional. The equirectangular JPEG is already uploaded and the
    viewer renders it on its own, so if any of this fails the capture is still
    a working 360 - just one that hands the GPU a single large texture instead
    of only the pieces on screen. That is why every error here is swallowed and
    reported as "no tiles" rather than failing the capture.

    Returns the manifest fields, or None.
    """
    if not settings.generate_tiles:
        return None
    try:
        face = face_size_for(pano.shape[1])
        levels = levels_for(face)
        count = 0
        for z, f, x, y, tile in cut_tiles(pano, face_size=face):
            put_object_bytes(mc, settings.bucket_public,
                             tile_key(capture_id, z, f, x, y),
                             encode_jpeg(tile, settings.jpeg_quality))
            count += 1
        log.info("%s capture=%s: stored %d cube tiles (face %dpx, %d levels)",
                 PREFIX, capture_id, count, face, len(levels))
        return {"face_size": face, "tile_levels": levels}
    except Exception as e:
        log.warning("%s capture=%s: cube tiles skipped (%s: %s); the "
                    "equirectangular panorama still works",
                    PREFIX, capture_id, type(e).__name__, e)
        return None


# How much vertical reach a capture must have before we will call it a sphere
# rather than a horizontal panorama. One ring of portrait photos spans about 80
# degrees, so anything appreciably wider than that came from more than one ring.
SPHERICAL_PITCH_SPREAD_DEG = 100.0

# What a capture must actually have photographed to be published as 360x180.
# Below these it is not a sphere, and padding it into one is the failure this
# whole change exists to stop. The pole thresholds are deliberately short of 90:
# the last few degrees straight overhead are a tiny solid angle and no hand-held
# capture closes them exactly.
MIN_PITCH_UP_DEG = 60.0
MIN_PITCH_DOWN_DEG = -60.0


def _pitch_of(quat):
    """Elevation of the camera axis, in degrees, for a device quaternion."""
    R = quaternion_to_matrix(*quat)
    # The rear camera looks along the device's -Z; the phone's world is +Z up.
    fz = -float(R[2][2])
    return math.degrees(math.asin(max(-1.0, min(1.0, fz))))


def _is_spherical_capture(quats):
    """Did this capture actually aim at more than the horizon?

    Derived from the poses rather than from a mode flag, because the existing
    plans do not distinguish the two - FullSpherePlan and PanoPlan are both
    ModePano - and inventing a schema field would break every capture already in
    flight. The poses cannot lie about where the phone pointed, and a single
    ring simply does not span this much pitch.
    """
    pitches = [_pitch_of(q) for q in quats if q is not None]
    if len(pitches) < 2:
        return False, 0.0
    spread = max(pitches) - min(pitches)
    return spread >= SPHERICAL_PITCH_SPREAD_DEG, spread


def _describe_missing(holes):
    """Turn hole rectangles into something a person can act on."""
    if not holes:
        return []
    out = []
    for hole in holes:
        lo, hi = hole["pitch"]
        where = ("straight overhead" if lo >= 60 else
                 "the ceiling" if lo >= 20 else
                 "straight down" if hi <= -60 else
                 "the floor" if hi <= -20 else
                 "the horizon")
        y0, y1 = hole["yaw"]
        out.append("%s, between %.0f and %.0f degrees round" % (where, y0, y1))
    return out


def _debug_dump(capture_id, name, image):
    """Write an intermediate to ORBIT_DEBUG_DIR, if one is configured.

    Looking only at the finished panorama makes it impossible to say WHERE the
    pipeline first went wrong - a stretched ceiling looks the same whether the
    photos were never taken, were dropped during warping, or were thrown away at
    the finish. These are the three places that differ.
    """
    root = os.environ.get("ORBIT_DEBUG_DIR")
    if not root or image is None:
        return
    try:
        path = os.path.join(root, str(capture_id))
        os.makedirs(path, exist_ok=True)
        cv2.imwrite(os.path.join(path, name), image)
    except Exception as e:
        log.debug("%s debug dump %s failed: %s", PREFIX, name, e)


def _debug_json(capture_id, name, payload):
    root = os.environ.get("ORBIT_DEBUG_DIR")
    if not root:
        return
    try:
        path = os.path.join(root, str(capture_id))
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, name), "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except Exception as e:
        log.debug("%s debug dump %s failed: %s", PREFIX, name, e)


def _finish_and_publish(mc, capture_id, pano, geom, ring, used, total,
                        sphere_coverage=None, coverage_note=None,
                        spherical=False):
    """Clean up a raw stitch, and either publish it or degrade honestly.

    Returns True when a sphere was published. A False return means the capture
    has already been reported as not stitched, and the caller should stop.

    The check that matters is `info.full_turn`. A stitch can succeed - every
    photo placed, seams blended, nothing black left - and still only cover a
    third of the way round. Mapping that onto a sphere stretches it by a factor
    of three, which is what the old code did silently every time. Better to say
    so and let the viewer swipe through the frames instead.
    """
    circumference = geom.circumference_px if geom else None
    equator = geom.equator_y if geom else None
    cover = getattr(geom, "coverage", None) if geom else None
    _debug_dump(capture_id, "panorama_raw.jpg", pano)
    if cover is not None:
        _debug_dump(capture_id, "coverage.png", cover)
    try:
        pano, info = finish_panorama(pano, circumference_px=circumference,
                                     equator_y=equator, coverage=cover,
                                     spherical=spherical)
    except Exception as e:
        log.warning("%s panorama clean-up failed (%s: %s); using the raw stitch",
                    PREFIX, type(e).__name__, e)
        info = None

    if info is not None and not info.full_turn:
        reason = (
            "These photos cover about %d degrees, not a full circle, so they "
            "cannot be shown as a 360 sphere without stretching them. Keep "
            "turning until you are back where you started, overlapping each "
            "photo with the last by about a third." % round(info.span_deg)
        )
        log.warning("%s capture=%s: only %.0f degrees covered; "
                    "degrading to the frame viewer", PREFIX, capture_id, info.span_deg)
        report_finalize(capture_id, {
            "stitched": False, "failure_cause": reason,
            "photos_used": used, "photos_total": total,
            "coverage_note": reason,
        })
        return False

    # A capture that claims to be 360x180 has to have BEEN 360x180. Publishing
    # one that was not is the whole failure: the missing sky and floor come back
    # as a blurred wash that reads as photography, so nobody can tell the
    # difference until they look up. Saying what is missing lets the app ask for
    # it instead.
    if spherical and info is not None and info.sphere_coverage is not None:
        short = (info.sphere_coverage < MIN_SPHERE_COVERAGE
                 or (info.pitch_max_deg or 0) < MIN_PITCH_UP_DEG
                 or (info.pitch_min_deg or 0) > MIN_PITCH_DOWN_DEG)
        if short:
            missing = _describe_missing(info.holes)
            reason = (
                "These photos cover %.0f%% of the sphere, from %.0f to %.0f "
                "degrees of tilt. Publishing that as a full 360 would mean "
                "inventing the parts nobody photographed. Still needed: %s."
                % (info.sphere_coverage * 100, info.pitch_min_deg,
                   info.pitch_max_deg,
                   "; ".join(missing[:4]) if missing else "more of the ceiling and floor")
            )
            log.warning("%s capture=%s: spherical capture is incomplete "
                        "(%.2f coverage, pitch %.0f..%.0f)", PREFIX, capture_id,
                        info.sphere_coverage, info.pitch_min_deg, info.pitch_max_deg)
            _debug_json(capture_id, "coverage.json", {
                "status": "incomplete", "mode": "spherical",
                "reason": "insufficient_coverage",
                "sphere_coverage": info.sphere_coverage,
                "vertical_min_deg": info.pitch_min_deg,
                "vertical_max_deg": info.pitch_max_deg,
                "holes": list(info.holes),
            })
            report_finalize(capture_id, {
                "stitched": False, "failure_cause": reason,
                "photos_used": used, "photos_total": total,
                "coverage_note": reason,
                "status": "incomplete", "mode": "spherical",
                "reason": "insufficient_coverage",
                "sphere_coverage": info.sphere_coverage,
                "vertical_min_deg": info.pitch_min_deg,
                "vertical_max_deg": info.pitch_max_deg,
                "missing_regions": missing,
            })
            return False

    h, w = pano.shape[:2]
    _debug_dump(capture_id, "panorama_final.jpg", pano)
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
        return False

    body = {
        "stitched": True,
        "panorama_key": panorama_key(capture_id),
        "width": w, "height": h,
        "photos_used": used, "photos_total": total,
        "coverage_note": coverage_note or describe_leftovers(total, used),
    }

    tiles = _upload_tiles(mc, capture_id, pano)
    if tiles:
        body.update(tiles)
    if sphere_coverage is not None:
        body["sphere_coverage"] = sphere_coverage
    if info is not None:
        body["mode"] = "spherical" if spherical else "horizontal"
        body["horizontal_coverage"] = round(min(1.0, info.span_deg / 360.0), 4)
        if info.sphere_coverage is not None:
            # Measured from the pixels that were actually placed, which is a
            # stricter and more useful number than the one derived from the
            # quaternions alone - it accounts for photos that never made it in.
            body["sphere_coverage"] = round(info.sphere_coverage, 4)
            body["vertical_min_deg"] = round(info.pitch_min_deg, 1)
            body["vertical_max_deg"] = round(info.pitch_max_deg, 1)
        _debug_json(capture_id, "coverage.json", {
            "status": "completed", "mode": body["mode"],
            "width": w, "height": h,
            "horizontal_coverage": body["horizontal_coverage"],
            "sphere_coverage": body.get("sphere_coverage"),
            "vertical_min_deg": body.get("vertical_min_deg"),
            "vertical_max_deg": body.get("vertical_max_deg"),
            "holes": list(info.holes),
        })
    report_finalize(capture_id, body)
    log.info("%s stitch succeeded capture=%s size=%sx%s using %d of %d",
             PREFIX, capture_id, w, h, used, total)
    return True


def handle_finalize_job(mc, job, attempt=1):
    capture_id = job["capture_id"]
    log.info("%s finalize job capture=%s: waiting for frames", PREFIX, capture_id)

    cap, frames = wait_for_frames(capture_id)
    mode = cap.get("mode", "pano")

    done_frames = [f for f in frames if f.get("status") == "done"]

    if mode == "spin":
        log.info("%s spin mode capture=%s -> frame renderer, no stitch attempted", PREFIX, capture_id)
        report_finalize(capture_id, {"stitched": False})
        return

    if not done_frames:
        reason = "No photo was processed successfully, so there is nothing to build from."
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

    # How big the photos may be, decided BEFORE any of them are loaded.
    #
    # A stitch needs every source photo resident at once, so photo count times
    # photo size is the floor under the whole job and no later cap can claw it
    # back. Thirty-one photos at 1600px is over 300MB before the stitcher
    # allocates anything of its own - which is exactly how a capture ends up
    # OOM-killed, retried, killed again, and reported as "the photos were too
    # large to process".
    #
    # On a host with room this returns the stored width and changes nothing.
    load_width, why = plan_source_width(
        len(ring), settings.target_width_default, _cgroup_memory_limit_mb(),
        attempt=attempt)
    if why:
        log.warning("%s capture=%s: %s", PREFIX, capture_id, why)

    # Loaded images and their source frames are kept in lockstep. If a frame
    # fails to load, dropping it from one list but not the other would attach
    # every subsequent rotation to the wrong photo.
    images = []
    loaded = []
    for f in ring:
        try:
            raw = get_object_bytes(mc, settings.bucket_public, processed_key(capture_id, f["index"]))
            img = decode_bytes_to_bgr(raw)
            if img is not None and img.shape[1] > load_width:
                img = resize_to_width(img, load_width)
            # The encoded bytes are the other copy of this photo in memory, and
            # they are no longer needed once it is decoded.
            del raw
        except Exception as e:
            log.warning("%s capture=%s: could not load processed frame %s for stitching: %s",
                        PREFIX, capture_id, f.get("index"), e)
            continue
        images.append(img)
        loaded.append(f)
    ring = loaded

    if not images:
        reason = "No processed photo could be loaded, so there is nothing to build from."
        report_finalize(capture_id, {"stitched": False, "failure_cause": reason})
        return

    total = len(images)

    # Rotations first, pixels second. If we know where the camera was pointing
    # there is nothing to rediscover: a blank wall places as reliably as a
    # bookshelf. _pose_of falls back to the recorded heading and tilt, so this
    # now covers guided captures from phones that never gave up a quaternion.
    quats = [_pose_of(f) for f in ring]
    posed = sum(1 for q in quats if q is not None)

    # posed >= 1: a lone photo can only be placed by its rotation — feature
    # matching needs a pair — so the pose path has to be allowed to try it.
    if posed >= 1 and posed >= total * 0.8:
        spherical, spread = _is_spherical_capture(quats)
        log.info("%s capture=%s: %d of %d photos carry a usable rotation; "
                 "stitching from known poses (pitch spread %.0f deg -> %s mode)",
                 PREFIX, capture_id, posed, total, spread,
                 "spherical" if spherical else "horizontal")
        _debug_json(capture_id, "poses.json", [
            {"index": f.get("index"), "yaw": f.get("yaw"), "pitch": f.get("pitch"),
             "quat": q, "camera_pitch_deg": None if q is None else round(_pitch_of(q), 2)}
            for f, q in zip(ring, quats)])
        ok, pano, reason, geom = stitch_with_poses(images, quats)
        if ok and pano is not None:
            src_h, src_w = images[0].shape[:2]
            coverage = sphere_coverage(
                [q for q in quats if q is not None],
                hfov_deg=geom.hfov_deg, aspect=src_h / float(src_w))
            log.info("%s capture=%s covers %.0f%% of the sphere",
                     PREFIX, capture_id, coverage * 100)
            _release(images)
            # Whatever comes of it, the capture has been reported on: either a
            # sphere was published or it was degraded to the frame viewer. Only
            # a stitch that never produced a picture falls through to the next
            # method - carrying on after a degrade would report twice.
            _finish_and_publish(mc, capture_id, pano, geom, ring,
                                used=posed, total=total,
                                sphere_coverage=coverage, spherical=spherical)
            return
        log.warning("%s capture=%s: pose stitch unusable (%s); "
                    "falling back to feature matching", PREFIX, capture_id, reason)

    # No usable rotations. Match features instead - but through our own
    # detail pipeline, which hands back the camera parameters. cv2.Stitcher
    # will not, and without them nothing downstream can tell a full turn from a
    # third of one.
    ok, pano, reason, geom, kept = stitch_with_features(images)
    if ok and pano is not None:
        _release(images)
        _finish_and_publish(mc, capture_id, pano, geom, ring,
                            used=len(kept), total=total,
                            coverage_note=describe_leftovers(total, len(kept)))
        return
    log.warning("%s capture=%s: feature stitch failed (%s); trying cv2.Stitcher",
                PREFIX, capture_id, reason)

    # Last resort. This one gives us no geometry at all, so finish_panorama has
    # to fall back to assuming a full turn - the assumption that used to be made
    # everywhere. It is kept only because some result beats none.
    fallback_ok, fallback_pano, fallback_reason = stitch_panorama(images)
    if not fallback_ok or fallback_pano is None:
        report_finalize(capture_id, {
            "stitched": False,
            "failure_cause": reason or fallback_reason,
            "photos_used": 0, "photos_total": total,
            "coverage_note": reason or fallback_reason,
        })
        return

    _release(images)
    _finish_and_publish(mc, capture_id, fallback_pano, None, ring,
                        used=total, total=total)


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
                # The attempt number reaches the job so a retry after a memory
                # failure loads the photos smaller. Retrying at the same size
                # fails the same way, three times, slowly.
                handle_finalize_job(mc, job, attempt=attempt)
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
    redis_desc = "url" if settings.redis_url else f"{settings.redis_host}:{settings.redis_port}"
    log.info("%s starting, redis=%s minio=%s api=%s",
              PREFIX, redis_desc, settings.minio_endpoint, settings.api_base_url)
    # The memory settings decide whether a stitch survives on a small instance,
    # and an OOM kill leaves no trace of its own - the container simply
    # restarts. Print them so a deploy can be checked from the log alone.
    from config import _cgroup_memory_limit_mb
    log.info("%s memory limit=%s MiB, target_width=%s, compositing=%s Mpx (%s)",
             PREFIX, _cgroup_memory_limit_mb() or "unlimited",
             settings.target_width_default,
             settings.stitch_compositing_mp or "full",
             "explicit" if os.environ.get("STITCH_COMPOSITING_MP") else "auto")
    start_health_server()
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
