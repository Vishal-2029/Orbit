-- Hotspots: the two things you can put inside a finished 360.
--
--   info  a marker that opens a title and a paragraph
--   link  an arrow that walks the viewer to another capture
--
-- One table for both. They differ only in which columns they use, and a second
-- table would have bought nothing but a join and two code paths in the viewer,
-- which draws them from the same list anyway.
CREATE TABLE IF NOT EXISTS hotspots (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  capture_id  uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('info', 'link')),

  -- Where it sits on the sphere, in RADIANS, with pitch positive DOWNWARDS.
  --
  -- Radians because that is what the viewer's maths speaks - its
  -- coordinatesToScreen and screenToCoordinates both take and return radians -
  -- so storing degrees would mean converting on the way in, on the way out, and
  -- in every test. Note this differs from frames.yaw, which is a compass
  -- bearing in degrees and is a genuinely different quantity: that one is where
  -- the phone was pointed, this one is where a marker was placed.
  yaw         double precision NOT NULL,
  pitch       double precision NOT NULL,

  -- info
  title       text,
  body        text,

  -- link
  target_capture_id uuid REFERENCES captures(id) ON DELETE CASCADE,
  -- Which way the arrow points along the floor, in radians. Purely cosmetic,
  -- but an arrow pointing the wrong way is worse than no arrow.
  rotation    double precision NOT NULL DEFAULT 0,

  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),

  -- Enforce the shape of each kind rather than trusting the writer. A link with
  -- no target renders as an arrow that goes nowhere, which looks like a bug in
  -- the viewer and is a bug in the data.
  CONSTRAINT hotspot_link_has_target
    CHECK (kind <> 'link' OR target_capture_id IS NOT NULL),
  -- A capture linking to itself is a loop the user cannot get out of by
  -- clicking, since the arrow would still be there when they arrive.
  CONSTRAINT hotspot_no_self_link
    CHECK (target_capture_id IS NULL OR target_capture_id <> capture_id)
);

-- The viewer asks for one capture's hotspots on every load, so this is the
-- index that matters.
CREATE INDEX IF NOT EXISTS hotspots_capture_idx ON hotspots (capture_id);

-- "What links here" - needed when a capture is deleted, and to work out which
-- captures form a tour.
CREATE INDEX IF NOT EXISTS hotspots_target_idx ON hotspots (target_capture_id)
  WHERE target_capture_id IS NOT NULL;
