# Deploy Orbit as one Render service

Runs the Go API and the Python CV worker in a single container, so the whole
backend is one Render service alongside Render Postgres and Render Key Value.

Compared with hosting the worker separately, this removes:

- the Hugging Face Space and its duplicate set of credentials
- external connections on Key Value, and the `0.0.0.0/0` allowlist they need —
	**the worker now reaches Redis over Render's internal network**
- the uptime monitor that kept the Space awake

## Memory: this fits on the free instance

Measured on this image, one capture at a time:

| Workload | Peak RSS |
|---|---|
| Idle (API + worker, both running) | ~139 MiB |
| 12 photos at 1200x900, `TARGET_WIDTH=1600` | 370 MiB |
| 12 photos at 4032x3024 (phone-sized), `TARGET_WIDTH=1600` | **378 MiB** |

Both stitches were run in a container capped at `--memory=512m`, matching a free
Render instance. Neither was OOM-killed, and both produced a full-quality,
non-degraded sphere. Source photo size barely moves the peak, because each frame
is resized to `TARGET_WIDTH` early, before anything expensive happens.

That leaves roughly 130 MiB of headroom, so the risks worth knowing are:

- **Concurrent captures.** The measurements are sequential. Two people
	processing at once roughly doubles the stitch's share.
- **Many more frames.** 12 photos was the test; a much larger ring holds more
	decoded frames in memory at once.

### If you hit an OOM

Render logs exit code 137 and restarts the instance. **Lowering `TARGET_WIDTH`
is not the fix** — measured on a 16-photo ring, dropping it from 1600 to 1024
saved only 39 MiB, because the stitch's cost is driven by how many photos there
are, not how wide each one is.

The effective lever is the stitcher's compositing resolution. OpenCV composites
at the input resolution by default, allocating a full-size warped image and mask
per photo, and that single step is the worker's largest allocation:

| 16 frames at 1600px | Peak |
|---|---|
| Frames loaded, before stitching | 206 MiB |
| Stitch, OpenCV default | 377 MiB |
| `STITCH_COMPOSITING_MP=1.2` | 301 MiB |
| `STITCH_COMPOSITING_MP=0.8` | 276 MiB |

**The worker sets this for itself.** It reads the container's cgroup memory
limit at startup and caps compositing at 1.2 Mpx when that limit is 768 MiB or
less, so a free instance is capped and a larger one keeps full resolution. There
is no env var to remember. Override with `STITCH_COMPOSITING_MP` if you want a
different value; an explicit setting always wins.

Confirm what it chose from the first lines of the deploy log:

```text
[orbit-worker] memory limit=512 MiB, target_width=1600, compositing=1.2 Mpx (auto)
```

End to end in the combined container under a hard 512 MiB cap, the same capture
peaks at 434 MiB uncapped and 354 MiB capped — 78 MiB of headroom against 157.

The trade is a smaller finished panorama (roughly 4214x952 rather than
5329x1204). If you want full resolution and no memory ceiling, move the worker
to a Hugging Face Space as described in [DEPLOY-FREE.md](DEPLOY-FREE.md); a free
Space has 16 GB and this setting can stay at its default there.

## 1. Point the service at the combined Dockerfile

In the Render web service's settings:

- **Dockerfile Path:** `deploy/Dockerfile.combined`
- **Docker Context:** `.` (repository root)
- **Health Check Path:** `/health`

The image builds the Go binary with `CGO_ENABLED=0` so the static binary runs on
the Debian-based Python image that OpenCV needs. `deploy/start-combined.sh`
starts both processes and stops the container if either one exits, so Render
restarts a half-dead instance instead of serving one where captures silently
never process.

## 2. Set the environment

One service means one combined set of variables. The worker reads the same
`MINIO_*` values as the API, so nothing is duplicated:

```env
PUBLIC_BASE_URL=https://REPLACE_API_SERVICE.onrender.com
DATABASE_URL=REPLACE_RENDER_POSTGRES_INTERNAL_URL
REDIS_URL=REPLACE_RENDER_KEY_VALUE_INTERNAL_URL
MINIO_ENDPOINT=REPLACE_ACCOUNT_ID.r2.cloudflarestorage.com
MINIO_ACCESS_KEY=REPLACE_R2_ACCESS_KEY
MINIO_SECRET_KEY=REPLACE_R2_SECRET
MINIO_USE_SSL=true
BUCKET_PRIVATE=orbit-private
BUCKET_PUBLIC=orbit-public
JWT_SECRET=REPLACE_LONG_RANDOM_SECRET
MAX_UPLOAD_MB=25
WEB_DIR=/var/empty/orbit-web
```

Two of those are specific to this layout:

- **`API_BASE_URL=http://localhost:8080`** — the worker reports results back to
	the API. In one container that call never leaves the instance, which is faster
	than a round trip through Render's load balancer.
- **`REDIS_URL`** is the Key Value **internal** URL. Both processes use it, so
	you can leave external connections disabled.

Do not set `PORT`; Render injects it. Do not set `HEALTH_PORT` — that exists
only for hosts that require the worker itself to listen on a port.

## 3. Optionally drop Vercel too

Because this image already contains `web/`, the API can serve the frontend at
the same origin. Set:

```env
WEB_DIR=/app/web
```

Then the whole application is one URL, `web/api-config.js` stays commented out
(the browser-derived origin is now correct), and there is no cross-origin setup
at all. Keep Vercel instead if you would rather the frontend stay fast while the
backend is asleep.

## 4. Verify

```bash
curl https://REPLACE_API_SERVICE.onrender.com/health
```

The Render log should show both processes starting:

```text
[orbit-worker] starting, redis=url minio=... api=http://localhost:8080
```

Then run a real capture: upload four photos, start processing, and confirm the
same log stream shows frame jobs and the capture reaching `ready`.

### If something is stuck

| Symptom | Cause |
|---|---|
| Instance restarts during a stitch, exit code 137 | Out of memory. See the memory section above. |
| Capture stays `queued` | The worker process is not running. The log names whichever process exited first. |
| Container exits right after deploy | Either process failed at boot — usually a bad `DATABASE_URL`, `REDIS_URL`, or R2 credential. The API logs the reason and exits. |
| Deploys are slow | Expected: the image installs OpenCV. Only the Python layer rebuilds when backend code changes. |
