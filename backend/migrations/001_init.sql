-- Orbit schema v1
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text UNIQUE NOT NULL,
  password_hash text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- A "capture" is one 360 session: either a photosphere (stand in place, shoot outward)
-- or a spin (orbit an object on a turntable).
CREATE TABLE IF NOT EXISTS captures (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid REFERENCES users(id) ON DELETE CASCADE,
  title           text NOT NULL DEFAULT 'Untitled 360',
  slug            text UNIQUE NOT NULL,
  mode            text NOT NULL DEFAULT 'pano',   -- pano | spin
  status          text NOT NULL DEFAULT 'draft',  -- draft|uploading|queued|processing|ready|failed|partial
  frame_count     int  NOT NULL DEFAULT 0,
  processed_count int  NOT NULL DEFAULT 0,
  settings        jsonb NOT NULL DEFAULT '{}'::jsonb,
  manifest        jsonb,
  error           text,
  is_public       boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One photo. slot_id ties it to the guidance step the user was told to shoot.
CREATE TABLE IF NOT EXISTS frames (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  capture_id    uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  idx           int  NOT NULL,
  slot_id       text,          -- e.g. "n", "ne", "e", "up", "down"
  yaw           double precision,   -- degrees, 0 = north/front, clockwise
  pitch         double precision,   -- degrees, 0 = horizon, + = up
  original_key  text NOT NULL,
  processed_key text,
  thumb_key     text,
  width         int, height int,
  offset_x      int NOT NULL DEFAULT 0,
  offset_y      int NOT NULL DEFAULT 0,
  status        text NOT NULL DEFAULT 'pending', -- pending|processing|done|failed
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (capture_id, idx)
);

CREATE TABLE IF NOT EXISTS jobs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  capture_id  uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  type        text NOT NULL,
  status      text NOT NULL DEFAULT 'pending',
  attempts    int  NOT NULL DEFAULT 0,
  last_error  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_frames_capture_idx ON frames (capture_id, idx);
CREATE INDEX IF NOT EXISTS idx_captures_user      ON captures (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_captures_slug      ON captures (slug);
CREATE INDEX IF NOT EXISTS idx_jobs_capture       ON jobs (capture_id, created_at DESC);
