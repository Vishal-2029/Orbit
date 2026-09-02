# Spin360 — Object-Spin 360° Viewer Platform

## Context

Build an application where a user uploads a set of photos taken around an object and
gets back a smooth, draggable 360° spin view.

This is **turntable spin**, not photogrammetry and not AI view synthesis. The output is
an ordered, normalized, aligned stack of images plus a manifest; the "3D" is an illusion
created by swapping frames as the user drags. This choice is deliberate: it is fully
deterministic, needs no GPU, and is what every e-commerce 360 viewer actually does.

**Stack decision:** Go + Fiber backend, Flutter client (mobile-first, Flutter Web for the
shareable viewer). Heavy image work (object alignment, background removal) goes to a
separate Python worker, because OpenCV/rembg have no credible Go equivalent.

Intended outcome: capture → upload → process → share a public spin URL, with live
progress the whole way.

---

## 1. System architecture

```
┌──────────────┐        ┌─────────────────────────┐        ┌──────────────┐
│   Flutter    │──REST──▶      Go + Fiber API     │───────▶│  PostgreSQL  │
│ (mobile/web) │◀──WS───│  auth, spins, uploads,  │        └──────────────┘
└──────────────┘        │  progress hub, manifest │        ┌──────────────┐
       │                └───────────┬─────────────┘───────▶│    Redis     │
       │ direct upload              │ publish              └──────────────┘
       │ (presigned)                ▼
       ▼                    ┌──────────────┐
┌──────────────┐            │    Kafka     │
│    MinIO     │            └──────┬───────┘
│  (S3 store)  │                   │ consume
└──────────────┘            ┌──────▼─────────────┐
       ▲                    │  Go Orchestrator   │  job state machine, retries
       │                    └──────┬─────────────┘
       │                           │ Kafka: spin.process.image
       │                    ┌──────▼─────────────┐
       └────────────────────│  Python CV Worker  │  align, cutout, encode
                            └────────────────────┘
```

### Why each piece

