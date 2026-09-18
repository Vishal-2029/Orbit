-- A photo's position, set by hand.
--
-- Solvers work from what the photos have in common, and some photos have too
-- little: a window of repeating mullions, a blank wall, a plain floor. When the
-- machine cannot tell, the person looking at the picture can, so a photo can be
-- dragged to where it belongs and LOCKED there.
--
-- Radians, in the viewer's frame: yaw 0 mid-panorama and growing rightwards,
-- pitch positive downwards, roll about the optical axis. The same convention as
-- hotspots and manifest.photos, so a position read off the 360 can be written
-- back without conversion.
--
-- manual_locked is the instruction, not the numbers: a locked photo is placed
-- exactly here and no solve may move it. Unlocked rows keep their numbers, so
-- unlocking and re-locking does not lose the arrangement.
ALTER TABLE frames ADD COLUMN IF NOT EXISTS manual_yaw   double precision;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS manual_pitch double precision;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS manual_roll  double precision;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS manual_locked boolean NOT NULL DEFAULT false;
