-- Full 3D camera rotation at the moment each photo was taken.
--
-- yaw/pitch describe where the phone pointed, but cannot express roll and lose
-- precision near the poles. The quaternion is what the device sensor actually
-- reports and what a pose-aware stitcher needs, so it is stored verbatim.
ALTER TABLE frames ADD COLUMN IF NOT EXISTS qx double precision;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS qy double precision;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS qz double precision;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS qw double precision;

-- Which sensor produced it, so we know how far to trust it.
-- gyro | absolute | deviceorientation | none
ALTER TABLE frames ADD COLUMN IF NOT EXISTS orientation_source text;
