"""Configuration for the Orbit CV worker. Every value can be overridden by env var."""
import os


def _env(k, default):
    v = os.environ.get(k)
    return v if v not in (None, "") else default


def _endpoint(k, default):
    """S3 endpoints are host[:port], never a URL.

    Pasting the scheme in is the natural mistake, and the Go client tolerates it
    while the Python one raises "path in endpoint is not allowed" at startup, so
    the two halves of the app disagree about a working config. Strip it here.
    """
    v = _env(k, default)
    for scheme in ("https://", "http://"):
        if v.startswith(scheme):
            v = v[len(scheme):]
    return v.rstrip("/")


def _envint(k, default):
    try:
        return int(_env(k, default))
    except (TypeError, ValueError):
        return default


def _cgroup_memory_limit_mb():
    """The container's memory ceiling in MiB, or None when not limited.

    Reads cgroup v2 first, then v1. A host with no limit reports "max" (v2) or
    a sentinel near 2^63 (v1); both mean "no ceiling".
    """
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # v1 reports an enormous sentinel rather than "max" when unlimited.
        if value <= 0 or value >= (1 << 62):
            return None
        return value // (1024 * 1024)
    return None


def _auto_tile_budget_px():
    """Total warped-tile pixels the host can hold, from its memory limit."""
    limit = _cgroup_memory_limit_mb()
    if limit is None:
        return 120_000_000          # unconstrained host: the historical budget
    return max(12_000_000, (limit * 1024 * 1024 // 3) // 4)


def _auto_compositing_mp():
    """Cap the stitch on a small instance; leave full resolution on a big one.

    768 MiB is the dividing line: a free 512 MiB instance needs the cap, while
    anything larger has room for OpenCV's default and should keep the sharper
    panorama it produces.
    """
    limit = _cgroup_memory_limit_mb()
    if limit is not None and limit <= 768:
        return 1.2
    return 0.0


class Settings:
    # Redis. Managed providers hand out a rediss:// URL carrying a password and
    # requiring TLS, which host/port cannot express; redis_url wins when set.
    redis_host = _env("REDIS_HOST", "localhost")
    redis_port = _envint("REDIS_PORT", 6380)
    redis_url = _env("REDIS_URL", "")
    stream_jobs = "orbit:jobs"
    group_workers = "cv-workers"
    stream_dlq = "orbit:jobs:dlq"
    consumer_name = _env("CONSUMER_NAME", f"cv-worker-{os.getpid()}")

    # MinIO / S3
    minio_endpoint = _endpoint("MINIO_ENDPOINT", "localhost:9010")
    minio_access_key = _env("MINIO_ACCESS_KEY", "orbitadmin")
    minio_secret_key = _env("MINIO_SECRET_KEY", "orbitadmin123")
    minio_use_ssl = _env("MINIO_USE_SSL", "false") == "true"
    bucket_private = _env("BUCKET_PRIVATE", "orbit-private")
    bucket_public = _env("BUCKET_PUBLIC", "orbit-public")

    # API
    api_base_url = _env("API_BASE_URL", "http://localhost:8080")

    # Processing
    target_width_default = _envint("TARGET_WIDTH", 1600)
    thumb_width = _envint("THUMB_WIDTH", 500)
    jpeg_quality = _envint("JPEG_QUALITY", 85)

    # Megapixels the stitcher composites at. OpenCV's default is the input
    # resolution, which is by far the worker's largest allocation: +171 MiB over
    # the loaded frames for a 16-photo ring, against +95 MiB capped to 1.2.
    #
    # Left to itself on a 512 MiB instance that difference is an OOM kill, and
    # the failure is invisible from outside - the container just restarts and
    # the capture silently falls back. So rather than depending on an operator
    # setting an env var they cannot verify, the cap is chosen from the memory
    # limit the container was actually given. An explicit value always wins.
    stitch_compositing_mp = float(_env("STITCH_COMPOSITING_MP", "0")) or _auto_compositing_mp()

    # Resolution the stitcher finds and matches features at. OpenCV's default is
    # 0.6 Mpx. Detailed real-world photos make this a second large allocation,
    # so it is trimmed on a small instance alongside the compositing cap.
    stitch_registration_mp = float(_env("STITCH_REGISTRATION_MP", "0")) or (
        0.4 if _auto_compositing_mp() else 0.0)

    # Pose stitching holds every warped tile and its mask at once, so its budget
    # has to come from the memory the container actually has rather than a fixed
    # constant sized for a big machine. Roughly 4 bytes per pixel (BGR + mask),
    # and no more than a third of the limit, leaving room for the source frames
    # and the finished panorama.
    pose_tile_budget_px = int(_env("POSE_TILE_BUDGET_PX", "0")) or _auto_tile_budget_px()
    # Megapixels per photo used when searching for the seam between two
    # overlapping shots. Higher lets the cut follow real edges in the scene
    # instead of stepping across a coarse grid; the working images are small
    # and short-lived, so this is cheap next to the warped tiles.
    seam_work_megapix = float(_env("SEAM_WORK_MEGAPIX", "0")) or (
        0.2 if _auto_compositing_mp() else 0.4)

    # Pixels around the sphere's equator. Smaller means smaller tiles from the
    # very first warp, which is the only saving that arrives early enough.
    pose_circumference_px = int(_env("POSE_CIRCUMFERENCE_PX", "0")) or (
        2560 if _cgroup_memory_limit_mb() and _cgroup_memory_limit_mb() <= 768 else 4096)

    # Retry / backoff
    max_attempts = _envint("MAX_ATTEMPTS", 3)
    backoff_base_seconds = _envint("BACKOFF_BASE_SECONDS", 2)

    # Finalize wait
    finalize_poll_interval = float(_env("FINALIZE_POLL_INTERVAL", "2"))
    finalize_timeout_seconds = _envint("FINALIZE_TIMEOUT_SECONDS", 180)

    # Optional / heavy features, off by default
    enable_bg_removal = _env("ENABLE_BG_REMOVAL", "false") == "true"

    # Consumer loop
    block_ms = _envint("BLOCK_MS", 5000)

    # Some free hosts (Hugging Face Spaces) only keep a container alive if it
    # listens on a port. This worker serves nothing but /health there.
    health_port = _envint("HEALTH_PORT", 0)

    # A job still unacknowledged after this long belongs to a worker that died
    # mid-way. Another worker takes it over. Must comfortably exceed the
    # slowest legitimate job (a big stitch), or healthy work gets stolen.
    reclaim_idle_ms = _envint("RECLAIM_IDLE_MS", 300_000)      # 5 minutes
    reclaim_every_seconds = _envint("RECLAIM_EVERY_SECONDS", 60)
    # How many times a job may be handed out before we stop retrying it. A job
    # that crashes its worker would otherwise kill every worker in turn.
    max_deliveries = _envint("MAX_DELIVERIES", 3)
    claim_idle_ms = _envint("CLAIM_IDLE_MS", 60000)


settings = Settings()
