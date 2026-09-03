# Deploy Orbit with Render and Vercel

> Looking for a deployment that costs nothing? See
> [DEPLOY-FREE.md](DEPLOY-FREE.md), which avoids the paid Render instances and
> the billing-enabled Google Cloud project this guide assumes.

This guide covers two deployment choices:

- **Docker:** run PostgreSQL, Redis, MinIO, the API, and the CV worker together
	with the repository's `docker-compose.yml`.
- **Render + Vercel + Google Cloud Run:** run the API on Render, the CV worker
	on a Google Cloud Run Worker Pool, and the static web client on Vercel.
	Render does not run this Compose stack for you.

The Render/Vercel setup needs provider-generated URLs and credentials, so those
values cannot be known before you create the services. Every value below that
starts with `REPLACE_` must be copied from the relevant provider dashboard.

## Fastest working setup: Docker

This is the only path with concrete local values already defined by the
repository. From `/home/vishal/Orbit` run:

```bash
docker compose up --build -d
docker compose ps
curl http://localhost:8080/health
```

Open `http://localhost:8080`. The API serves the web client, and the API creates
the `orbit-private` and `orbit-public` MinIO buckets automatically.

The Docker service environment is:

```env
PORT=8080
PUBLIC_BASE_URL=http://localhost:8080
DATABASE_URL=postgres://orbit:orbit@postgres:5432/orbit?sslmode=disable
REDIS_ADDR=redis:6379
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=orbitadmin
MINIO_SECRET_KEY=orbitadmin123
MINIO_USE_SSL=false
BUCKET_PRIVATE=orbit-private
BUCKET_PUBLIC=orbit-public
JWT_SECRET=dev-only-change-me
MAX_UPLOAD_MB=25
WEB_DIR=/app/web
```

The worker uses:

```env
REDIS_HOST=redis
REDIS_PORT=6379
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=orbitadmin
MINIO_SECRET_KEY=orbitadmin123
MINIO_USE_SSL=false
BUCKET_PRIVATE=orbit-private
BUCKET_PUBLIC=orbit-public
API_BASE_URL=http://api:8080
```

These are Docker-internal values. Do not copy `postgres:5432`, `redis:6379`,
or `minio:9000` into a process running directly on your host; use the host
ports `5433`, `6380`, and `9010` instead.

Stop the Docker deployment with:

```bash
docker compose down
```

Delete all local database and image data with:

```bash
docker compose down -v
```

## Render + Cloud Run + Vercel deployment

For this deployment:

- **Render Web Service:** Go API and WebSocket server
- **Google Cloud Run Worker Pool:** Python CV worker in Docker
- **Render PostgreSQL:** application database
- **Redis-compatible service:** job queue and progress events
- **S3-compatible object storage:** uploaded and processed images
- **Vercel:** static web client

> Orbit currently has no authentication, rate limiting, or per-user quotas. Do not expose this deployment publicly until those protections are added, or put it behind an access-control layer.

## Important hosting constraint

The CV worker is not a request-driven serverless function. It continuously
consumes Redis jobs, peaking around 380 MiB while stitching phone-sized photos. Do not deploy it
as a normal Cloud Run service: request-driven services can scale to zero and
are subject to request lifetimes. Use a Cloud Run Worker Pool with at least
2 GiB memory, configured to keep a worker running. The worker needs outbound
network access to the Redis service, S3-compatible storage, and public Render
API. It does not need inbound HTTP access.

Render's filesystem is ephemeral. Do not use a local MinIO container or local disk for permanent photos. Use an external S3-compatible service such as Cloudflare R2, AWS S3, or another provider.

## 1. Prepare the repository

Push this repository to GitHub or another Git provider supported by Render and Vercel. Keep the repository root as the project root; the `backend`, `cv-worker`, and `web` directories are all used by the deployment.

The backend needs a production web directory path. Since Vercel serves the frontend separately, set `WEB_DIR` to a path that does not exist, for example `/var/empty/orbit-web`. The API does not need to serve the frontend when it is deployed separately.

## 2. Create PostgreSQL on Render

In the Render dashboard:

1. Create a PostgreSQL database.
2. Copy its **internal connection string**.
3. Use that value as `DATABASE_URL` on the Render API service. The Cloud Run
	worker does not connect to PostgreSQL.

Run the migrations after the database is available:

```bash
# From a machine that has psql installed; use the Render external connection string here.
psql "$DATABASE_URL" -f backend/migrations/001_init.sql
psql "$DATABASE_URL" -f backend/migrations/002_quaternion.sql
```

Use the internal connection string in Render service environment variables. Never commit a database URL or password to Git.

## 3. Create Redis

