# Deploy Orbit live for free

The free deployment of Orbit, as actually built:

| Piece | Service | Notes |
|---|---|---|
| API + WebSocket | Render Web Service (Docker, Free) | `Orbit` |
| Database | Render PostgreSQL (Free) | `Orbit-db` |
| Job queue | Render Key Value / Valkey (Free) | `orbit-redis` |
| Photo storage | Cloudflare R2 | 10 GB free |
| CV worker | Hugging Face Space (Docker, free CPU) | 16 GB RAM |
| Web client | Vercel | Hobby |

Everything Render can host is on Render. The two exceptions are forced by the
platform, not by preference:

- **Storage is on R2** because Render has no generally available object storage,
	and the MinIO template needs a persistent disk, which free instances cannot
	attach. Anything written to a free instance's filesystem is lost on restart.
- **The worker is on a Space** because it is a long-lived Redis Streams consumer
	and a free Render web service sleeps after 15 minutes without inbound traffic,
	which nothing ever sends to a queue consumer. Render Background Workers are
	paid-only.

	**If you would rather keep everything on Render, you can:** running the worker
	inside the API container measures at 378 MiB peak under a 512 MiB cap, so it
	fits the free instance, and it removes the Space, the external Redis URL and
	its allowlist. See [DEPLOY-RENDER-SINGLE.md](DEPLOY-RENDER-SINGLE.md) — that
	is now the simpler path, and this guide remains the one that keeps the worker
	isolated from the API.

## Free-tier limits to know

- **The free Render Postgres is deleted 30 days after creation** (plus a 14-day
	grace period). Recreate it and re-run the migrations before then, or move the
	database to a provider whose free tier does not expire. Captures created
	before the rebuild are lost.
- **Free Key Value has no persistence.** A restart empties the queue. In-flight
	captures are then stuck in `processing` until the API reaper fails them;
	finished captures are unaffected because their results live in R2 and
	Postgres.
- **The API sleeps after ~15 minutes idle** and takes 50+ seconds to answer the
	first request afterwards.
- **Orbit has no authentication, rate limiting, or quotas.** Anyone with the URL
	can upload and consume your R2 storage. Keep the URL private.

## Already done

Steps 1-5 of `DEPLOY-RENDER-VERCEL.md` cover the Render API, Render Postgres,
Render Key Value, and the R2 buckets. If the API answers, those are correct:

```bash
curl https://REPLACE_API_SERVICE.onrender.com/health
# {"status":"ok","service":"orbit-api"}
```

The API exits at boot if it cannot reach Postgres or Redis, so a healthy
response proves both connections work.

## 1. Let the worker reach Redis

The worker runs outside Render, so it needs the Key Value **external** URL. By
default a Key Value instance is not reachable at that URL.

1. Open `orbit-redis` in the Render dashboard.
2. Under **Access Control**, allow external connections. To reach it from a
	 Hugging Face Space you need `0.0.0.0/0`, because Space egress IPs are not
	 fixed. This exposes the queue to anyone holding the URL, so treat that URL as
	 a secret.
3. Copy the **external connection string**. It looks like:

```text
rediss://red-xxxxx:PASSWORD@virginia-keyvalue.render.com:6379
```

Both services now accept a full URL:

```env
REDIS_URL=rediss://red-xxxxx:PASSWORD@virginia-keyvalue.render.com:6379
```

`REDIS_URL` carries the TLS and password that `REDIS_ADDR` / `REDIS_HOST` +
`REDIS_PORT` cannot express, and it overrides them when both are set. **Leave
the API on its internal Redis connection** — internal traffic is faster, free,
and stays off the public internet. Only the worker uses the external URL.

## 2. Build the Space directory

A Space builds only from its own repository, so the worker source has to be
copied into one:

```bash
scripts/make-hf-space.sh
```

That writes `../orbit-cv-space` containing the worker code, a Space Dockerfile,
and a README carrying the `sdk: docker` / `app_port: 7860` front matter the
Space runtime requires.

## 3. Create and push the Space

On huggingface.co, create a **new Space** with SDK **Docker** and hardware
**CPU basic (free)**. Then:

