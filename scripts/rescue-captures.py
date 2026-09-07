#!/usr/bin/env python3
"""Find captures that failed for lack of memory, and rebuild them.

A capture that ran out of memory mid-stitch is not broken data. Every photo is
uploaded, processed and stored; only the last step - joining them into a sphere
- fell over, and the capture was recorded as `partial` with a frame-swap viewer
and a note about the photos being too large. Run it again on a worker that sizes
its working set to the memory it actually has, and it stitches.

So this looks for exactly that failure and re-triggers processing. It does not
re-upload anything and does not touch captures that failed for any other reason
- a capture whose photos genuinely do not overlap will fail again no matter how
much memory it is given, and retrying it forever is just noise.

    scripts/rescue-captures.py                        # show what it would do
    scripts/rescue-captures.py --apply                # rebuild them
    scripts/rescue-captures.py --apply --watch 300    # and keep checking

    scripts/rescue-captures.py --apply --id <uuid>    # one specific capture

The API base comes from --api or $ORBIT_API_BASE, defaulting to localhost.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_API = os.environ.get("ORBIT_API_BASE", "http://localhost:8080")

# The worker's own words when a job was killed part-way through, repeatedly.
# Matching the message rather than the status is what keeps this from retrying
# captures that failed for a reason more memory will not fix.
MEMORY_FAILURE_MARKERS = (
    "too large to process",
    "stopped part-way through",
    "ran out of memory",
    "too large to place onto a sphere",
    "too large to stitch",
)

# Statuses worth looking at. A `ready` capture already has its sphere.
RETRYABLE_STATUS = ("partial", "failed")


def request(method, url, timeout=60):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def failure_reason(capture):
    """The user-facing reason a capture is not a sphere, or ''."""
    manifest = capture.get("manifest") or {}
    return (manifest.get("degraded_why") or capture.get("error") or "").strip()


def is_memory_failure(capture):
    if capture.get("status") not in RETRYABLE_STATUS:
        return False
    # A capture that never had enough photos will not be fixed by retrying.
    if (capture.get("processed_count") or 0) < 2:
        return False
    reason = failure_reason(capture).lower()
    return any(marker in reason for marker in MEMORY_FAILURE_MARKERS)


def list_captures(api, limit=200):
    status, body = request("GET", f"{api}/api/v1/captures?limit={limit}&offset=0")
    if status != 200:
        raise SystemExit(f"could not list captures ({status}): {body.get('error', '')}")
    return body.get("captures", [])


def get_capture(api, capture_id):
    status, body = request("GET", f"{api}/api/v1/captures/{capture_id}")
    if status != 200:
        raise SystemExit(f"could not read {capture_id} ({status}): {body.get('error', '')}")
    return body.get("capture", body)


def rebuild(api, capture):
    cid = capture["id"]
    status, body = request("POST", f"{api}/api/v1/captures/{cid}/process")
    if status in (200, 201, 202):
        return True, "queued"
    return False, f"HTTP {status} {body.get('error', '')}".strip()


def sweep(api, apply_changes, only_id=None):
    if only_id:
        candidates = [get_capture(api, only_id)]
        # An explicit id is a deliberate instruction, so report what is wrong
        # with it rather than silently doing nothing.
        if not is_memory_failure(candidates[0]):
            c = candidates[0]
            print(f"  {only_id}  status={c.get('status')} - not a memory failure")
            print(f"    reason: {failure_reason(c)[:160] or '(none recorded)'}")
            return 0
    else:
        candidates = [c for c in list_captures(api) if is_memory_failure(c)]

    if not candidates:
        print("  nothing to rebuild")
        return 0

    done = 0
    for c in candidates:
        photos = c.get("processed_count") or c.get("frame_count") or 0
        print(f"  {c['id']}  {photos:>3} photos  status={c.get('status')}")
        print(f"    {failure_reason(c)[:150]}")
        if not apply_changes:
            print("    would rebuild (pass --apply)")
            continue
        ok, note = rebuild(api, c)
        print(f"    {'rebuilding' if ok else 'FAILED'}: {note}")
        done += 1 if ok else 0
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=DEFAULT_API, help=f"API base (default {DEFAULT_API})")
    ap.add_argument("--apply", action="store_true",
                    help="actually rebuild; without it, only report")
    ap.add_argument("--id", help="one capture, instead of sweeping all of them")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="keep sweeping every SECONDS until interrupted")
    args = ap.parse_args()

    api = args.api.rstrip("/")
    status, _ = request("GET", f"{api}/health", timeout=30)
    if status != 200:
        raise SystemExit(f"{api} is not answering (HTTP {status})")

    print(f"api: {api}")
    if not args.apply:
        print("dry run - nothing will be changed. Add --apply to rebuild.\n")

    while True:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] captures failed for lack of memory:")
        try:
            n = sweep(api, args.apply, args.id)
            if args.apply and n:
                print(f"[{stamp}] rebuilt {n}")
        except SystemExit:
            raise
        except Exception as e:
            print(f"[{stamp}] sweep failed: {type(e).__name__}: {e}", file=sys.stderr)

        if not args.watch:
            return
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nstopped")
            return


if __name__ == "__main__":
    main()
