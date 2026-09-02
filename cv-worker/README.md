# Orbit CV worker

Python worker that consumes the Go backend's Redis Stream job queue,
processes captured photos into a 360 viewer (panorama stitch or
frame-swap), and reports results back to the Go API's internal callbacks.

## Setup

```bash
cd cv-worker
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Infra (Redis, MinIO, Postgres) is started separately via `docker compose up`
in the repo root. This worker does not touch Postgres directly — all state
changes go through the Go API's internal HTTP callbacks.

## Run

```bash
. .venv/bin/activate
python3 worker.py
```

Config is entirely environment-variable driven (see `config.py`); sane
localhost defaults match `docker-compose.yml` and the Go API's own defaults
(Redis on 6380, MinIO on 9010, API on :8080).

## What it does

1. **`capture.frame.process`** — downloads the original from
   `orbit-private`, applies EXIF rotation, strips metadata, resizes to
   `settings.target_width`, CLAHE colour-normalises, optionally
   centroid-aligns (spin mode only), encodes a full JPEG (q=85) to
   `captures/{id}/processed/{idx:03d}.jpg` and a thumb to
   `captures/{id}/thumb/{idx:03d}.jpg` in `orbit-public`, then POSTs
   `.../frames/{frame_id}/done` or `.../failed`.

2. **`capture.finalize`** — polls `GET /api/v1/captures/{id}` (and
   `/frames`) until every frame is done or failed, or 180s elapses. For
   `mode=pano`, sorts ring frames by yaw and attempts `cv2.Stitcher`
   (SCANS mode first, then PANORAMA) to build an equirectangular panorama.
   Success uploads to `captures/{id}/panorama.jpg` and reports
   `{"stitched": true, ...}`. Failure reports `{"stitched": false,
   "failure_cause": "<plain English>"}` — the Go side already falls back to
   the frame-swap viewer, so a failed stitch never crashes the worker or
   leaves the capture stuck. `mode=spin` always reports
   `{"stitched": false}` by design (spin uses the frame renderer).

## Error handling

- Each job retries up to 3 times with exponential backoff
  (`2s, 4s, ...`), except image-decode/corruption errors, which are
  treated as permanent (no point retrying a corrupt file).
- On success: `XACK`.
- On exhausted retries / permanent error: the API is told about the
  failure in plain English (`.../failed` for a frame,
  `{"stitched": false, "failure_cause": ...}` for finalize), the job is
  pushed to the `orbit:jobs:dlq` stream with the error, and it is still
  `XACK`ed so the stream doesn't stall.
- OpenCV `Stitcher` status codes are mapped to plain-English causes users
  can act on (not enough overlap, photos don't line up, camera angles
  inconsistent).
- One bad photo never kills a whole capture: `finalize` only requires 2+
  successfully processed frames to attempt a stitch, and reports a
  degraded-but-usable result otherwise.

## Optional background removal

`rembg` is a heavy dependency and is **not** installed by default. Set
`ENABLE_BG_REMOVAL=true` and `pip install rembg` separately to turn it on;
the import is lazy and guarded so the worker runs fine without it.

## Testing

`scripts/make_test_photos.py` synthesises a wide gradient+shapes scene and
crops 8 overlapping (~45%) windows out of it — a photo set with a genuine
chance of stitching successfully, unlike random unrelated images.

```bash
python3 scripts/make_test_photos.py /tmp/orbit_test_photos
```

Then create a capture, upload photos via multipart POST, POST
`/process`, and run `worker.py` — see the project's end-to-end test run for
the exact `curl` sequence.