Orbit uses Redis Streams. Provide a Redis-compatible service that gives the Render services a reachable host and port. Render Key Value or an external Redis provider can be used if it supports Redis Streams.

For the API, set:

```env
REDIS_ADDR=<redis-host>:<redis-port>
```

For the worker, set the separate variables:

```env
REDIS_HOST=<redis-host>
REDIS_PORT=<redis-port>
```

If the Redis provider requires TLS or a password, set `REDIS_URL` instead on both the API and the worker:

```env
REDIS_URL=rediss://default:<password>@<host>:<port>
```

`REDIS_URL` overrides the host/port variables on both services.

## 4. Create S3-compatible object storage

Create two buckets:

```text
orbit-private
orbit-public
```

Configure the storage provider so objects in `orbit-public` can be read by the URLs generated by the API. With Cloudflare R2, this normally means configuring a public custom domain or public bucket access. Do not make the private bucket public.

You need these values:

- S3 endpoint, including its port if required, but without `https://`
- access key
- secret key
- whether TLS is used

Example:

```env
MINIO_ENDPOINT=<s3-endpoint>
MINIO_ACCESS_KEY=<access-key>
MINIO_SECRET_KEY=<secret-key>
MINIO_USE_SSL=true
BUCKET_PRIVATE=orbit-private
BUCKET_PUBLIC=orbit-public
```

Although the variables are named `MINIO_*`, the backend and worker use the MinIO S3-compatible client, so they can point at compatible object storage.

## 5. Deploy the Render API service with Docker

Create a **Web Service** in Render:

- **Root Directory:** leave empty
- **Environment:** Docker
- **Dockerfile Path:** `backend/Dockerfile`
- **Docker Context:** repository root (`.`)
- **Health Check Path:** `/health`
- **Region:** use the same region as PostgreSQL and Redis

Set these environment variables on the API service:

```env
PUBLIC_BASE_URL=https://REPLACE_API_SERVICE.onrender.com
DATABASE_URL=REPLACE_RENDER_POSTGRES_INTERNAL_URL
REDIS_ADDR=REPLACE_REDIS_HOST:REPLACE_REDIS_PORT
MINIO_ENDPOINT=REPLACE_S3_ENDPOINT
MINIO_ACCESS_KEY=REPLACE_S3_ACCESS_KEY
MINIO_SECRET_KEY=REPLACE_S3_SECRET_KEY
MINIO_USE_SSL=true
BUCKET_PRIVATE=orbit-private
BUCKET_PUBLIC=orbit-public
JWT_SECRET=REPLACE_LONG_RANDOM_SECRET
MAX_UPLOAD_MB=25
WEB_DIR=/var/empty/orbit-web
```

Do not set `PORT` in Render. Render injects the port automatically. The Docker
image listens on port `8080`; set Render's service port to `8080` if the
dashboard asks for one. `PUBLIC_BASE_URL` must be the exact public Render API
URL because share-manifest image URLs are built from it.

After deploy, verify:

```bash
curl https://<your-api-service>.onrender.com/health
```

Expected response contains:

```json
{"status":"ok","service":"orbit-api"}
```

## 6. Deploy the CV worker on Google Cloud Run

Enable Cloud Run and Artifact Registry in a Google Cloud project, and select a
region close to the Render API, Redis, and S3-compatible storage:

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \\
	cloudbuild.googleapis.com
gcloud config set project REPLACE_GCP_PROJECT_ID
gcloud artifacts repositories create orbit \\
	--repository-format=docker \\
	--location=REPLACE_GCP_REGION
```

Build the worker image from the repository root and push it to Artifact
Registry:

```bash
gcloud builds submit \\
	--tag REPLACE_GCP_REGION-docker.pkg.dev/REPLACE_GCP_PROJECT_ID/orbit/cv-worker:latest \\
	--file=cv-worker/Dockerfile .
