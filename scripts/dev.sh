#!/usr/bin/env bash
# Start everything Orbit needs, in one command.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
LOGS="$ROOT/.logs"; mkdir -p "$LOGS"

say() { printf "\033[36m==>\033[0m %s\n" "$*"; }

say "Starting infra (Postgres, MinIO, Redis)..."
docker compose up -d >/dev/null
until docker exec orbit-postgres pg_isready -U orbit >/dev/null 2>&1; do sleep 1; done
docker exec -i orbit-postgres psql -U orbit -d orbit -q < backend/migrations/001_init.sql
say "Infra ready."

cleanup() {
  say "Stopping Orbit processes..."
  [[ -n "${API_PID:-}"    ]] && kill "$API_PID"    2>/dev/null || true
  [[ -n "${WORKER_PID:-}" ]] && kill "$WORKER_PID" 2>/dev/null || true
  [[ -n "${WEB_PID:-}"    ]] && kill "$WEB_PID"    2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Advertise the LAN IP, not localhost, so a phone on the same Wi-Fi gets image
# URLs it can actually load. Override by exporting PUBLIC_BASE_URL yourself.
LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[\d.]+' | head -1)
LAN_IP=${LAN_IP:-localhost}
export PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-http://$LAN_IP:8080}

say "Starting API on :8080  (log: .logs/api.log)"
say "  public base: $PUBLIC_BASE_URL"
( cd backend && go run ./cmd/api ) > "$LOGS/api.log" 2>&1 &
API_PID=$!

for i in $(seq 1 60); do
  curl -sf localhost:8080/health >/dev/null && break
  sleep 1
  [[ $i == 60 ]] && { say "API failed to start; see .logs/api.log"; tail -20 "$LOGS/api.log"; exit 1; }
done
say "API healthy."

if [[ -x cv-worker/.venv/bin/python ]]; then
  say "Starting CV worker  (log: .logs/worker.log)"
  ( cd cv-worker && .venv/bin/python worker.py ) > "$LOGS/worker.log" 2>&1 &
  WORKER_PID=$!
else
  say "SKIPPING CV worker - no virtualenv. Run: cd cv-worker && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

if [[ -f web/serve.py ]]; then
  say "Starting web client on :5173  (log: .logs/web.log)"
  ( cd web && python3 serve.py ) > "$LOGS/web.log" 2>&1 &
  WEB_PID=$!
fi

echo
say "Orbit is up:"
echo "    On this PC   http://localhost:8080"
echo "    On your PHONE http://$LAN_IP:8080     <-- same Wi-Fi"
echo "    MinIO admin  http://localhost:9011  (orbitadmin / orbitadmin123)"
echo
echo "    Phone camera needs one Chrome setting - see docs/RUNNING.md"
echo
say "Press Ctrl-C to stop everything."
wait
