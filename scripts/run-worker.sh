#!/usr/bin/env bash
# Run the CV worker on this machine against the hosted API.
#
# The worker only ever makes OUTBOUND connections - it pulls jobs from Redis,
# reads and writes objects in S3-compatible storage, and posts results back to
# the API. Nothing has to reach it, so it does not need a public address, a
# tunnel, or a hosting account. A desktop with real memory is a better host for
# it than a 512MB shared instance, and considerably better than a free Space
# with a one-at-a-time quota.
#
# This is not a workaround. Stitching is the memory-hungry part of this product
# and it is the part least suited to a small always-on box: it runs for a minute
# per capture and then does nothing. Running it where the memory already exists
# is the sensible arrangement, and it is what the deploy docs describe as the
# split-services option.
#
#   scripts/run-worker.sh                 # reads scripts/worker.env
#   scripts/run-worker.sh path/to/env
#
# Stop it with Ctrl-C. Captures queued while it is off are picked up when it
# starts again - Redis holds them.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

ENV_FILE=${1:-"$ROOT/scripts/worker.env"}

if [ ! -f "$ENV_FILE" ]; then
	cat >&2 <<MISSING
No environment file at:
  $ENV_FILE

Copy the template and fill in the five values from your hosts:

  cp scripts/worker.env.example scripts/worker.env
  \${EDITOR:-nano} scripts/worker.env

They are the same values the API service already uses. REDIS_URL must be the
EXTERNAL url - the internal one only resolves inside the host's network.
MISSING
	exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${REDIS_URL:?REDIS_URL is not set in $ENV_FILE}"
: "${MINIO_ENDPOINT:?MINIO_ENDPOINT is not set in $ENV_FILE}"
: "${API_BASE_URL:?API_BASE_URL is not set in $ENV_FILE}"

PY="$ROOT/cv-worker/.venv/bin/python"
if [ ! -x "$PY" ]; then
	echo "No virtualenv at $PY - run: cd cv-worker && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
	exit 1
fi

# A distinct name in the Redis consumer group, so this worker and any hosted one
# can run side by side without both claiming the same job.
export CONSUMER_NAME="${CONSUMER_NAME:-desktop-$(hostname -s)-$$}"

echo "worker : $CONSUMER_NAME"
echo "api    : $API_BASE_URL"
echo "memory : $(free -g | awk '/^Mem:/{print $2}') GB on this machine"
echo
echo "Leave this running while you capture. Ctrl-C to stop."
echo

cd "$ROOT/cv-worker"
exec "$PY" worker.py
