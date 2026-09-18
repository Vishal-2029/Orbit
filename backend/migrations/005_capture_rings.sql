-- A capture's rings, stitched one at a time as they are shot.
--
-- A sphere capture is shot as rings: level all the way round, then tilted up,
-- then tilted down, then the ceiling and floor. Waiting until the last of 32
-- photos before stitching anything means the work all lands at the end, and a
-- ring that came out badly is only discovered there. A ring that is finished is
-- a ring that can be built, so each one is stitched as it completes and kept
-- here: its own preview, and its own record of how it went.
--
-- Separate from captures.manifest on purpose. The manifest describes the
-- FINISHED 360; these are intermediate results that exist while the capture is
-- still being shot, and a rebuild replaces them without touching the manifest.
CREATE TABLE IF NOT EXISTS capture_rings (
  capture_id   uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  ring         text NOT NULL,          -- "r+0", "r+45", "r-45", "up", "down"
  status       text NOT NULL DEFAULT 'queued',
  panorama_key text,
  width        integer,
  height       integer,
  photos_used  integer,
  photos_total integer,
  note         text,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (capture_id, ring)
);