```

Create a Cloud Run Worker Pool (or choose **Worker pools** in the Cloud Run
console) using that image. Configure at least **2 GiB memory**, one CPU, and a
worker count of 1 for the first deployment. Keep the pool running; do not use
scale-to-zero for this Redis Streams consumer.

Set these environment variables on the worker pool:

```env
REDIS_HOST=REPLACE_REDIS_HOST
REDIS_PORT=REPLACE_REDIS_PORT
MINIO_ENDPOINT=REPLACE_S3_ENDPOINT
MINIO_ACCESS_KEY=REPLACE_S3_ACCESS_KEY
MINIO_SECRET_KEY=REPLACE_S3_SECRET_KEY
MINIO_USE_SSL=true
BUCKET_PRIVATE=orbit-private
BUCKET_PUBLIC=orbit-public
API_BASE_URL=https://REPLACE_API_SERVICE.onrender.com
TARGET_WIDTH=1600
THUMB_WIDTH=500
JPEG_QUALITY=85
```

`REDIS_HOST` and `REDIS_PORT` must be reachable from Cloud Run. Do not use a
Render-internal hostname or port; use a Redis provider endpoint that permits
Cloud Run egress. The S3 endpoint must also be externally reachable. If the
Redis provider requires TLS or a password, set `REDIS_URL` instead of the
host/port pair.

Watch the Cloud Run Worker Pool logs while processing a test capture through
the Render API. A worker pool that scales to zero, restarts, or is killed for
memory will leave captures stuck in `processing` until the API reaper handles
them. Confirm the pool remains active and inspect failures in Cloud Logging.

## 7. Deploy the web client to Vercel

Create a new Vercel project from the same repository:

- **Framework Preset:** Other
- **Root Directory:** `web`
- **Build Command:** leave empty
- **Output Directory:** `.`, or leave the default if Vercel accepts the static directory
- **Install Command:** leave empty

The web client is plain HTML, CSS, and JavaScript; it has no npm build step.

Because `web/config.js` derives the API URL from the page origin, Vercel needs one small configuration file so the frontend knows the Render API. Add `web/vercel-config.js` only if you choose to override the default browser-derived API URL, or use a Vercel rewrite/proxy. The simplest deployment is to make the Render API the frontend origin by serving `web/` from the API, but that does not use Vercel.

For a separate Vercel frontend, update `web/config.js` to support a Vercel environment value, then set that value in Vercel. The current file has no Vercel environment-variable hook, so a separate Vercel deployment will otherwise call the Vercel domain instead of the Render API.

A minimal change is:

```js
window.ORBIT_API_BASE = window.ORBIT_API_BASE || "https://<your-api-service>.onrender.com";
window.ORBIT_WS_BASE = window.ORBIT_WS_BASE || "wss://<your-api-service>.onrender.com";
```

Place those assignments before the existing `window.ORBIT_API_BASE` and `window.ORBIT_WS_BASE` assignments in `web/config.js`, commit, and redeploy Vercel. The backend already allows cross-origin requests, and WebSocket connections use `wss://` on HTTPS.

Then open:

```text
https://<your-vercel-project>.vercel.app
```

Camera access works because Vercel provides HTTPS.

## 8. Test the complete deployment

Run these checks:

```bash
curl https://<your-api-service>.onrender.com/health
```

Then in the Vercel site:

1. Create a capture.
2. Upload at least four photos.
3. Start processing.
4. Confirm the Cloud Run Worker Pool logs show frame jobs.
5. Confirm the capture becomes `ready` or `partial`.
6. Open the generated share link and verify the images load.

If the capture remains `queued`, check Redis connectivity and the worker logs. If it remains `processing`, check worker memory and the API logs. If images are broken, check the public bucket policy and `MINIO_ENDPOINT`/bucket settings.

## Environment variable reference

### API service

| Variable | Example |
|---|---|
| `PORT` | `10000` |
| `PUBLIC_BASE_URL` | `https://orbit-api.onrender.com` |
| `DATABASE_URL` | Render PostgreSQL internal URL |
| `REDIS_ADDR` | `redis-host:6379` |
| `MINIO_ENDPOINT` | `account.r2.cloudflarestorage.com` |
| `MINIO_ACCESS_KEY` | provider access key |
| `MINIO_SECRET_KEY` | provider secret key |
| `MINIO_USE_SSL` | `true` |
| `BUCKET_PRIVATE` | `orbit-private` |
| `BUCKET_PUBLIC` | `orbit-public` |
| `JWT_SECRET` | long random value |
| `MAX_UPLOAD_MB` | `25` |
| `WEB_DIR` | `/var/empty/orbit-web` |

### Worker service

| Variable | Example |
|---|---|
| `REDIS_HOST` | `redis-host` |
| `REDIS_PORT` | `6379` |
| `MINIO_ENDPOINT` | `account.r2.cloudflarestorage.com` |
| `MINIO_ACCESS_KEY` | provider access key |
| `MINIO_SECRET_KEY` | provider secret key |
| `MINIO_USE_SSL` | `true` |
| `BUCKET_PRIVATE` | `orbit-private` |
| `BUCKET_PUBLIC` | `orbit-public` |
| `API_BASE_URL` | `https://orbit-api.onrender.com` |

## Recommended production topology

For a low-cost deployment, use Render for the API and PostgreSQL, an external
Redis service, external S3-compatible storage, and a Cloud Run Worker Pool for
the worker. Cloud Run pricing and free-tier eligibility vary by region and
resource usage. Vercel can host the static frontend, but the frontend's API
and WebSocket base URLs must be configured as described above.
