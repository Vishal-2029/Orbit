#!/usr/bin/env bash
# Assemble a Hugging Face Space directory that runs the Orbit CV worker.
#
# A Space builds only from its own repo, so the worker source is copied in
# alongside a Space-flavoured Dockerfile and README. Secrets are NOT written
# here — set them in the Space's Settings > Variables and secrets.
#
# Usage: scripts/make-hf-space.sh [output-dir]     (default: ../orbit-cv-space)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
OUT=${1:-"$ROOT/../orbit-cv-space"}

mkdir -p "$OUT"
rm -rf "$OUT/ops" "$OUT/worker.py" "$OUT/config.py" "$OUT/requirements.txt"
cp -r cv-worker/ops cv-worker/worker.py cv-worker/config.py cv-worker/requirements.txt "$OUT/"
find "$OUT" -name __pycache__ -type d -prune -exec rm -rf {} +

# Spaces build from the repo root, so paths lose the cv-worker/ prefix.
cat > "$OUT/Dockerfile" <<'EOF'
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEALTH_PORT=7860

RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./

EXPOSE 7860
ENTRYPOINT ["python", "worker.py"]
EOF

cat > "$OUT/README.md" <<'EOF'
---
title: Orbit CV Worker
emoji: 🛰️
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

Background CV worker for Orbit. It consumes the `orbit:jobs` Redis stream and
writes results to S3-compatible storage; it serves no UI. The port 7860
endpoint returns `{"status":"ok"}` and exists only to satisfy the Space
runtime and to keep the container awake.

Configure `REDIS_URL`, `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`,
`MINIO_USE_SSL`, `BUCKET_PRIVATE`, `BUCKET_PUBLIC`, and `API_BASE_URL` under
Settings > Variables and secrets.
EOF

# A git repo, ready to push. The script stops short of pushing because that
# needs the operator's own Hugging Face credentials, and the remote is theirs to
# name - but everything up to that point is done here rather than left as a list
# of commands to retype.
if [ ! -d "$OUT/.git" ]; then
	git -C "$OUT" init -q
fi
cat > "$OUT/.gitignore" <<'EOF'
__pycache__/
*.pyc
.venv/
EOF
git -C "$OUT" add -A
if ! git -C "$OUT" diff --cached --quiet; then
	git -C "$OUT" -c user.email=orbit@local -c user.name=orbit 		commit -q -m "orbit cv worker"
fi

cat <<NEXT

Space assembled and committed in:
  $OUT

Why a Space: the worker needs memory. A stitch holds every source photo at once,
so a 30-photo capture wants well over a gigabyte, and a 512MB instance shared
with the API cannot do it at any setting. A free Space has 16GB.

To publish it:

  1. huggingface.co > New Space > SDK "Docker", hardware "CPU basic (free)"

  2. cd $OUT
     git remote add origin https://huggingface.co/spaces/<user>/<space>
     git push -u origin main

  3. In the Space: Settings > Variables and secrets

     Secrets (not Variables - a free Space's code is public):
       REDIS_URL           the EXTERNAL rediss:// url, with its password
       MINIO_ACCESS_KEY
       MINIO_SECRET_KEY

     Variables:
       MINIO_ENDPOINT      host only, no https://
       MINIO_USE_SSL       true
       BUCKET_PRIVATE      orbit-private
       BUCKET_PUBLIC       orbit-public
       API_BASE_URL        https://<your-api>.onrender.com

  4. On the API service, remove the worker from the container: it should run
     the API alone now. Redeploy.

  5. Rebuild anything that failed for memory:
       scripts/rescue-captures.py --api https://<your-api>.onrender.com --apply

Full walkthrough, including the Redis and R2 setup: docs/DEPLOY-FREE.md
NEXT
