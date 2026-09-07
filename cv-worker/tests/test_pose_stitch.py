"""Geometry checks for pose-based stitching.

These exercise the production code path directly rather than a synthetic
renderer, because the thing that must be right is where a given device rotation
places a photo on the sphere.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_pose_stitch.py
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ops.pose_stitch as ps  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


def upright():
    """Phone held vertically, camera pointing at the horizon."""
    h = math.radians(90) / 2
    return [math.sin(h), 0, 0, math.cos(h)]


def yawed(deg):
    h = math.radians(deg) / 2
    return qmul([0, 0, math.sin(h), math.cos(h)], upright())


def test_quaternion_matrix():
    check("identity quaternion -> identity matrix",
          np.allclose(ps.quaternion_to_matrix(0, 0, 0, 1), np.eye(3)))
    q = [0, 0, math.sin(math.radians(45)), math.cos(math.radians(45))]
    R = ps.quaternion_to_matrix(*q)
    check("90 deg about Z maps X to Y",
          np.allclose(R @ np.array([1, 0, 0]), [0, 1, 0], atol=1e-6))
    check("unnormalised quaternion is still handled",
          np.allclose(ps.quaternion_to_matrix(0, 0, 0, 5), np.eye(3)))
    check("zero quaternion does not divide by zero",
          np.allclose(ps.quaternion_to_matrix(0, 0, 0, 0), np.eye(3)))


def test_camera_rotation_is_a_rotation():
    for deg in (0, 37, 90, 180, 271):
        R = ps.camera_rotation(yawed(deg))
        check("camera rotation at %d deg is orthonormal" % deg,
              np.allclose(R @ R.T, np.eye(3), atol=1e-5))
        check("camera rotation at %d deg has det 1" % deg,
              abs(np.linalg.det(R) - 1) < 1e-5)


def test_intrinsics():
    K, f = ps.intrinsics(640, 480, 65)
    half = math.degrees(math.atan((640 / 2) / f))
    check("frame edge equals half the fov", abs(half - 32.5) < 0.01, "%.3f" % half)
    check("principal point is the image centre", K[0][2] == 320 and K[1][2] == 240)


def test_yaw_maps_to_even_horizontal_spacing():
    """The core guarantee: rotating the phone must slide the photo along the
    sphere by a proportional, predictable amount."""
    K, f = ps.intrinsics(640, 480, 65)
    warper = cv2.PyRotationWarper("spherical", f)
    circumference = 2 * math.pi * f
    px_per_deg = circumference / 360.0

    xs = []
    for deg in range(0, 181, 30):
        pt = warper.warpPoint((320.0, 240.0), K, ps.camera_rotation(yawed(deg)))
        xs.append(pt[0])

    check("the starting direction sits at the centre of the panorama",
          abs(xs[0]) < 1.0, "%.2f" % xs[0])
    for i in range(1, len(xs)):
        step = xs[i] - xs[i - 1]
        # Past 180 degrees the position wraps; compare on the circle.
        step = (step + circumference / 2) % circumference - circumference / 2
        # NEGATIVE, and that sign is the whole point. yawed() turns the phone
        # anticlockwise seen from above - the user turning LEFT - so the photo
        # belongs to the LEFT of the one before it, at a smaller x.
        #
        # This check used to demand a positive step. It passed for as long as
        # camera_rotation ran azimuth backwards, because it only ever compared
        # the code against itself: even spacing, consistent direction, nothing
        # anchored to a real scene. test_yaw_matches_a_known_scene below is
        # what actually pins the direction down.
        check("30 deg of yaw moves %.1f px (got %.1f)" % (-30 * px_per_deg, step),
              abs(step + 30 * px_per_deg) < 1.0)


def pitched(deg):
    h = math.radians(deg) / 2
    return qmul(upright(), [math.sin(h), 0, 0, math.cos(h)])


def _equirect_with_landmarks():
    """A synthetic 360 with a distinctly coloured letter every 90 degrees.

    Longitude runs left to right, so in this source image the landmarks appear
    in the order F, R, P, L. Any correct rendering of the same scene must show
    them in that cyclic order too.
    """
    W, H = 2048, 1024
    eq = np.full((H, W, 3), 110, np.uint8)
    rng = np.random.default_rng(11)
    for _ in range(2500):                      # texture, so the warp is visible
        cv2.circle(eq, (int(rng.integers(0, W)), int(rng.integers(0, H))),
                   int(rng.integers(3, 12)),
                   tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
    for deg, colour in ((0, (0, 0, 255)), (90, (0, 255, 0)),
                        (180, (255, 0, 0)), (270, (0, 255, 255))):
        x = int(deg / 360.0 * W)
        for dx in (-W, 0, W):                  # draw across the wrap too
            cv2.rectangle(eq, (x + dx - 40, H // 2 - 60),
                          (x + dx + 40, H // 2 + 60), colour, -1)
    return eq


def _view_at(eq, lon_deg, w, h, hfov):
    """The perspective photo whose optical axis points at this longitude."""
    a = math.radians(lon_deg)
    R = np.array([[math.cos(a), 0, -math.sin(a)],
                  [0, 1, 0],
                  [math.sin(a), 0, math.cos(a)]], np.float32)
    f = (w / 2.0) / math.tan(math.radians(hfov) / 2.0)
    j, i = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    d = np.stack([(j - w / 2) / f, (i - h / 2) / f, np.ones_like(j)], -1)
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    v = d @ R
    lon = np.arctan2(v[..., 0], v[..., 2])
    lat = np.arcsin(np.clip(v[..., 1], -1, 1))
    return cv2.remap(eq,
                     (((lon / (2 * math.pi)) + .5) * eq.shape[1]).astype(np.float32),
                     (((lat / math.pi) + .5) * eq.shape[0]).astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def test_yaw_matches_a_known_scene():
    """Does the finished sphere actually agree with the room it was shot in?

    Every other yaw test here compares the code against itself: it checks that
    the spacing is even and the direction consistent, which stays true whether
    azimuth runs forwards or backwards. A mirrored 360 passes all of them.

    This one renders photos FROM a known panorama, stitches them back, and
    checks the landmarks come out in the order they went in.

    yawed(d) turns the phone anticlockwise seen from above, so its optical axis
    sweeps towards NEGATIVE longitude - hence the -d here.
    """
    eq = _equirect_with_landmarks()
    imgs, quats = [], []
    for k in range(12):
        d = k * 30
        imgs.append(_view_at(eq, -d, 480, 640, ps.DEFAULT_HFOV_DEG))
        quats.append(yawed(d))

    ok, pano, reason, geom = ps.stitch_with_poses(imgs, quats)
    check("a full turn of known views stitches", ok, str(reason))
    if not ok:
        return

    width = pano.shape[1]
    bearings = {}
    for name, colour in (("F", (0, 0, 255)), ("R", (0, 255, 0)),
                         ("P", (255, 0, 0)), ("L", (0, 255, 255))):
        hit = np.abs(pano.astype(np.int16) - np.array(colour, np.int16)).sum(2) < 90
        if hit.sum() < 200:
            continue
        # Circular mean: a landmark may straddle the wrap and appear at both ends.
        a = np.where(hit.any(0))[0] / float(width) * 2 * math.pi
        bearings[name] = math.degrees(
            math.atan2(np.sin(a).mean(), np.cos(a).mean()) % (2 * math.pi))

    check("all four landmarks survive the stitch", len(bearings) == 4, str(sorted(bearings)))
    if len(bearings) != 4:
        return

    order = "".join(n for n, _ in sorted(bearings.items(), key=lambda kv: kv[1]))
    check("the scene is not mirrored: landmarks keep their real order",
          order in "FRPL" * 2,
          "left to right got %s, wanted a rotation of FRPL" % order)


def test_quaternion_from_heading_matches_the_sensor():
    """A rotation built from yaw and pitch must land where a real one would.

    These two helpers are how the rest of this file describes a phone pointing
    somewhere, so agreeing with them is agreeing with the sensor convention.
    yawed() turns anticlockwise; a compass heading is clockwise; hence the sign.
    """
    for deg in (0, 30, 90, 180, 270, 359):
        built = ps.quaternion_to_matrix(*ps.quaternion_from_heading(-deg))
        sensed = ps.quaternion_to_matrix(*yawed(deg))
        check("heading %d matches the sensor quaternion" % deg,
              np.allclose(built, sensed, atol=1e-5))

    for deg in (-45, -20, 20, 45):
        built = ps.quaternion_to_matrix(*ps.quaternion_from_heading(0, deg))
        sensed = ps.quaternion_to_matrix(*pitched(deg))
        check("pitch %d matches the sensor quaternion" % deg,
              np.allclose(built, sensed, atol=1e-5))


def test_headings_alone_stitch_a_full_turn():
    """The point of the fallback: no quaternion, still a sphere.

    A phone that will not report a rotation vector used to drop the whole
    capture onto feature matching - the one path a blank wall defeats - even
    though the guided flow had recorded a heading for every shot all along.
    """
    eq = _equirect_with_landmarks()
    # A photo taken at compass heading H shows the scene at longitude H: both
    # run clockwise. So the heading passed here is the SAME number as the
    # longitude rendered, and getting that pairing backwards is exactly how a
    # mirrored panorama sneaks through - which is why the landmark order is
    # checked below and not just the span. A mirrored sphere spans 360 too.
    headings = [k * 30 for k in range(12)]
    imgs = [_view_at(eq, h, 480, 640, ps.DEFAULT_HFOV_DEG) for h in headings]
    quats = [ps.quaternion_from_heading(h) for h in headings]

    ok, pano, reason, geom = ps.stitch_with_poses(imgs, quats)
    check("headings alone are enough to stitch", ok, str(reason))
    if not ok:
        return

    span = float((pano > 10).any(axis=2).any(axis=0).sum()) / (
        geom.circumference_px / 360.0)
    check("they cover a full turn", abs(span - 360) < 15, "%.0f degrees" % span)

    width = pano.shape[1]
    bearings = {}
    for name, colour in (("F", (0, 0, 255)), ("R", (0, 255, 0)),
                         ("P", (255, 0, 0)), ("L", (0, 255, 255))):
        hit = np.abs(pano.astype(np.int16) - np.array(colour, np.int16)).sum(2) < 90
        if hit.sum() < 200:
            continue
        a = np.where(hit.any(0))[0] / float(width) * 2 * math.pi
        bearings[name] = math.degrees(
            math.atan2(np.sin(a).mean(), np.cos(a).mean()) % (2 * math.pi))
    order = "".join(n for n, _ in sorted(bearings.items(), key=lambda kv: kv[1]))
    check("and the scene is not mirrored", len(order) == 4 and order in "FRPL" * 2,
          "left to right got %s, wanted a rotation of FRPL" % order)


def test_geometry_comes_back_with_the_picture():
    """finish_panorama cannot do its job without these two numbers."""
    eq = _equirect_with_landmarks()
    imgs = [_view_at(eq, -k * 30, 480, 640, ps.DEFAULT_HFOV_DEG) for k in range(12)]
    quats = [yawed(k * 30) for k in range(12)]

    ok, pano, reason, geom = ps.stitch_with_poses(imgs, quats)
    check("a stitch reports its geometry", ok and geom is not None, str(reason))
    if not geom:
        return

    check("the panorama is no wider than one full turn",
          pano.shape[1] <= geom.circumference_px + 4,
          "%d px across, one turn is %.0f" % (pano.shape[1], geom.circumference_px))
    check("the horizon is inside the picture",
          0 <= geom.equator_y <= pano.shape[0],
          "row %.0f of %d" % (geom.equator_y, pano.shape[0]))

    # A level ring is symmetric about the horizon, so it should land mid-height.
    check("a level ring puts the horizon at mid-height",
          abs(geom.equator_y - pano.shape[0] / 2.0) < pano.shape[0] * 0.06,
          "row %.0f of %d" % (geom.equator_y, pano.shape[0]))


def test_pitch_moves_the_right_way():
    """Direction, not just distance.

    The original version of this test only asserted that pitching moved the
    point by more than 100 pixels. It passed happily while the whole panorama
    was upside down: photos of the ceiling came out underfoot. Canvas y grows
    downward, so looking UP must produce a SMALLER y.
    """
    K, f = ps.intrinsics(640, 480, 65)
    warper = cv2.PyRotationWarper("spherical", f)
    flat = warper.warpPoint((320.0, 240.0), K, ps.camera_rotation(upright()))

    for deg in (20, 45):
        p = warper.warpPoint((320.0, 240.0), K, ps.camera_rotation(pitched(deg)))
        check("looking up %d deg puts content ABOVE the horizon" % deg,
              p[1] < flat[1], "y=%.1f vs level %.1f" % (p[1], flat[1]))
        check("looking up %d deg does not shift sideways" % deg,
              abs(p[0] - flat[0]) < 2.0, "dx=%.2f" % (p[0] - flat[0]))

    for deg in (-20, -45):
        p = warper.warpPoint((320.0, 240.0), K, ps.camera_rotation(pitched(deg)))
        check("looking down %d deg puts content BELOW the horizon" % deg,
              p[1] > flat[1], "y=%.1f vs level %.1f" % (p[1], flat[1]))


def test_yaw_goes_one_consistent_direction():
    """Turning right must always move content the same way, never reverse."""
    K, f = ps.intrinsics(640, 480, 65)
    warper = cv2.PyRotationWarper("spherical", f)
    xs = [warper.warpPoint((320.0, 240.0), K, ps.camera_rotation(yawed(d)))[0]
          for d in (0, 30, 60, 90, 120)]
    steps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    check("every yaw step moves the same direction",
          all(s > 0 for s in steps) or all(s < 0 for s in steps), str(steps))
    check("every yaw step is the same size",
          max(steps) - min(steps) < 1.0, str(steps))


def test_first_photo_is_not_split_across_the_seam():
    """The starting direction must sit in the middle of the panorama.

    A panorama wraps, so its left and right edges are glued together. If the
    reference direction lands on that join, the user's first photo is sliced in
    half and appears at both ends of the finished 360 - which reads as "the
    first photo shows up last".
    """
    K, f = ps.intrinsics(640, 480, 65)
    warper = cv2.PyRotationWarper("spherical", f)
    circumference = 2 * math.pi * f
    split = []
    for deg in range(0, 360, 30):
        _, w = warper.warp(np.zeros((480, 640, 3), np.uint8), K,
                           ps.camera_rotation(yawed(deg)),
                           cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)
        if w.shape[1] > circumference / 2:
            split.append(deg)
    check("the user's starting direction is not the one that gets split",
          0 not in split, "split at %s" % split)
    check("the split falls behind the user, near 180 deg",
          all(120 <= d <= 240 for d in split), "split at %s" % split)


def test_photo_content_keeps_its_orientation():
    """The bug every earlier test missed.

    Each photo was being laid onto the sphere rotated half a turn, so the sky
    came out underfoot and left came out right. The photo's CENTRE stays put
    under that rotation, and every test measured centres - so all of them
    passed while the finished 360 was upside down and back to front.

    This checks two off-centre landmarks instead.
    """
    K, f = ps.intrinsics(640, 480, 65)
    circumference = 2 * math.pi * f

    for deg in (0, 90, 270):
        img = np.full((480, 640, 3), 40, np.uint8)
        img[10:80, 10:120] = 255                    # white, top-LEFT
        img[400:470, 520:630] = (0, 0, 255)         # red, bottom-RIGHT

        # A single photo is refused by design, so exercise the warp directly
        # through the same helper the stitcher uses. The source goes in as-is:
        # the stitcher no longer pre-rotates it, so neither does this.
        warper = cv2.PyRotationWarper("spherical", f)
        _, w = warper.warp(img, K, ps.camera_rotation(yawed(deg)),
                           cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)
        if w.shape[1] > circumference / 2:
            continue                                # this one straddles the seam

        g = cv2.cvtColor(w, cv2.COLOR_BGR2GRAY)
        ys, xs = np.where(g > 240)
        red = ((w[:, :, 2].astype(int) > 200) & (w[:, :, 1].astype(int) < 80)
               & (w[:, :, 0].astype(int) < 80))
        ry, rx = np.where(red)
        check("at yaw %d the top-left of the photo stays top-left" % deg,
              len(xs) and xs.mean() < w.shape[1] / 2 and ys.mean() < w.shape[0] / 2)
        check("at yaw %d the bottom-right of the photo stays bottom-right" % deg,
              len(rx) and rx.mean() > w.shape[1] / 2 and ry.mean() > w.shape[0] / 2)
        check("at yaw %d the view stays a narrow slice" % deg,
              w.shape[1] < 1.2 * circumference / 4, "width=%d" % w.shape[1])


def test_refuses_without_enough_poses():
    imgs = [np.zeros((64, 64, 3), np.uint8)] * 3
    ok, out, reason, geom = ps.stitch_with_poses(imgs, [None, None, None])
    check("refuses when no photo has a pose", ok is False and out is None)
    check("gives a plain-english reason", bool(reason) and "rotation" in reason.lower(), str(reason))


def test_end_to_end_placement():
    """Eight distinct views placed by pose should tile a wide strip."""
    imgs, quats = [], []
    for i in range(8):
        img = np.full((240, 320, 3), 30, np.uint8)
        cv2.circle(img, (160, 120), 60,
                   (int(30 * i) % 255, 200, (255 - 30 * i) % 255), -1)
        imgs.append(img)
        quats.append(yawed(i * 45))
    ok, pano, reason, geom = ps.stitch_with_poses(imgs, quats)
    check("eight posed views stitch", ok is True, str(reason))
    if ok:
        h, w = pano.shape[:2]
        check("result spans roughly the full circle", w > 4 * h, "%dx%d" % (w, h))


def test_large_portrait_photos_do_not_explode():
    """The regression that killed the worker.

    Real phone photos arrive tall (1600x2844). At full size that gives a
    7890px sphere circumference and a 97 degree vertical span, and multi-band
    blending six of them allocated 19GB before the kernel stepped in.
    """
    import resource

    w, h = 1600, 2844
    _, full_focal = ps.intrinsics(w, h, ps.DEFAULT_HFOV_DEG)
    full_circ = 2 * math.pi * full_focal
    check("full-size sources would exceed the budget", full_circ > ps.MAX_CIRCUMFERENCE_PX,
          "%.0f px" % full_circ)

    imgs, quats = [], []
    for i in range(6):
        img = np.full((h, w, 3), 40, np.uint8)
        cv2.circle(img, (w // 2, h // 2), 200, (0, 200, 255), -1)
        imgs.append(img)
        quats.append(yawed(i * 60))

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    ok, pano, reason, geom = ps.stitch_with_poses(imgs, quats)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    check("tall phone photos stitch instead of dying", ok is True, str(reason))
    if ok:
        check("canvas stays within the circumference budget",
              pano.shape[1] <= ps.MAX_CIRCUMFERENCE_PX + 8, "width=%d" % pano.shape[1])
    check("peak memory stays reasonable (<2GB growth)", after - before < 2048,
          "grew %.0f MB" % (after - before))
    print("       peak RSS %.0f MB (was 8000+ MB before the cap)" % after)


def test_full_sphere_capture_with_pole_shots():
    """The capture that killed the worker a second time.

    Thirteen tall portrait photos including shots straight up and straight
    down. Pole shots warp toward a pole, where a spherical projection stretches
    without bound, so their tiles dwarf the horizon ring. Seam finding on
    full-size copies of all of them allocated over 8GB.
    """
    import resource

    w, h = 1600, 2844
    imgs, quats = [], []
    for i in range(9):                       # the horizon ring
        img = np.full((h, w, 3), 40, np.uint8)
        cv2.circle(img, (w // 2, h // 2), 300, (0, 180, 255), -1)
        imgs.append(img)
        quats.append(yawed(i * 40))
    for pitch in (45, -45, 89, -89):         # sky, floor and both poles
        img = np.full((h, w, 3), 70, np.uint8)
        cv2.circle(img, (w // 2, h // 2), 300, (255, 180, 0), -1)
        imgs.append(img)
        quats.append(pitched(pitch))

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    ok, pano, reason, geom = ps.stitch_with_poses(imgs, quats)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    check("a 13-photo sphere with pole shots stitches", ok is True, str(reason))
    check("peak memory stays under 3GB", after < 3072, "peak %.0f MB" % after)
    print("       13 photos incl. poles, peak RSS %.0f MB" % after)
    if ok:
        check("the result is a sane size",
              pano.shape[1] <= ps.MAX_CIRCUMFERENCE_PX + 8
              and pano.shape[0] <= ps.MAX_CIRCUMFERENCE_PX,
              "%dx%d" % (pano.shape[1], pano.shape[0]))


if __name__ == "__main__":
    for fn in [test_quaternion_matrix, test_camera_rotation_is_a_rotation,
               test_intrinsics, test_yaw_maps_to_even_horizontal_spacing,
               test_yaw_matches_a_known_scene,
               test_quaternion_from_heading_matches_the_sensor,
               test_headings_alone_stitch_a_full_turn,
               test_geometry_comes_back_with_the_picture,
               test_pitch_moves_the_right_way,
               test_yaw_goes_one_consistent_direction,
               test_first_photo_is_not_split_across_the_seam,
               test_photo_content_keeps_its_orientation,
               test_refuses_without_enough_poses, test_end_to_end_placement,
               test_large_portrait_photos_do_not_explode,
               test_full_sphere_capture_with_pole_shots]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
