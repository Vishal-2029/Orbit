#!/usr/bin/env bash
# Run the API and the CV worker side by side in one container.
#
# Neither process is optional: an API with no worker leaves every capture stuck
# at "queued", and a worker with no API has nothing to report results to. So if
# either exits, take the whole container down and let the platform restart it,
# rather than sitting in a half-working state the health check still passes.
set -uo pipefail

term() {
	kill -TERM "${API_PID:-}" "${WORKER_PID:-}" 2>/dev/null || true
	wait
}
trap term TERM INT

# The worker reports results back to the API over the loopback interface, so it
# must target whatever port the API actually bound. Hosts inject that (Render
# uses 10000), so deriving it here beats asking anyone to keep two env vars in
# sync -- getting it wrong fails every frame with connection refused.
export API_BASE_URL="${API_BASE_URL:-http://localhost:${PORT:-8080}}"
echo "[start-combined] api on port ${PORT:-8080}, worker reporting to $API_BASE_URL"

cd /app/cv-worker
python worker.py &
WORKER_PID=$!

cd /app
/app/orbit-api &
API_PID=$!

# Wait for whichever dies first, report which, then stop the other.
wait -n "$API_PID" "$WORKER_PID"
STATUS=$?

if ! kill -0 "$API_PID" 2>/dev/null; then
	echo "[start-combined] api exited (status $STATUS), stopping worker" >&2
else
	echo "[start-combined] cv worker exited (status $STATUS), stopping api" >&2
fi

term
exit "$STATUS"
