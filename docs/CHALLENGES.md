# What is genuinely hard about this, and what went wrong

An honest engineering account, not a sales sheet.

---

## 1. The hardest problem: a handful of handheld photos is thin for a clean sphere

**This is the central risk of the whole product.**

A phone's rear camera has roughly a 65–70° horizontal field of view.

**This matters a lot for the 4-photo minimum.** Four shots at 90° apart leaves
roughly a 20–25° *gap* between them — there is nothing to match on, so a seamless
sphere is usually impossible from four photos alone. Four is the minimum that
produces a genuine look-around in all directions, not the number that stitches
well. Eight (adding the in-between angles) gives ~20° of overlap and is where
stitching starts working reliably; that is exactly why the app keeps offering
the optional extras after the four cardinals.

Even with generous overlap, any of the following breaks the stitch:

- **The user walks instead of pivoting.** Standing still and turning is a pure
  rotation, which a stitcher can solve exactly. Walking in a small circle adds
  translation, and translation means *parallax* — near objects shift relative to
  far ones — which no panorama stitcher can correct. This is the number one
  cause of failure and it is a *user behaviour* problem, not a code problem.
- **Blank walls.** Feature matching needs texture. A plain painted wall or a
  clear sky has no features to match, so the stitcher cannot work out how two
  photos relate.
- **Auto-exposure drift.** The phone re-meters every shot, so the bright window
  side and the dark corner side come out at different brightness and the seam is
  visible even when the geometry is perfect.
- **Moving subjects.** A person walking through the overlap region gets stitched
  twice, or half of them disappears.

**How this is handled:** it is not "solved", it is *contained*. Three layers:

1. **Prevent** — the guidance engine (`domain/guidance.go`) orders the shots
   front / right / behind / left, arms the shutter only when the compass says
   the phone is within 15° of the target, and shows a ghost overlay of the
   previous photo so the horizon stays put. Tips explicitly say "pivot on the
   spot — do not walk in a circle".
   **The server also verifies it.** Each photo's compass heading is compared
   against every shot already taken, and one aimed within 35° of an existing
   shot is rejected at the shutter, naming the shot it clashed with. Without
   this, four photos of the same wall are accepted and the failure only shows
   up much later at stitch time, when the user can no longer fix it.
2. **Detect** — the worker maps OpenCV's stitcher status codes to plain English
   (`ERR_NEED_MORE_IMGS` → "not enough overlap, try 12 photos instead of 8").
3. **Degrade** — and this is the important design decision: **a failed stitch is
   not an error.** The capture goes to status `partial`, the manifest switches
   `renderer` to `frames`, and the user gets a working swipeable 360 built from
   the exact same photos, plus a friendly note explaining how to get a seamless
   one next time. A 360 with a seam beats an error screen every single time.

The alternative — real photogrammetry or neural view synthesis — needs 50–200
photos, a GPU, and minutes of compute per scene. That is a different product.

## 1b. The black bars and the line where the 360 joins itself

`cv2.Stitcher` warps every photo onto a shared curved surface, so its output is
never a neat rectangle. The ragged area outside the warp is filled with **pure
black**, which shows up as bars along the top and bottom. Worse, a sphere
texture is cyclic — the last column sits directly against the first — so if the
capture does not close a perfect circle, that join is a hard black line exactly
where the first and last photo meet.

`cv-worker/ops/finish.py` fixes both, in this order (the order matters, because
cropping and resizing both disturb the edge columns):

1. **Crop the padding.** The largest all-content rectangle is found with a
   maximal-rectangle-in-a-histogram sweep. If that crop would throw away more
   than 65% of the image — which happens on a ragged stitch — it is skipped and
   the leftover black is **inpainted** instead. A smeared edge beats a black
   wedge.
2. **Limit the width**, before any seam work, so resampling cannot reintroduce
   a step at the join.
3. **Trim the duplicate overlap.** A ring capture usually overshoots, so the
   same wall appears at both ends. A strip from the left edge is template-matched
   against the right edge; everything past a confident match is cut, leaving
   exactly one full turn.
4. **Cross-fade the join** and drop the opening columns, so the new last column
   and new first column are genuinely adjacent pixels rather than merely
   softened.
5. **Pad to 2:1** by extending the top and bottom rows — rows only, never
   columns, so the wrap survives.

Measured on a real stitch: pure-black pixels went from visible bars to
**0.000%**, and the wrap discontinuity dropped by more than 10x.

**What this cannot fix:** if the capture genuinely does not close the loop —
the user stopped three-quarters of the way round — the first and last photo show
different things. No blending can invent the missing quarter. The join becomes a
soft gradient instead of a hard line, which is the honest best available.

