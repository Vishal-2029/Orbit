# Running Orbit on your own machine

## What you need

Already installed on this machine and verified:

| Tool | Needed for | Your version |
|---|---|---|
| Docker | Postgres, MinIO, Redis | 29.7.2 ✅ |
| Go 1.22+ | the API | 1.26.5 ✅ |
| Python 3.10+ | the CV worker and the web server | 3.12.3 ✅ |

Nothing else. No npm, no bundler, no Flutter SDK.

---

## The short version

```bash
cd /home/vishal/Orbit
./scripts/dev.sh
```

That one command starts the database, object store, queue, API, CV worker and
web client together, waits for each to be healthy, and prints the URLs. Press
`Ctrl-C` to stop everything.

Then open **http://localhost:5173**.

---

## The long version, one piece at a time

Useful when something is broken and you want to see which piece.

### 1. Start the infrastructure

```bash
make up
```

Starts three containers and applies the database schema:

| Service | Port | Why not the default port |
|---|---|---|
| PostgreSQL | **5433** | you already run PostgreSQL 18 on 5432 |
| MinIO API | **9010** | 9000 is commonly taken |
| MinIO console | **9011** | browse your stored photos |
| Redis | **6380** | 6379 is commonly taken |

Check it worked:

```bash
docker compose ps
```

All three should say `running`.

### 2. Start the API

```bash
make api
```

Wait for `Orbit API listening on :8080`. In another terminal:

```bash
curl localhost:8080/health
# {"status":"ok","service":"orbit-api",...}
```

### 3. Set up and start the CV worker (first time only)

```bash
cd cv-worker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # takes a few minutes, OpenCV is large
cd .. && make worker
```

### 4. Start the web client

```bash
make web
```

Open **http://localhost:5173**.

---

## Using it from your phone (the part that matters)

The camera is the whole point, and your laptop probably doesn't have a good one.
But there is a catch you need to know about:

> **Browsers only allow camera access on a "secure context"** — that means
> `https://`, or `localhost`. If your phone opens `http://192.168.1.x:5173`,
> the camera **will not turn on**. This is a browser security rule, not a bug in
> Orbit.

Two ways around it, easiest first.

### Option A — Chrome flag (2 minutes, no installs)

1. Find your laptop's LAN IP:
   ```bash
   hostname -I | awk '{print $1}'
   ```
2. Start the API so it advertises that IP instead of localhost:
   ```bash
   PUBLIC_BASE_URL=http://192.168.1.x:8080 make api     # use your real IP
   ```
3. On your Android phone, open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`,
   add `http://192.168.1.x:5173`, set it to **Enabled**, and relaunch Chrome.
4. Open `http://192.168.1.x:5173` on the phone. The camera now works.

Your phone and laptop must be on the same Wi-Fi.

### Option B — a real HTTPS tunnel (works on iPhone too)

```bash
# install once: https://ngrok.com/download
ngrok http 5173
```

Open the `https://....ngrok-free.app` URL it prints, on any phone. Because it is
real HTTPS, the camera works with no flags. You will also need to point the
client at a tunnelled API — run a second tunnel for port 8080 and start the API
with `PUBLIC_BASE_URL=https://<that-url>`.

### Option C — just test on the laptop

Open `http://localhost:5173` and use your webcam. The guidance flow, upload,
processing, progress bar and viewer all work identically; only the photos are
worse.

---

## Trying it without a camera at all

To prove the pipeline end to end using generated images:

```bash
python3 scripts/make_test_photos.py scripts/testdata   # 8 overlapping test photos
bash scripts/e2e_test.sh                # runs the whole flow and prints the manifest
```

---

## Configuration

Every setting is an environment variable with a working default
(`backend/internal/config/config.go`):

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | API port |
| `PUBLIC_BASE_URL` | `http://localhost:8080` | **how the browser reaches the API.** Image URLs in the manifest are built from this — set it when using a LAN IP or a tunnel |
| `DATABASE_URL` | `postgres://orbit:orbit@localhost:5433/orbit?sslmode=disable` | |
| `REDIS_ADDR` | `localhost:6380` | |
| `MINIO_ENDPOINT` | `localhost:9010` | |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `orbitadmin` / `orbitadmin123` | dev credentials |
| `MAX_UPLOAD_MB` | `25` | per-photo size cap |
| `JWT_SECRET` | `dev-only-change-me` | unused until auth is added |

`PUBLIC_BASE_URL` is the one that catches people out. If your 360 loads but the
images are broken, this is why.

---

## Everyday commands

```bash
make help      # list everything
make up        # start infra
make down      # stop infra, keep data
make reset     # stop infra and DELETE all photos and captures
make test      # run the Go test suite
make logs      # tail infra logs
make build     # compile binaries into backend/bin/
```

Inspect storage:

```bash
cd backend
go run ./cmd/storagectl stats            # objects, bytes, captures by status
go run ./cmd/storagectl verify <id>      # check every photo actually exists
go run ./cmd/storagectl sweep            # dry run: find orphaned files
go run ./cmd/storagectl sweep --apply    # actually delete them
```

---

## When something breaks

| Symptom | Cause | Fix |
|---|---|---|
| `postgres: ... connection refused` | infra not started | `make up` |
| Camera shows a black box on the phone | not a secure context | see the phone section above |
| Stuck at "queued", progress never moves | CV worker isn't running | `make worker`, check `.logs/worker.log` |
| 360 loads but images are broken | `PUBLIC_BASE_URL` points at localhost while you browse from a phone | restart the API with the right value |
| "need at least 4 photos" | fewer than 4 uploaded | take front, right, behind and left |
| "This is pointing the same way as..." | you did not actually turn between shots | turn to face the direction named on screen, then shoot |
| Result is a swipeable sequence, not a smooth sphere | the stitch failed — see the banner for the reason | usually caused by walking instead of pivoting, or blank walls. See `docs/CHALLENGES.md` |
| Port already in use | something else on 5433/9010/6380 | edit `docker-compose.yml` and the matching env var |
