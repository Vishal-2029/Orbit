# Orbit API reference

Base URL: `http://localhost:8080`

All responses are JSON. Errors are `{"error": "plain english message"}` with an
appropriate status code.

---

## Health

```
GET /health  →  {"status":"ok","service":"orbit-api","time":"..."}
```

## Create a capture

```
POST /api/v1/captures
{ "title": "My living room", "mode": "pano",
  "ring_count": 6, "include_up_down": true, "remove_bg": false }
```

`mode` is one of:

| Mode | Meaning | Shot list | Direction check |
|---|---|---|---|
| `pano` (default) | stand still, guided | front, right, behind, left (+ optional up/down/extras) | yes |
| `spin` | orbit an object | evenly spaced turntable steps | yes |
| `auto` | photos you already have | none — upload in any order | no |

Anything unrecognised falls back to `pano`. For `pano`, a `ring_count` above 6
also offers the four in-between angles.

In `auto` mode the plan comes back with `slots: null` and
`duplicate_tolerance: 0`. There are no directions to aim at, so the client just
uploads every photo with `has_heading=false` and `index` set to its position in
the list. The stitcher matches features pairwise and works out the arrangement
itself, so upload order does not matter. `min_required` is still 4.

Returns `201` with **both** the capture and the shot plan:

```json
{
  "capture": { "id": "...", "slug": "97s7eyucpp", "mode": "pano",
               "status": "draft", "frame_count": 10, "settings": {...} },
  "plan": {
    "mode": "pano",
    "min_required": 8,
    "yaw_step": 45,
    "align_tolerance": 12,
    "slots": [
      { "id":"ring_0", "index":0, "label":"Front", "icon":"▲", "yaw":0, "pitch":0,
        "hint":"Start here. Pick something you can remember and point at it.",
        "required":true },
      { "id":"ring_2", "index":2, "label":"Right side", "icon":"▶", "yaw":90,
        "hint":"Turn right again. You're now looking 90° from the start.",
        "required":true },
      { "id":"up", "index":8, "label":"Up (ceiling / sky)", "icon":"⬆",
        "pitch":90, "required":false }
    ],
    "tips": ["Stand still. Pivot on the spot — do not walk in a circle.", "..."]
  }
}
```

The `plan` is the whole capture UX: render `slots` in order, show `label` and
`hint`, and use `yaw` + the device compass to point the user.

## Fetch the plan again

```
GET /api/v1/captures/:id/plan  →  the same Plan object
```

## Upload one photo

`multipart/form-data`:

```
POST /api/v1/captures/:id/photos

photo    (file, required)   the JPEG
index    (int,  required)   which slot, 0-based
slot_id  (string)           e.g. "ring_2" or "up"
yaw      (float)            compass heading the phone was actually at
pitch    (float)            tilt the phone was actually at
```

Returns `201 {"frame": {...}}`. Re-uploading the same `index` **replaces** that
photo, which is how "retake this shot" works.

Only allowed while the capture is `draft` or `uploading`.

## Start processing

```
POST /api/v1/captures/:id/process
```

Rejects with `400` and a readable message if fewer than `min_required` (4) photos
were uploaded.
Otherwise queues one job per frame plus a finalize job and moves the capture to
`queued`.

## Poll status

```
GET /api/v1/captures/:id  →  {"capture": {...}, "progress": 0..100}
GET /api/v1/captures      →  {"captures": [...]}   ?limit=50&offset=0
GET /api/v1/captures/:id/frames → {"frames":[...]}
```

## Live progress (WebSocket)

```
WS /ws/captures/:id
```

The server sends the current state on connect, then pushes:

```json
{"type":"status",      "status":"processing", "progress":10}
{"type":"frame_done",  "index":3, "processed":4, "total":10, "progress":32}
{"type":"frame_failed","index":5, "message":"photo was too blurry to use"}
{"type":"ready",       "status":"ready", "progress":100, "manifest":{...}}
{"type":"error",       "message":"..."}
```

The socket closes itself after `ready`.

## Get the viewer manifest

```
GET /api/v1/captures/:id/manifest
GET /s/:slug/manifest                 ← public share link, needs is_public
```

`409` while not ready yet. When ready:

```json
{
  "version": 1, "capture_id": "...", "slug": "97s7eyucpp",
  "title": "My living room", "mode": "pano",
  "renderer": "sphere",
  "panorama": "http://localhost:8080/api/v1/captures/.../image/panorama",
  "width": 4096, "height": 2048,
  "degraded": false
}
```

or, when the stitch could not be done:

```json
{
  "renderer": "frames",
  "frames":   ["http://.../image/processed/0", "..."],
  "previews": ["http://.../image/thumb/0", "..."],
  "yaws":     [0, 45, 90, 135, 180, 225, 270, 315],
  "degraded": true,
  "degraded_why": "The photos could not be stitched into one seamless sphere, so they are shown as a swipeable 360 sequence instead. Retake with more overlap between shots for a seamless result."
}
```

**A client only needs to branch on `renderer`.** `sphere` → texture an inverted
sphere with `panorama`. `frames` → preload `frames` and swap on drag.

## Images

```
GET /api/v1/captures/:id/image/panorama
GET /api/v1/captures/:id/image/processed/:idx
GET /api/v1/captures/:id/image/thumb/:idx
GET /api/v1/captures/:id/image/original/:idx
```

Served through the API so the object store is never publicly exposed.

## Manage

```
PATCH  /api/v1/captures/:id   {"title":"New name","is_public":false}
DELETE /api/v1/captures/:id   removes the row and every stored object
```

## Internal (CV worker → API)

Not for clients.

```
POST /api/v1/internal/captures/:id/frames/:frameId/done    {"index":0,"width":1600,"height":1200}
POST /api/v1/internal/captures/:id/frames/:frameId/failed  {"index":0,"reason":"..."}
POST /api/v1/internal/captures/:id/finalize
     {"stitched":true,"panorama_key":"...","width":4096,"height":2048}
     {"stitched":false,"failure_cause":"Not enough overlap between photos."}
```
