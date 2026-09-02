#!/usr/bin/env bash
# End-to-end proof: create a capture, upload 8 photos, process, wait, print the manifest.
# Requires the API and the CV worker to be running.
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API:-http://localhost:8080}
PHOTOS=${PHOTOS:-scripts/testdata}

say()  { printf "\033[36m==>\033[0m %s\n" "$*"; }
fail() { printf "\033[31mFAIL:\033[0m %s\n" "$*"; exit 1; }

command -v jq >/dev/null || fail "jq is required: sudo apt install jq"
curl -sf "$API/health" >/dev/null || fail "API is not running at $API. Start it with: make api"

# The generator needs numpy + Pillow, which live in the CV worker's virtualenv.
PY_BIN=python3
[[ -x cv-worker/.venv/bin/python ]] && PY_BIN=cv-worker/.venv/bin/python

if [[ ! -d $PHOTOS ]] || [[ -z $(ls -A "$PHOTOS"/*.jpg 2>/dev/null) ]]; then
  say "Generating test photos with $PY_BIN ..."
  "$PY_BIN" scripts/make_test_photos.py "$PHOTOS" \
    || fail "could not generate test photos (need numpy+Pillow: cd cv-worker && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)"
fi

mapfile -t FILES < <(ls "$PHOTOS"/*.jpg | sort)
(( ${#FILES[@]} >= 8 )) || fail "need at least 8 test photos in $PHOTOS, found ${#FILES[@]}"

say "Creating capture..."
RESP=$(curl -sf -X POST "$API/api/v1/captures" \
  -H 'Content-Type: application/json' \
  -d '{"title":"E2E test capture","mode":"pano","include_up_down":false}')
ID=$(jq -r .capture.id <<<"$RESP")
SLUG=$(jq -r .capture.slug <<<"$RESP")
[[ $ID != null && -n $ID ]] || fail "no capture id in response: $RESP"
say "capture id=$ID slug=$SLUG"

say "Uploading ${#FILES[@]} photos..."
i=0
for f in "${FILES[@]}"; do
  YAW=$(( i * 45 ))
  curl -sf -X POST "$API/api/v1/captures/$ID/photos" \
    -F "photo=@$f" -F "index=$i" -F "slot_id=ring_$i" \
    -F "yaw=$YAW" -F "pitch=0" > /dev/null \
    || fail "upload of $f failed"
  printf "  [%d/%d] %s at %d°\n" "$((i+1))" "${#FILES[@]}" "$(basename "$f")" "$YAW"
  i=$((i+1))
done

say "Starting processing..."
curl -sf -X POST "$API/api/v1/captures/$ID/process" >/dev/null || fail "process failed"

say "Waiting for it to finish (up to 180s)..."
for n in $(seq 1 180); do
  C=$(curl -sf "$API/api/v1/captures/$ID")
  STATUS=$(jq -r .capture.status <<<"$C")
  PROG=$(jq -r .progress <<<"$C")
  printf "\r  status=%-12s progress=%3s%%  (%ss)" "$STATUS" "$PROG" "$n"
  case $STATUS in
    ready|partial) echo; break ;;
    failed) echo; fail "capture failed: $(jq -r .capture.error <<<"$C")" ;;
  esac
  sleep 1
  [[ $n == 180 ]] && { echo; fail "timed out — is the CV worker running? check .logs/worker.log"; }
done

say "Manifest:"
MANIFEST=$(curl -sf "$API/api/v1/captures/$ID/manifest") || fail "no manifest"
jq . <<<"$MANIFEST"

RENDERER=$(jq -r .renderer <<<"$MANIFEST")
DEGRADED=$(jq -r .degraded <<<"$MANIFEST")
echo
if [[ $RENDERER == sphere ]]; then
  say "RESULT: stitched into a seamless sphere."
else
  say "RESULT: fell back to the swipeable frame viewer (degraded=$DEGRADED)."
  say "Reason: $(jq -r '.degraded_why // "n/a"' <<<"$MANIFEST")"
  say "This is a supported outcome, not a failure."
fi
echo
say "View it at: http://localhost:5173/#/view/$SLUG"
