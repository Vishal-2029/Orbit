# Orbit — Architecture

## What this product does

A user stands in one place, is guided by the app to take **at least 4 photos**
— front, right, behind, left — with up, down and the in-between angles offered
afterwards, and gets back a **draggable 360° view** they can share with a link.

There is a second mode for objects: put the object on a turntable, orbit it, and
get an e-commerce style spin.

| Mode | The user... | Output | Renderer |
|---|---|---|---|
| `pano` (default) | stands still and turns on the spot | one stitched equirectangular image | textured sphere |
| `spin` | orbits an object | an ordered stack of aligned frames | frame swap on drag |

## Why two renderers, and why that matters

These are genuinely different products that share one pipeline:

- **Photosphere** needs the photos *stitched* into a single seamless image, then
  projected onto the inside of a sphere. Real 3D-ish look-around, but stitching
  can fail.
- **Object spin** needs no stitching at all. Frames are swapped as you drag. The
  "3D" is an illusion, it is fully deterministic, and it essentially cannot fail.

Because stitching is the one step that can genuinely fail on real photos, the
system is designed so that **a failed stitch degrades into a working spin viewer
rather than into an error screen.** See `backend/internal/service/manifest.go`.

## Components

```
   Browser / phone camera
            │  multipart photo upload
            ▼
   ┌──────────────────────┐        ┌──────────────┐
   │   Go + Fiber API     │───────▶│  PostgreSQL  │  captures, frames, jobs
   │  captures, uploads,  │        └──────────────┘
   │  manifest, WS hub    │───────▶┌──────────────┐
   └──────────┬───────────┘        │    MinIO     │  originals + derivatives
              │ XADD               └──────────────┘
              ▼
   ┌──────────────────────┐
   │  Redis Streams       │  job queue + pub/sub for live progress
   └──────────┬───────────┘
              │ XREADGROUP
              ▼
   ┌──────────────────────┐
   │  Python CV worker    │  EXIF, normalise, align, stitch, encode
   └──────────┬───────────┘
              │ HTTP callback
              ▼
       API → WebSocket → client progress bar
```

### Deviation from the original spec: Kafka → Redis Streams

The original plan specified Kafka. This build uses **Redis Streams** instead, and
the spec itself flagged Kafka as over-engineering for a single node. Redis
Streams provides everything this workload actually needs — durability, consumer
groups, at-least-once delivery, replay and a dead-letter stream — and Redis is
already in the stack for pub/sub, so it costs zero extra operational surface.

This is not a lock-in. Everything goes through the `queue.Queue` interface
(`backend/internal/queue/queue.go`). A Kafka implementation is a new file
implementing `Publish`, with no changes to any caller.

### Deviation: uploads are proxied, not presigned

The spec calls for presigned PUT URLs straight to MinIO. For **8–36 photos**
that trade is not worth it: presigned browser uploads require CORS configuration
on the object store and give the API no chance to validate the bytes. Photos are
therefore posted to the API, which streams them into MinIO.

`Store.PresignPut` is implemented and tested, so switching is a small change in
`service.AddPhoto` once frame counts get large enough to matter.

## Data model

- `captures` — one 360 session. Holds `status`, `frame_count`, `processed_count`
  (which drives the progress bar) and the final `manifest` as JSONB.
- `frames` — one photo. Carries `slot_id`, `yaw` and `pitch`, i.e. *which guided
  shot this was and where the phone was pointing*. That metadata is what lets
  the stitcher order the ring correctly even if the user shoots out of sequence.
- `jobs` — retry/attempt bookkeeping.

## The capture guidance engine