```bash
cd ../orbit-cv-space
git init && git add -A && git commit -m "orbit cv worker"
git remote add origin https://huggingface.co/spaces/REPLACE_USER/REPLACE_SPACE
git push -u origin main
```

In **Settings > Variables and secrets**, add:

```env
REDIS_URL=REPLACE_RENDER_KEY_VALUE_EXTERNAL_URL
MINIO_ENDPOINT=REPLACE_ACCOUNT_ID.r2.cloudflarestorage.com
MINIO_ACCESS_KEY=REPLACE_R2_ACCESS_KEY
MINIO_SECRET_KEY=REPLACE_R2_SECRET
MINIO_USE_SSL=true
BUCKET_PRIVATE=orbit-private
BUCKET_PUBLIC=orbit-public
API_BASE_URL=https://REPLACE_API_SERVICE.onrender.com
```

Put `REDIS_URL` and the two R2 keys in **Secrets**, not Variables — a free
Space's repository is public, though its secrets are not. `MINIO_ENDPOINT` must
be a bare host: no `https://`, no path.

`HEALTH_PORT=7860` is already set by the generated Dockerfile. It makes the
worker serve a small `/health` endpoint, which is how the Space runtime knows
the container is alive. The worker still does all its real work over Redis and
never receives inbound job traffic.

A healthy Space log looks like:

```text
[orbit-worker] starting, redis=url minio=... api=...
[orbit-worker] health server listening on :7860
```

## 4. Keep both services awake

Point a free uptime monitor (UptimeRobot, cron-job.org) at two URLs every 10
minutes:

- `https://REPLACE_SPACE_URL.hf.space/` — stops the Space pausing for inactivity
- `https://REPLACE_API_SERVICE.onrender.com/health` — keeps the API from
	sleeping, so first requests stop taking a minute

## 5. Web client on Vercel

Create a Vercel project from this repository:

- **Framework Preset:** Other
- **Root Directory:** `web`
- **Build / Install Command:** leave empty
- **Output Directory:** `.`

The client is plain HTML/CSS/JS with no build step.

The frontend is on a different origin than the API, so tell it where the API
lives. Edit `web/api-config.js`, uncomment both lines, and fill in your Render
URL:

```js
window.ORBIT_API_BASE = "https://REPLACE_API_SERVICE.onrender.com";
window.ORBIT_WS_BASE  = "wss://REPLACE_API_SERVICE.onrender.com";
```

Commit and redeploy. `index.html` already loads that file before `config.js`,
and `config.js` keeps any value already set, so nothing else changes. The
backend allows cross-origin requests, and WebSockets use `wss://` over HTTPS.

Camera capture works because Vercel serves the site over HTTPS.

> The Render API returns 404 at `/` by design: `WEB_DIR` points at a
> nonexistent path because Vercel serves the frontend. Only `/health` and the
> `/api/v1/...` routes answer there.

## 6. Test end to end

Open `https://REPLACE_PROJECT.vercel.app` and:

1. Create a capture.
2. Upload at least four photos.
3. Start processing.
4. Watch the Space logs show frame jobs.
5. Confirm the capture reaches `ready` or `partial`.
6. Open the share link and check the images load.

### If something is stuck

| Symptom | Cause to check |
|---|---|
| Capture stays `queued` | The worker is not reaching Redis. Confirm external connections are enabled on `orbit-redis` and that the Space's `REDIS_URL` is the **external** URL, not the internal one. |
| Worker logs a connection timeout | The Access Control allowlist does not include the Space. It needs `0.0.0.0/0`. |
| Capture stays `processing` | The worker died mid-job. Check the Space logs for a memory error or traceback. |
| Images broken in the share link | `orbit-public` is not publicly readable, or `PUBLIC_BASE_URL` / `MINIO_ENDPOINT` is wrong. |
| First request takes ~60 s | The Render free instance is waking. Expected; step 4 hides it. |
| Space shows a configuration error | It is not listening on 7860. Confirm `HEALTH_PORT=7860` and `app_port: 7860` in the Space README front matter. |
| Everything breaks after ~30 days | The free Render Postgres expired and was deleted. See the limits section above. |

## Local development is unchanged

`docker compose up --build -d` still runs the whole stack locally against plain
Redis and MinIO. See `docs/DEPLOY.md`.
