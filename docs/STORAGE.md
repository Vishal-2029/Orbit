# Storage layer

## Layout

Every capture (a 360 session) owns a prefix in object storage:

```
captures/<capture-id>/original/<idx:03d>.jpg   raw upload from the client
captures/<capture-id>/processed/<idx:03d>.jpg  aligned/background-removed frame
captures/<capture-id>/thumb/<idx:03d>.jpg       small preview used in the UI
captures/<capture-id>/panorama.jpg              stitched equirectangular output
```

Key helpers live in `internal/storage/minio.go` (`OriginalKey`, `ProcessedKey`,
`ThumbKey`, `PanoramaKey`, `CapturePrefix`) so the layout exists in exactly
one place. Deleting a capture's storage is always `Delete(ctx, bucket,
CapturePrefix(id))` - a recursive prefix delete, never a manual per-key loop.

Postgres (`captures`, `frames`, `jobs` tables - see `migrations/001_init.sql`)
is the source of truth for what *should* exist; object storage just holds the
bytes referenced by `frames.original_key` / `processed_key` / `thumb_key` and
by the capture's `manifest`.

## Two buckets, and why

- `orbit-private` - original uploads. These are unprocessed, potentially
  large, and never served directly; the API always proxies reads
  (`PublicURL` builds `/api/v1/captures/.../image/...` URLs) so the bucket
  never needs public/anonymous read access or CORS configuration.
- `orbit-public` - processed outputs (thumbnails, stitched panoramas,
  anything meant to be fetched directly by a viewer/CDN). Splitting them out
  means the bucket that *can* be made publicly readable or fronted by a CDN
  never contains an unprocessed original, and a bug in the public-serving
  path can't leak files that haven't been through processing.

Both buckets share the same `captures/<id>/...` key layout, just scoped to
different kinds of objects, so `CapturePrefix` cleans up a capture in either
bucket the same way.

## Cleanup (orphan sweeper)

Two independent processes write to storage/DB (the API on upload, the
cv-worker on process, `Capture.Delete` on delete), and any of those can be
interrupted mid-way - an upload that's proxied through the API can fail after
`Put` but before the frame row commits, a browser tab can be closed on a
draft that never gets photos, etc. `internal/storage/cleanup.go` implements
`SweepOrphans` to reconcile the two sides:

1. **Orphan objects** - lists the capture-id prefixes actually present under
   `captures/` in each bucket (one delimited `ListObjects` call per bucket,
   not a full recursive enumeration) and diffs them against the capture IDs
   in Postgres. Any prefix with no matching row is an orphan.
2. **Orphan capture rows** - `draft` or `uploading` captures older than 24h
   with zero frame rows. These are abandoned sessions: the user opened the
   uploader and never got anywhere.

The sweeper is **dry-run by default** - `SweepOrphans(ctx, store, pool,
buckets...)` only reports what it found. Nothing is deleted unless you call
`SweepOrphansApply` (or pass `--apply` via storagectl), which is a distinct
entry point rather than a boolean easy to flip by accident in calling code.
When applied, capture-row deletion also best-effort deletes any objects that
might exist for that ID (cheap safety net; the object-orphan pass would catch
it on the next sweep regardless).

Run it periodically (e.g. a daily cron/systemd timer calling `storagectl
sweep --apply`) rather than on every request - it does a bucket-wide listing
and a full-table status scan, which is not something you want on the hot
path.

## storagectl

A small operator CLI, `cmd/storagectl`, built against the same config env
vars as the API (`DATABASE_URL`, `MINIO_ENDPOINT`, etc. - see
`internal/config/config.go` for defaults, which match the local `docker
compose` setup).

Build and run:

```sh
cd backend
go build -o storagectl ./cmd/storagectl

./storagectl stats
# object counts + bytes per bucket, capture counts by status

./storagectl sweep
# dry-run: lists what SweepOrphans would delete

./storagectl sweep --apply
# actually deletes orphan objects and stale draft/uploading captures

./storagectl verify <capture-id>
# checks every frame row's original_key actually resolves to an object
# in orbit-private; prints and exits non-zero if any are missing
```

`verify` is the tool to reach for when a capture is stuck or a viewer 404s on
an image: it tells you whether the DB row is lying about what's in storage,
as opposed to a processing bug further down the pipeline.
