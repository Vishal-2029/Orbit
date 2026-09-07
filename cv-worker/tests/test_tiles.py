"""Cube tiling: do the six faces actually add up to the panorama they came from?

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_tiles.py
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import ops.tiles as T  # noqa: E402
from test_pose_stitch import _equirect_with_landmarks  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


def faces_back_to_equirect(faces, size, out_w, out_h):
    """Rebuild an equirectangular image by sampling the six faces.

    This is the inverse of what cube_face does, written independently of it, so
    that a sign error in one is not cancelled by the same error in the other.
    """
    lon = (np.arange(out_w, dtype=np.float32) + 0.5) / out_w * 2 * math.pi - math.pi
    lat_phi = (np.arange(out_h, dtype=np.float32) + 0.5) / out_h * math.pi
    lon, phi = np.meshgrid(lon, lat_phi)

    # Direction for every pixel of the target, from the shader's own mapping:
    #   theta = atan2(x, -z),  phi = acos(y)
    y = np.cos(phi)
    horiz = np.sin(phi)
    x = horiz * np.sin(lon)
    z = -horiz * np.cos(lon)

    out = np.zeros((out_h, out_w, 3), np.uint8)
    for face in T.FACES:
        rx, ry = T.FACE_ROTATION[face]
        # Rotate the world direction INTO face space (inverse rotation, so the
        # order reverses and the angles negate).
        fx, fy, fz = x.copy(), y.copy(), z.copy()
        if ry:
            c, s = math.cos(-ry), math.sin(-ry)
            fx, fz = fz * s + fx * c, fz * c - fx * s
        if rx:
            c, s = math.cos(-rx), math.sin(-rx)
            fy, fz = fy * c - fz * s, fy * s + fz * c

        # A direction belongs to this face when it points out of its front.
        on_face = fz < -1e-6
        with np.errstate(divide="ignore", invalid="ignore"):
            u = np.where(on_face, fx / -fz, 9.0)
            v = np.where(on_face, -fy / -fz, 9.0)
        inside = on_face & (np.abs(u) <= 1.0) & (np.abs(v) <= 1.0)
        if not inside.any():
            continue

        px = ((u + 1.0) * 0.5 * size).astype(np.float32)
        py = ((v + 1.0) * 0.5 * size).astype(np.float32)
        sampled = cv2.remap(faces[face], px, py, cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE)
        out[inside] = sampled[inside]
    return out


def test_faces_rebuild_the_panorama():
    """The round trip. If any face is flipped, rotated or in the wrong place,
    the rebuilt panorama stops matching the original and this catches it - which
    a test that only checked tile counts never would."""
    eq = _equirect_with_landmarks()
    h, w = eq.shape[:2]
    size = T.face_size_for(w)

    faces = {f: T.cube_face(eq, f, size) for f in T.FACES}
    back = faces_back_to_equirect(faces, size, w // 2, h // 2)
    small = cv2.resize(eq, (w // 2, h // 2), interpolation=cv2.INTER_AREA)

    # Compare away from the poles. An equirect has almost no real detail in the
    # top and bottom rows - the whole width collapses to a point - so a
    # resampling difference there says nothing about whether the faces are right.
    band = slice(int(h * 0.15), int(h * 0.35))
    a = cv2.cvtColor(back[band], cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(small[band], cv2.COLOR_BGR2GRAY).astype(np.float32)

    # Blur both before comparing. The round trip resamples twice, and this test
    # panorama is dense random circles - the highest-frequency content there is
    # - so a raw correlation is dominated by resampling softness rather than by
    # whether a face is in the right place. A light blur removes that and leaves
    # the structure, which is what is actually being asked about.
    a = cv2.GaussianBlur(a, (0, 0), 1.5)
    b = cv2.GaussianBlur(b, (0, 0), 1.5)
    corr = float(np.corrcoef(a.ravel(), b.ravel())[0, 1])
    check("the six faces rebuild the panorama they came from (r=%.3f)" % corr,
          corr > 0.97, "a low score means a face is flipped or misplaced")

    # And it must be a much better match than the mirrored version, which is the
    # failure a correlation alone could be fooled by on symmetric content.
    mirror = float(np.corrcoef(a.ravel(), np.fliplr(b).ravel())[0, 1])
    check("and it beats the mirrored comparison (%.3f vs %.3f)" % (corr, mirror),
          corr > mirror + 0.2)


def test_landmarks_land_on_the_right_faces():
    """Named directions must end up on the face that looks that way.

    The landmark panorama puts a coloured block at 0, 90, 180 and 270 degrees of
    longitude. Longitude 0 is the centre column, which is the direction the
    FRONT face looks along.
    """
    eq = _equirect_with_landmarks()
    size = T.face_size_for(eq.shape[1])

    colours = {"F": (0, 0, 255), "R": (0, 255, 0), "P": (255, 0, 0), "L": (0, 255, 255)}
    # In the source, the blocks sit at x = deg/360 * W, and the centre column is
    # longitude 0. So the block drawn at 0 is at longitude -180 (behind), the one
    # at 90 is at -90 (left), 180 is at 0 (front) and 270 is at +90 (right).
    expected = {"P": "f", "L": "r", "F": "b", "R": "l"}

    for name, colour in colours.items():
        found = []
        for face in T.FACES:
            img = T.cube_face(eq, face, size)
            hit = (np.abs(img.astype(np.int16) - np.array(colour, np.int16)).sum(2) < 90)
            if hit.sum() > size * size * 0.002:
                found.append(face)
        check("landmark %s appears on the %s face" % (name, expected[name]),
              expected[name] in found, "found on %s" % (found or "nothing"))


def test_the_pyramid_is_shaped_the_way_the_viewer_expects():
    eq = _equirect_with_landmarks()
    w = eq.shape[1]
    size = T.face_size_for(w)
    check("the face size matches the panorama's detail, not more",
          size == 512, "%d for a %dpx panorama" % (size, w))
    check("a 4096-wide panorama gives a 1024 face", T.face_size_for(4096) == 1024)
    check("and an oversized one is capped", T.face_size_for(16384) == T.MAX_FACE_SIZE)

    levels = T.levels_for(size)
    check("levels run smallest first", [l["size"] for l in levels] == sorted(
        l["size"] for l in levels), str(levels))
    check("the largest level is the full face", levels[-1]["size"] == size, str(levels))
    check("every level is a whole number of tiles",
          all(l["size"] % l["tileSize"] == 0 for l in levels), str(levels))

    # Every level is ONE tile per face. Subdividing a face into a grid is the
    # usual way to tile, and it is deliberately not done: with any level whose
    # tile is smaller than its face, the viewer drew the face being looked at
    # and left its neighbours black - 16% of the frame, in two bars down the
    # sides. This is the property that keeps that from coming back.
    check("every level is exactly one tile per face",
          all(l["size"] == l["tileSize"] for l in levels), str(levels))
    check("the smallest level is the cheap one to fetch first",
          levels[0]["size"] == T.BASE_SIZE, str(levels))


def test_cut_tiles_produces_every_tile_once():
    eq = _equirect_with_landmarks()
    size = T.face_size_for(eq.shape[1])
    levels = T.levels_for(size)

    seen = set()
    count = 0
    wrong = None
    for z, face, x, y, img in T.cut_tiles(eq, face_size=size):
        seen.add((z, face, x, y))
        count += 1
        # A tile IS a face at that level's resolution.
        want = levels[z]["tileSize"]
        if img.shape[0] != want or img.shape[1] != want:
            wrong = "z=%d %s %d,%d is %dx%d, wanted %d" % (
                z, face, x, y, img.shape[1], img.shape[0], want)
            break

    check("every tile is exactly its level's tile size", wrong is None, wrong or "")
    check("and they are all at grid position 0,0",
          all(x == 0 and y == 0 for _, _, x, y in seen), "")
    expected = 6 * len(levels)
    check("the right number of tiles come out (%d)" % expected, count == expected,
          "got %d" % count)
    check("none of them repeat", len(seen) == count)


def test_the_wrap_seam_does_not_show_on_a_face():
    """The back face straddles the panorama's left/right join.

    Sampling past the edge has to come back in on the other side. Without that
    the face would be filled with edge-clamped smear down one side, which reads
    as a hard vertical band in the finished 360.
    """
    eq = _equirect_with_landmarks()
    size = T.face_size_for(eq.shape[1])
    back = T.cube_face(eq, "b", size)

    # A column of edge-clamped smear shows up as neighbouring columns that are
    # identical. Real content never is.
    mid = back[:, size // 2 - 40:size // 2 + 40].astype(np.int16)
    diffs = np.abs(np.diff(mid, axis=1)).mean()
    check("the seam-straddling face has real detail across the join (%.1f)" % diffs,
          diffs > 1.0, "near-zero means the edge was smeared, not wrapped")


def test_strips_do_not_change_the_picture():
    """Rendering a face in strips is a memory fix, not a visual one.

    A face is built a few rows at a time so its working set stops depending on
    the face size - about 48MB of float32 temporaries for a 1024 face, landing
    right at the worker's high-water mark on an instance that shares its memory
    with the API. Each row's projection is independent of every other, so the
    result must be identical to rendering the whole face at once.
    """
    eq = _equirect_with_landmarks()
    for face in T.FACES:
        whole = T.cube_face(eq, face, 256, strip_rows=256)
        strips = T.cube_face(eq, face, 256, strip_rows=64)
        check("face %s is identical however it is strip-rendered" % face,
              np.array_equal(whole, strips))


def test_tiles_are_skipped_on_a_small_instance():
    """Tiles are an optimisation, and the first thing to give up when memory is
    the binding constraint. Being restarted mid-job costs the user their
    capture; missing tiles costs them nothing they would notice."""
    import config

    original = config._cgroup_memory_limit_mb
    try:
        for limit, want in ((None, True), (512, False), (640, False),
                            (768, True), (2048, True)):
            config._cgroup_memory_limit_mb = lambda l=limit: l
            got = config._auto_generate_tiles()
            check("a %s host %s tiles" % (
                      "%dMB" % limit if limit else "memory-unlimited",
                      "cuts" if want else "skips"),
                  got is want, "got %s" % got)
    finally:
        config._cgroup_memory_limit_mb = original


if __name__ == "__main__":
    for fn in [test_faces_rebuild_the_panorama,
               test_landmarks_land_on_the_right_faces,
               test_the_pyramid_is_shaped_the_way_the_viewer_expects,
               test_cut_tiles_produces_every_tile_once,
               test_the_wrap_seam_does_not_show_on_a_face,
               test_strips_do_not_change_the_picture,
               test_tiles_are_skipped_on_a_small_instance]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