| Component | Choice | Why |
|---|---|---|
| API | **Go + Fiber** | Fast, your stack. Handles auth, CRUD, presigned URLs, WS fan-out. |
| Object storage | **MinIO** (S3 API) | Originals + derivatives + sprite sheets. Swap to S3 in prod with zero code change. |
| Database | **PostgreSQL** | Relational and it fits: users → spins → frames. `JSONB` column for the manifest. Do **not** use Mongo here; the data is strictly relational. |
| Cache / locks | **Redis** | Job progress counters, rate limits, presigned-URL dedupe, WS session registry for multi-instance fan-out (Redis Pub/Sub). |
| Queue | **Kafka** | Per-frame jobs are a natural partitioned stream. Partition key = `spin_id` so all frames of one spin land in order on one partition. Gives replay + durability. |
| Realtime | **WebSocket** (Fiber's `contrib/websocket`) | Push per-frame progress to the Flutter client. |
| CV worker | **Python** (OpenCV, rembg, Pillow) | Alignment and background removal. |
| Client | **Flutter** | One codebase: capture-assist on mobile, viewer on web. |

### Honest note on Kafka
For a v1 with one server, Kafka is over-engineering — Redis Streams or Asynq would do
the same job with a tenth of the ops burden. Since you asked for it explicitly, the plan
uses Kafka, and the topic design below is genuinely useful once you have several worker
replicas. Keep the publisher behind a `Queue` interface so it can be swapped.

---

## 2. Repository structure

Three repos (or one monorepo with these top-level dirs):

```
spin360/
├── backend/                      # Go + Fiber
│   ├── cmd/
│   │   ├── api/main.go           # HTTP + WS server
│   │   └── orchestrator/main.go  # Kafka consumer, job state machine
│   ├── internal/
│   │   ├── config/               # env loading
│   │   ├── http/
│   │   │   ├── router.go
│   │   │   ├── middleware/       # auth, ratelimit, requestid, recover, cors
│   │   │   └── handlers/         # auth, spin, upload, manifest, ws
│   │   ├── domain/               # entities + interfaces (no deps)
│   │   │   ├── spin.go
│   │   │   ├── frame.go
│   │   │   └── ports.go          # Repository, Storage, Queue, Hub interfaces
│   │   ├── service/              # business logic
│   │   │   ├── spin_service.go
│   │   │   ├── upload_service.go
│   │   │   └── manifest_service.go
│   │   ├── repo/postgres/        # sqlc-generated queries
│   │   ├── storage/minio/
│   │   ├── queue/kafka/          # producer + consumer (segmentio/kafka-go)
│   │   ├── cache/redis/
│   │   ├── realtime/             # WS hub + Redis pubsub bridge
│   │   └── pipeline/             # job state machine, retry policy
│   ├── migrations/               # goose/atlas SQL migrations
│   └── docker-compose.yml
│
├── cv-worker/                    # Python
│   ├── worker.py                 # Kafka consumer loop
│   ├── ops/
│   │   ├── normalize.py          # resize, EXIF rotate, colorspace
│   │   ├── align.py              # centroid/ECC alignment
│   │   ├── cutout.py             # rembg background removal
│   │   ├── encode.py             # WebP variants
│   │   └── sprite.py             # sprite sheet builder
│   └── requirements.txt
│
└── app/                          # Flutter
    ├── lib/
    │   ├── main.dart
    │   ├── core/                 # dio client, ws client, di, theme, router
    │   ├── features/
    │   │   ├── auth/
    │   │   ├── capture/          # camera + turntable guidance overlay
    │   │   ├── upload/           # batch picker, reorder, progress
    │   │   ├── spin_list/
    │   │   └── viewer/           # THE spin widget
    │   └── shared/widgets/
    └── pubspec.yaml
```

---

## 3. Data model (PostgreSQL)

```sql
users(id uuid pk, email citext unique, password_hash text, created_at timestamptz)

spins(
  id uuid pk,
  user_id uuid fk,
  title text,
  slug text unique,                    -- public share URL
  status text,                         -- draft|uploading|queued|processing|ready|failed
  frame_count int,
  processed_count int,                 -- for progress %
  settings jsonb,                      -- {bg_remove, align, target_width, direction}
  manifest jsonb,                      -- final viewer manifest, null until ready
  error text,
  is_public bool default false,
  created_at, updated_at
)

frames(
  id uuid pk,
  spin_id uuid fk on delete cascade,
  index int,                           -- 0..n-1, the spin order
  original_key text,                   -- MinIO object key
  processed_key text,
  thumb_key text,
  width int, height int,
  offset_x int, offset_y int,          -- alignment correction applied
  status text,                         -- pending|processing|done|failed
  unique(spin_id, index)
)

jobs(
  id uuid pk, spin_id uuid fk, type text, status text,
  attempts int, last_error text, created_at, updated_at
)
```

Indexes: `frames(spin_id, index)`, `spins(user_id, created_at desc)`, `spins(slug)`.

---

## 4. Storage layout (MinIO)

```
spins/{spin_id}/original/{index}.jpg        # untouched upload
spins/{spin_id}/processed/{index}.webp      # aligned, cut out, full-res
spins/{spin_id}/preview/{index}.webp        # ~500px, loads first
spins/{spin_id}/sprite.webp                 # grid sheet (only if frames <= 36)
spins/{spin_id}/manifest.json               # cached copy, CDN-friendly
```

Buckets: `spin-private` (originals), `spin-public` (derivatives, public-read).

---

## 5. Kafka topics

| Topic | Key | Payload | Consumer |
|---|---|---|---|
| `spin.created` | `spin_id` | spin id, settings | Orchestrator |
| `spin.frame.process` | `spin_id` | spin_id, frame_id, index, key, settings | Python CV worker |
| `spin.frame.done` | `spin_id` | frame_id, processed_key, offsets, dims | Orchestrator |
| `spin.finalize` | `spin_id` | spin_id | Orchestrator (sprite + manifest) |
| `spin.events` | `spin_id` | progress/status events | API (→ WS fan-out) |
| `spin.dlq` | — | failed jobs after N retries | manual/ops |

Partition key = `spin_id` everywhere, so one spin's frames stay ordered and one
consumer owns it. Consumer groups: `cv-workers`, `orchestrator`, `api-events`.

---

## 6. Processing pipeline (the part that decides quality)

Per frame, in the Python worker:

1. **EXIF-correct rotation**, strip metadata.
2. **Normalize** — resize to a fixed target width, identical for every frame; convert to
   a consistent colorspace.
3. **Align / de-jitter** — the object drifts a few pixels between shots when the rig
   isn't perfect, and that reads as wobble in the final spin. Two-tier approach:
   - Segment the object (threshold or rembg mask), compute its centroid and bbox.
   - Translate the frame so the centroid sits at a canvas-fixed point.
   - Optional refinement: OpenCV `findTransformECC` (translation-only) against frame 0.
   - Store `offset_x/offset_y` per frame for debugging.
   **This single step is the difference between "professional" and "amateur".**
4. **Background removal** (optional per spin settings) — `rembg` / U²-Net → RGBA, or
   composite onto white.
5. **Encode variants** — full WebP (q=82) + preview WebP (~500px, q=70).
6. Emit `spin.frame.done`.

Then once all frames are done, `spin.finalize`:
7. **Sprite sheet** for ≤36 frames (one request, one decode). Individual files above that.
8. **Write manifest.json**, set `spins.status = ready`, emit final WS event.

### Manifest format
```json
{
  "version": 1,
  "spin_id": "…",
  "frame_count": 36,
  "direction": "cw",
  "default_frame": 0,
  "width": 1200, "height": 1200,
  "sprite": { "url": "…/sprite.webp", "cols": 6, "rows": 6 },
  "frames":  ["…/processed/0.webp", "…"],
  "preview": ["…/preview/0.webp",  "…"]
}
```

---

## 7. Go API surface

```
POST   /api/v1/auth/register | /login | /refresh
POST   /api/v1/spins                      → create draft, return spin_id
POST   /api/v1/spins/:id/upload-urls      → N presigned PUT URLs (client uploads direct to MinIO)
POST   /api/v1/spins/:id/frames/commit    → client confirms uploads + final frame ORDER
POST   /api/v1/spins/:id/process          → validate, publish spin.created, status=queued
GET    /api/v1/spins/:id                  → status + progress
GET    /api/v1/spins                      → list (paginated)
PATCH  /api/v1/spins/:id                  → title, is_public, reorder, reverse direction
DELETE /api/v1/spins/:id
GET    /api/v1/spins/:id/manifest         → viewer manifest (public if is_public)
GET    /s/:slug                           → public share page
WS     /ws/spins/:id                      → live progress
```

**Direct-to-MinIO presigned upload is important** — do not proxy 40 photos through the
Go API. The API only issues URLs and records keys.

### WebSocket messages (server → client)
```json
{"type":"frame_done","index":7,"processed":8,"total":36}
{"type":"status","status":"processing"}
{"type":"ready","manifest_url":"…"}
{"type":"error","message":"…"}
```
Multi-instance fan-out: the API subscribes to Redis Pub/Sub channel `spin:{id}`; the
orchestrator publishes there. Any API pod can serve any client's socket.

---

## 8. Flutter app

**Packages:** `dio` (upload w/ progress), `web_socket_channel`, `image_picker` +
`camera`, `riverpod` (state), `go_router`, `flutter_secure_storage`, `reorderable_grid_view`.

### Screens
1. **Capture** (mobile) — camera with a turntable guidance overlay: a target ring, a
   frame counter, and an angle-step prompt ("shoot 36 photos, 10° apart"). Optional
   ghost-overlay of the previous frame at low opacity to keep framing consistent.
2. **Upload** — batch picker, auto-sort by EXIF timestamp then filename, drag-to-reorder
   grid, reverse-direction toggle, per-file upload progress.
3. **Processing** — WS-driven progress bar with per-frame ticks and a live thumbnail
   strip filling in.
4. **Spin list** — user's spins, each with a thumbnail and status chip.
5. **Viewer** — the payoff.

### The viewer widget (the actual engine, ~200 lines)
- Fetch manifest → preload **preview** frames into `ui.Image` list, show progress;
  swap in full-res in the background.
- `GestureDetector` on horizontal drag:
  ```dart
  frameIndex = (startFrame + (dragDx / sensitivity).round()) % frameCount;
  if (frameIndex < 0) frameIndex += frameCount;   // the modulo is what makes it loop
  ```
- Render via `CustomPainter` on a `Canvas` — no flicker, and pan/zoom comes free with
  a canvas transform.
- Extras: flick inertia (decay the velocity, keep stepping frames), autospin on load
  that stops on first touch, double-tap zoom, fullscreen.
- Preload **all** frames before enabling drag. Lazy-loading mid-drag = visible gaps.

---

## 9. Build order

**Phase 1 — vertical slice, no queue**
Postgres + MinIO in docker-compose. Go API: create spin → presigned upload → commit →
process **synchronously in-process** (resize only, no align/cutout) → manifest.
Flutter: picker + upload + viewer widget.
*Goal: drag a real spin end to end.* Everything after this is quality and scale.

**Phase 2 — async**
Add Kafka + orchestrator + Python CV worker. Move processing off the request path.
Add WebSocket + Redis pubsub progress.

**Phase 3 — quality**
Alignment (centroid → ECC), background removal, sprite sheets, preview/full variants.

**Phase 4 — product**
Auth, public share slugs, spin list/management, capture-assist camera overlay,
embeddable web viewer.

**Phase 5 — hardening**
DLQ + retry policy, rate limits, storage quotas, orphaned-upload cleanup job, metrics.

---

## 10. Verification

- **Backend unit:** alignment math, manifest generation, frame ordering after reorder.
- **Integration (testcontainers):** Postgres + MinIO + Kafka; upload 36 fixtures →
  assert status reaches `ready`, manifest has 36 frames in order, sprite dims = 6×6.
- **Failure paths:** kill the CV worker mid-spin → orchestrator retries → job resumes,
  no duplicate frames. Push a corrupt image → frame marked `failed`, spin `failed`,
  error surfaced over WS.
- **Manual:** shoot a real 36-photo turntable set of an actual object. Check for wobble
  with alignment off vs on — that comparison is the acceptance test for the whole
  pipeline.
- **Flutter:** widget test for modulo wrap-around at both ends (drag past frame 0 going
  left must land on frame N-1); manual smoke on mobile + web.
- **Load:** 10 concurrent spins × 36 frames; confirm Kafka partitions spread across
  worker replicas and no spin's frames interleave.

---

## Open decisions

- **Kafka vs Redis Streams/Asynq** — recommend starting with the `Queue` interface and
  an Asynq impl in Phase 1–2, adding Kafka in Phase 3 when there's real parallelism.
- **Auth** — JWT access/refresh vs a hosted provider.
- **rembg model** — `u2net` (better) vs `u2netp` (4× faster). Start with `u2netp`.