## 2. The compass is unreliable, so it can never be load-bearing

The web `DeviceOrientationEvent` compass is a mess in practice:

- **iOS requires an explicit permission prompt** triggered by a user gesture, and
  uses a non-standard `webkitCompassHeading` property.
- **Android** reports `alpha` relative to an arbitrary origin unless
  `absolute: true`, which not all devices honour.
- **Indoors, magnetometers are simply wrong** — steel frames, wiring and
  monitors throw the heading off by tens of degrees.
- **Desktop browsers have no sensor at all.**

**How this is handled:** the compass is treated as a *nicety, never a
requirement*. It drives an optional "turn right / on target" arrow and an
auto-arm hint. If it is missing, denied or nonsense, the app falls back to a
plain numbered checklist — "Shot 3 of 8: Right side" — and a manual shutter, and
everything still works. Yaw is stored per frame when available so the stitcher
can order the ring, but the stitcher also falls back to capture order.

## 3. Progress that lies

Naively incrementing `processed_count` breaks the moment a job is retried: an
at-least-once queue will deliver the same frame twice and the bar reads 110%.

**How this is handled:** `MarkFrameDone` never increments. It sets the frame row
to `done` and then *recounts* `SELECT count(*) ... WHERE status='done'` inside
the same transaction. Reprocessing the same frame ten times gives the same
number. The bar also stops at 80% when frames finish, because stitching is real
work that takes real time and a bar that sits at 100% while the user waits is
worse than one that admits there is a step left.

## 4. Knowing when everything is finished

Per-frame jobs and the finalize job go into the same stream, so finalize is
routinely dequeued *before* the frames it depends on are done.

**How this is handled:** the finalize job polls capture status until
`processed + failed == frame_count`, with a timeout. This is the least elegant
part of the system. The clean fix is a completion barrier — an atomic Redis
counter decremented per frame, with the last decrement enqueueing finalize — and
that is the first thing to change if frame counts grow.

## 5. Things that bit us during this build

| Problem | Cause | Fix |
|---|---|---|
| Port collisions | The machine already runs PostgreSQL 18 on 5432 | Infra moved to 5433 / 9010 / 6380 |
| Suspected `Exists()` bug on missing objects | We assumed `StatObject` might not return `NoSuchKey` | **Not a real bug.** Probed against live MinIO: `StatObject` does return `NoSuchKey`/404. Left unchanged, now covered by a test |
| `getUserMedia` silently unavailable | Camera access needs a **secure context**. `localhost` counts; a phone hitting the laptop's LAN IP over plain HTTP does **not** | The client detects this and explains it, rather than showing a dead black rectangle |
| A useless subquery in `RETURNING` | Careless first draft of `MarkFrameDone` | Removed |

## 6. What is deliberately *not* built

Being explicit so nothing here is a surprise:

- **No authentication.** Every capture is anonymous and public by default. The
  `users` table and the `user_id` column exist, and the config carries a
  `JWT_SECRET`, but no login flow is wired up. **Do not put this on the public
  internet as-is.**
- **No rate limiting or storage quotas.** Anyone who can reach the API can fill
  the disk.
- **No background removal.** `rembg` is a heavy optional dependency and is off by
  default; the setting is plumbed through but the model is not shipped.
- **No sprite sheets.** Worth adding for spin mode above ~24 frames.
- **The Flutter app is not built.** The working client is the zero-build web app
  in `web/`, which runs on a phone browser and uses the real camera. That was the
  right call for "I want to click pictures right now": Flutter Web needs an SDK
  build step and a Flutter mobile build needs Android Studio, a signing setup and
  a device, none of which get the user to a working camera any faster. The Go API
  is client-agnostic, so a Flutter client remains a straightforward addition.

## 7. Honest difficulty rating

| Piece | Difficulty | Why |
|---|---|---|
| Go API, DB, storage, queue, WebSocket | **Low–medium** | Well-trodden. Mostly careful plumbing. |
| Capture guidance engine | **Medium** | The maths is trivial; making the *wording* clear to a non-technical user is the actual work. |
| Camera UI with live compass | **Medium–high** | Browser sensor APIs are inconsistent and untestable headlessly. |
| Frame-swap 360 viewer | **Low** | ~200 lines. The modulo wrap-around is the only trap. |
| Sphere viewer | **Medium** | three.js on an inverted sphere; straightforward once the texture is right. |
| **Panorama stitching that reliably works** | **High** | Not a coding problem. It is a photo-quality problem, and it depends on a user standing still in a textured room. This is why the fallback exists. |