`backend/internal/domain/guidance.go` is the piece that answers "which photo
should the user take next". It produces a `Plan`: an ordered list of `Slot`s,
each with a plain-language `Label` ("Right side"), a `Hint` ("Turn a quarter turn
to your RIGHT"), an `Icon`, a `Group` and a `Yaw`/`Pitch`.

The order is deliberate and matches how people actually think about a room:
**front, right, behind, left** are `GroupCore` and required; **up and down** are
`GroupUpDown`; the four 45° in-between angles are `GroupExtra`. Both optional
groups come last, so a user who stops early still has an even ring rather than a
lopsided one.

The client uses `Yaw` with the phone's compass to draw a live turn arrow and
arms the shutter within `AlignTolerance` (15°) of the target.

### Blue-dot capture, and why the compass was the wrong sensor

The first version drew a text arrow ("turn RIGHT 40°") from
`DeviceOrientationEvent.alpha`, which is derived from the **magnetometer**.
Indoors that is wrong by tens of degrees, because steel, wiring and monitors all
pull it around. The arrows were never going to line up.

Google's Street View capture avoids the magnetometer for exactly this reason and
fuses the **gyroscope** with the accelerometer instead. `web/orientation.js` now
does the same, trying three sources best-first:

| Source | Sensors | Notes |
|---|---|---|
| `RelativeOrientationSensor` | gyro + accelerometer | preferred; no magnetic interference. Chrome/Android 69+ |
| `AbsoluteOrientationSensor` | + magnetometer | only if the relative one is missing |
| `DeviceOrientationEvent` | legacy Euler angles | the only option on iOS Safari |

All three are normalised to one **quaternion**, with screen rotation folded out,
so holding the phone in landscape does not rotate every target.

`web/sphere-math.js` turns that into the on-screen dots. A reference frame is
locked when the user sets Front; each slot becomes a fixed world direction; and
every animation frame that direction is rotated into device space and projected
through the camera's focal length to a pixel position. Turn the phone and the
dots slide across the screen and off the edge, because they are anchored to the
world rather than to the display.

A dot held inside the centre reticle (within 8°) for 550 ms fires the shutter by
itself. The hold requirement is what stops it firing mid-swing and capturing a
blurred frame. Targets behind the camera become an edge arrow pointing the
shortest way round.

The projection maths is covered by `web/tests/sphere-math.test.js` (24 assertions,
`node web/tests/sphere-math.test.js`) — turning right must centre the right dot,
pitching up must move it up, and behind-camera targets must report as not visible.

### What each photo now records

Frames carry the full rotation quaternion (`qx,qy,qz,qw`) plus
`orientation_source`, not just a yaw. Yaw alone cannot express roll and loses
precision near the poles.

### Stitching from known rotations

`cv-worker/ops/pose_stitch.py` uses those quaternions instead of rediscovering
the geometry from pixels. When at least 80% of a capture's photos carry a
rotation, each one is projected straight onto the sphere with
`cv2.PyRotationWarper("spherical")` and multi-band blended. Nothing is matched,
so **a blank wall places exactly as reliably as a bookshelf** - which is the
failure mode plain feature matching cannot escape.

Two coordinate changes are needed to get there, and both are easy to get wrong:

| From | To | Why |
|---|---|---|
| device frame (+Y up, camera along -Z) | OpenCV camera (+Y down, +Z forward) | `diag(1,-1,-1)` |
| sensor world (+Z up) | warper world (+Y down) | OpenCV's sphere spins about **Y** |

Without the second one every photo lands on a pole, where a sphere stretches
without limit: a single 65 degree view smears across the entire circumference
and the canvas explodes. That is caught by a size guard and by
`cv-worker/tests/test_pose_stitch.py`, which asserts that 30 degrees of yaw
moves a photo by exactly `2*pi*f/12` pixels and that a 65 degree view stays a
narrow slice.

If fewer than 80% of the photos have rotations, or the pose stitch fails, the
worker falls back to the coverage check plus feature matching described above.
Photos imported from a gallery carry no rotation at all - messaging apps strip
the metadata - so they always take the fallback path.

### Making the result a real photo sphere

A 2:1 equirectangular JPEG is just a wide photo until something tells a viewer
what it is. `cv-worker/ops/xmp.py` writes a **GPano XMP** packet into the
panorama, so Google Maps, Google Photos, Facebook and any photo-sphere viewer
open it as a draggable 360 rather than a flat image.

All seven required properties are written (`ProjectionType`, the cropped-area
pair, the full-pano pair, and the left/top offsets), plus `UsePanoramaViewer`,
`StitchingSoftware` and `SourcePhotosCount`. The packet replaces any existing
XMP rather than stacking a second one, and any failure returns the original
bytes untouched - metadata must never cost us the panorama.

**`PoseHeadingDegrees` is only written when it is genuinely known.** It is
measured clockwise from *true north*, and the gyroscope path has no idea where
north is: `RelativeOrientationSensor` tracks change from wherever it started.
So the heading is emitted only when the reading came from a magnetometer-backed
source (`absolute` or `deviceorientation`). Inventing one would point Google
Maps at the wrong bearing, which is worse than omitting it.

One honest caveat: `finish_panorama` pads the strip to 2:1 by replicating the
top and bottom rows, and the metadata declares that padded image as a full
sphere. The sky and floor in a finished photo sphere are therefore extended
pixels, not photographed ones.

### Verifying the user actually turned

`MinRequired` is 4, and four photos only make a 360 if they face four *different*
directions. So `AngularDistance` compares each incoming photo's heading against
every photo already stored, and the server rejects one that lands within
`DuplicateTolerance` (35°) of an existing shot — with a message naming the shot
it clashed with. Re-uploading the same index is always allowed, because that is
a retake.

This check only runs when the client sets `has_heading=true`. Without a compass
there is nothing to compare, and guessing would be worse than not checking.

## Status lifecycle

```
draft ──photo──▶ uploading ──process──▶ queued ──first frame──▶ processing
                                                                    │
                                    ┌───────────────────────────────┤
                                    ▼                               ▼
                             ready (stitched)            partial (degraded,
                                                          frame viewer, with
                                                          a reason for the user)
                                    │
                                    ▼
                             failed (every photo unusable)
```

`partial` is a deliberate success state, not an error.

## Live progress

The worker calls back to the API after each frame. The API writes an event to
Redis Pub/Sub on `orbit:capture:{id}`; every API instance bridges that channel
into its local WebSocket subscribers. So progress works correctly with more than
one API replica, and a client that connects late is immediately sent the current
state rather than waiting for the next tick.
