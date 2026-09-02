# Orbit — 360° capture, from your camera to a shareable link

Stand in one place. The app tells you exactly where to point your phone —
*"Right side. Turn a quarter turn to your RIGHT."* — you shoot **front, right,
behind, left** (plus up and down if you want), and you get back a draggable 360°
view with a share link.

There is a second mode for products: put an object on a turntable, orbit it, and
get an e-commerce style spin.

> The original design brief for this project is preserved at
> [`docs/ORIGINAL-SPEC.md`](docs/ORIGINAL-SPEC.md). This README describes what
> was actually built, which differs from that brief in a few places — each
> deviation is explained in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Start here

```bash
cd /home/vishal/Orbit
./scripts/dev.sh
```

Then open **http://localhost:5173**.

To use your phone's camera instead of a webcam, read the phone section in
[`docs/RUNNING.md`](docs/RUNNING.md) — there is a browser security rule that
will otherwise leave you looking at a black rectangle.

## Documentation

| Document | What's in it |
|---|---|
| [`docs/RUNNING.md`](docs/RUNNING.md) | **How to run it locally**, how to use it from your phone, config, troubleshooting |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the pieces fit, and why the stack deviates from the brief |
| [`docs/API.md`](docs/API.md) | Every endpoint, with real request and response bodies |
| [`docs/CHALLENGES.md`](docs/CHALLENGES.md) | **What's genuinely hard, what broke, and what is deliberately not built** |
| [`docs/STORAGE.md`](docs/STORAGE.md) | Storage layout, the two-bucket split, orphan cleanup |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | **Hosting it online for free**, and what to secure first |

## How the capture flow works

```
1. Pick a mode        Photosphere (stand still)  or  Object spin (turntable)
2. The app plans      Front, Right, Behind, Left  (then optional Up, Down and
                      the in-between angles), each with a plain-English label
3. You shoot          A live arrow says "turn RIGHT 40°"; the shutter turns green
                      when you're on target. A ghost of the last photo helps you
                      keep the horizon steady
4. Checked            The server compares each photo's compass heading against
                      the ones you already took and REJECTS a repeat of the same
                      direction, so you can't end up with four shots of one wall
5. Upload             Each photo posts with its slot, compass heading and tilt
6. Process            One job per photo, then a stitch attempt
7. Watch              A WebSocket streams per-frame progress
8. View & share       Drag to look around. Send anyone the /s/<slug> link
```

## What's in the box

```
Orbit/
├── backend/          Go + Fiber API — captures, uploads, manifest, WebSocket
│   ├── cmd/api/            the server
│   ├── cmd/storagectl/     storage stats, verify and orphan cleanup CLI
│   └── internal/domain/    the capture guidance engine
├── cv-worker/        Python — EXIF, normalise, align, stitch, encode
├── web/              Zero-build camera client + 360 viewer (no npm needed)
├── scripts/          dev.sh, test photo generator, end-to-end test
├── docs/             the documentation table above
└── docker-compose.yml   Postgres, MinIO, Redis
```

## The one design decision worth knowing

**Stitching handheld photos into a seamless sphere sometimes fails.** Blank
walls, changing light, or the user walking instead of pivoting will all defeat
any stitcher — that is a photography problem, not a software one.

So Orbit treats a failed stitch as a *degraded success*, never an error. The
capture lands in status `partial`, the manifest switches its renderer from
`sphere` to `frames`, and you still get a working swipeable 360 built from the
same photos — plus a plain-English note explaining how to shoot a better one.

A 360 with a visible seam beats an error screen.

## Status

| Area | State |
|---|---|
| Go API, Postgres, MinIO, Redis Streams, WebSocket progress | working, tested |
| Capture guidance engine | working, 14 unit tests |
| Storage layer + orphan sweeper + CLI | working, 10 integration tests |
| Python CV worker (normalise, align, stitch, degrade) | working |
| Web camera client + sphere/frame viewers | working |
| Authentication, rate limits, quotas | **not built — do not deploy publicly** |
| Flutter client | not built; the web client covers the same flow |

See [`docs/CHALLENGES.md`](docs/CHALLENGES.md) for the full list of what is and
isn't done, and why.
