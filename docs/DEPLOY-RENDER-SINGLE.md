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

If you do hit an OOM, Render logs exit code 137. Lower `TARGET_WIDTH` (to 1200,
say) before considering a paid instance — memory scales with pixel count.

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
