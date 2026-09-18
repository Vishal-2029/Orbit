# When a 360 comes out wrong: what it looks like, and what it was

Every fault here was found on a real capture, measured, and fixed. It is
written as a lookup so the next one starts from the measurement rather than
from scratch: what the finished sphere looked like, the line the worker logged,
the number that proved the cause, and where the fix lives.

**How to point at a bad join.** Open the 360 and press **#**. Every photo is
labelled where it actually landed — "Photo 3 · ahead-right, tilted down" — with
the same numbers as the restitch screen. A fault is then "between Photo 5 and
Photo 6", which is two named photos to pull and measure, not a hunt.

**The first question is always which photos, not which code.** Download the
originals for those frames, match them, and measure. Half of these turned out
to be the opposite of the first guess.

---

## The joins step upward, one photo at a time, and frames break at every seam

**Looked like:** a top edge climbing in steps across the panorama; window frames
and railings cut and offset where two photos met.

**Logged:** `pose refinement: 5 of 27 pairs used ... field of view 63.0 deg from
0 pairs`.

**Was:** two faults at once. The lens was assumed to see 63 degrees when it saw
about 74 (a phone's ultra-wide, EXIF 16mm equivalent), so every photo was drawn
too small; and the compass had drifted up to 10 degrees part-way round. The
refinement that should have corrected both could not match a plain tiled floor,
kept 5 of 27 pairs, and fell back on the sensor.

**Measured by:** fitting each photo pair's own rotation across candidate fields
of view. The count of rays that agree peaks at the true one - 260 inliers at 74
degrees against 191 at 63 - and the sensor's own errors showed as a median
4-degree disagreement that no field of view could explain.

**Fixed in:** `cv-worker/ops/ray_solve.py`. Matched points across the join went
from 50.7 px apart to 6.2 px.

---

## A 32-photo sphere broken across a stairwell, a wall and a window

**Logged:** `ray solve came apart (a camera moved 29 deg); not used`.

**Was:** the correction was right and its own safety limit was wrong. Measured
pair by pair, the sensor was accurate about TILT - pitch and roll within a
degree or two, because those come from gravity - and wrong about HEADING by
15-30 degrees on several shots, because yaw in gyro mode has no fixed reference
and jumps near steel and glass. One shot logged at 317 degrees sat at 300. The
solve dropped any pair disagreeing by over 20 degrees in total and refused any
camera moved over 25, so the real heading corrections were exactly what it threw
away.

**Fixed in:** `cv-worker/ops/ray_solve.py` - trust is now split by axis: strict
on tilt, loose on heading, with the limits and the prior split the same way.
Matched points went from 266 px apart to 10 px, and the solve is used instead of
rejected.

**If it recurs:** compare the photo-derived pair rotation against the sensor's,
decomposed into tilt and heading (`_tilt_yaw_disagreement`). Tilt disagreement
means a real problem - a wrong field of view, or parallax. Heading
disagreement alone is normal and the solve should absorb it.

---

## A pillar with a notch cut through it, and a doubled edge

**Was:** parallax, not pose error. The photographer turned their body, so the
phone swung on an arc instead of pivoting on the spot: a pillar a metre away
cannot line up while the stairs behind it do. The seam finder then cut straight
through the pillar's face.

**Measured by:** the pillar's shape changing between photos, and a translation
model explaining more matches than a pure rotation (19 against 7).

**Fixed in:** `cv-worker/ops/pose_stitch.py` - graph-cut seams route the cut
along the pillar's edge rather than through it. No rotation-only stitcher can
remove parallax itself; this hides it where it shows.

**Cost, and the limit:** graph cut took 495 s of a 504 s render on 32 photos
against 19 s for the dynamic-programming finder, so it is capped at
`GRAPH_CUT_MAX_PHOTOS`. Raising that cap needs a timing run, not a guess.

---

## Two joins in a stairwell that no setting would fix

**Was:** too few matches, not bad angles. Dark granite steps and a white pillar
gave 7 usable matches on one join - below the 8 the solve needs - so it was
skipped and the drift collected there.

**Measured by:** counting matches per join at several resolutions and contrast
thresholds. Resolution did not help; a lower contrast threshold did, because the
texture was there but faint (7 to 21 matches, 16 to 23 on its neighbour).

**Fixed in:** `cv-worker/ops/ray_solve.py` - SIFT at `contrastThreshold=0.02`.

---

## The photographer's feet on the floor

**Logged:** `straight-down photo kept only where nothing else reached: 0%`.

**Was:** pointing a phone straight down photographs whoever holds it. The ring
tilted 45 degrees down already covered that floor cleanly to the nadir, but the
seam finder judges photos by how well they agree, not by what they show.

**Fixed in:** `cv-worker/ops/pose_stitch.py` - `_nadir_fills_holes_only`: the
down shot is cut out of everywhere another photo reaches and keeps only genuine
holes.

---

## The 360 is too dark

**Was:** twice, not a stitching fault. Measured through the pipeline, the
originals were dark before the stitcher saw them (median 43 of 255), and the
panorama faithfully matched its inputs.

Two real causes were found behind it: the capture screen was biasing exposure
1-2 stops DOWN before locking it, which fires indoors on almost every room
(`web/screens/capture.js`); and the finished sphere had no tone step at all.

**Fixed in:** `cv-worker/ops/finish.py` - `auto_brighten` lifts a finished
panorama towards `AUTO_EXPOSURE_TARGET`, measured over the photographed pixels
only (black polar caps would drag the median down) and applied as a gamma so
unphotographed black stays black.

**If it recurs:** measure originals, processed frames and panorama before
touching anything. If the originals are dark, the camera is the place to fix it.

---

## A rebuild appears to change nothing

**Was:** not the stitcher at all. Panoramas, tiles and thumbnails are rewritten
in place at a fixed URL, and were served with `Cache-Control: immutable` for a
year, so a browser that had seen the old one never asked again.

**Fixed in:** `backend/internal/service/capture.go` and
`backend/internal/httpapi/router.go` - image URLs carry `?v=<updated_at>`, and
the immutable promise is only made when that stamp is present.

**If it recurs:** fetch the panorama URL directly and compare it against what
the browser shows. If they differ, it is caching, not stitching.

---

## Running a check yourself

The worker's log is the first place to look; these lines are the ones that
matter, and each maps to a section above:

```
pose refinement: N of M pairs used ...       few pairs -> the photos are hard to match
ray solve used: field of view F deg ...      the correction was taken
ray solve not used: X px vs Y px already     refinement was already better
ray solve came apart ...                     a safety limit refused it
straight-down photo kept only where ...      the nadir shot was trimmed
lifted a dark panorama: median A -> B        the tone step fired
```

To reproduce a capture locally, without touching the live site: pull its frames
and originals from the API (`/api/v1/captures/<id>/frames`, and
`/image/original/<idx>` per frame), then run `stitch_with_poses` on them with
the recorded quaternions. That is how every fault above was measured, and it
costs nothing but CPU.
