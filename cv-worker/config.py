"""Configuration for the Orbit CV worker. Every value can be overridden by env var."""
import os


def _env(k, default):
    v = os.environ.get(k)
    return v if v not in (None, "") else default


def _envint(k, default):
    try:
        return int(_env(k, default))
    except (TypeError, ValueError):
        return default


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
    minio_endpoint = _env("MINIO_ENDPOINT", "localhost:9010")
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
