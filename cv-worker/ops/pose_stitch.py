"""Stitching from KNOWN camera rotations instead of guessing them.

The ordinary stitcher works backwards: it matches features between photos and
solves for where each camera must have been. That fails whenever two photos
share no recognisable detail - a blank wall, a plain sky, a dim corridor - and
when it fails the photo is silently dropped.

If the phone recorded its rotation when the shutter fired, none of that guessing
is necessary. Each photo has a known direction, so it can be projected straight
onto the sphere. A featureless wall places just as reliably as a bookshelf.

This is the approach Street View capture uses, and it is why it copes with
surfaces that defeat pure feature matching.
"""
import collections
import logging
import gc
import math

import cv2
import numpy as np

from config import settings
from ops.rotation_refine import refine_poses

log = logging.getLogger("orbit-worker")

# Rear-camera horizontal field of view to assume when the photos cannot measure
# it themselves. Phones vary (60-70 degrees); measured from a real capture's
# overlaps it came out at 63, where 65 had been assumed. Normally replaced per
# capture by rotation_refine.estimate_hfov.
DEFAULT_HFOV_DEG = 63.0

# Widest sphere we are willing to build, in pixels around the equator.
#
# This is the single most important limit in the file. The warp canvas is
# 2*pi*focal wide, and focal grows with the source photo's width, so full-size
# phone photos produce an enormous canvas: a 1600px-wide portrait shot gives a
# 7890px circumference and a 97 degree vertical span, and near the poles a
# spherical warp stretches without bound. Multi-band blending six of those
# allocated 19GB and got the worker killed by the kernel.
#
# The finished panorama is capped at 4096 wide anyway, so warping any larger
# throws the extra pixels away regardless.
MAX_CIRCUMFERENCE_PX = 4096

# Hard ceiling on the composite canvas. Anything past this means the geometry
# is wrong, not that the photo is detailed.
MAX_CANVAS_PX = 40_000_000

# Combined size of all warped tiles held in memory at once. Photos aimed at the
# sky or the floor warp toward a pole, where a spherical projection stretches
# without bound, so a handful of pole shots can dwarf the whole horizon ring.
MAX_TILE_PIXELS = 120_000_000


def quaternion_to_matrix(x, y, z, w):
    """Quaternion [x,y,z,w] -> 3x3 rotation matrix (device frame -> world)."""
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n == 0:
        return np.eye(3, dtype=np.float32)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ], dtype=np.float32)



class SphereGeometry(collections.namedtuple(
        "SphereGeometry", "circumference_px equator_y coverage hfov_deg")):
    """Where the world sits in a panorama this module built.

    circumference_px  pixels for one full turn, i.e. 2*pi*focal
    equator_y         the row the horizon falls on
    coverage          uint8 mask, same size as the panorama, non-zero exactly
                      where a photograph actually landed
    hfov_deg          the field of view the photos were warped with

    The mask is the important one. Finishing used to infer coverage from the
    pixels, by calling anything near-black "no data" and anything else real.
    That cannot tell a photographed dark corridor from unphotographed sky, and
    it cannot see that a row is half-covered at all - which is how a row full of
    real ceiling came to be classed as ragged and thrown away. The blender knows
    the truth exactly, because it was handed one mask per photo, so the truth is
    carried forward instead of being guessed at again downstream.

    The finishing stage cannot recover either of these from the pixels, and
    guessing them is what made a partial capture come out stretched over a whole
    sphere. They are cheap to carry, so they are carried.
    """
    __slots__ = ()


SphereGeometry.__new__.__defaults__ = (None, None)   # coverage, hfov_deg optional


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


def quaternion_from_heading(yaw_deg, pitch_deg=0.0):
    """Build a device quaternion from the compass heading and tilt of a shot.

    Not every photo arrives with a sensor quaternion - an older phone, a denied
    permission, a browser that does not expose the rotation vector - and until
    now that dropped the whole capture onto feature matching, which is the path
    that fails on blank walls. But the guided capture flow cannot work at all
    without knowing where the phone is pointing, so every frame it plans already
    carries a yaw and a pitch. That is enough to place a photo on the sphere.

    What is lost is ROLL: yaw and pitch cannot express a phone tilted sideways,
    so this assumes it was held square. For a guided capture, where the app
    shows a levelling hint and the user is turning on the spot, that is a much
    smaller error than not placing the photo at all.

    yaw_deg   compass bearing, 0 = the reference direction, measured CLOCKWISE
    pitch_deg 0 at the horizon, positive looking up
    """
    # Clockwise seen from above is a NEGATIVE rotation about the world's up
    # axis in a right-handed frame, hence the minus.
    hz = math.radians(-float(yaw_deg)) / 2.0
    q_yaw = [0.0, 0.0, math.sin(hz), math.cos(hz)]

    # A phone held upright is already a quarter turn about its own X axis: the
    # device's +Y (its top edge) has to end up pointing at the sky. Pitch is a
    # further rotation about the same axis, so the two simply add.
    hx = math.radians(90.0 + float(pitch_deg)) / 2.0
    q_upright_and_pitch = [math.sin(hx), 0.0, 0.0, math.cos(hx)]

    return _qmul(q_yaw, q_upright_and_pitch)


# OpenCV's spherical warper takes R as a CAMERA-TO-WORLD matrix: it maps an
# image pixel to R @ K^-1 @ p and reads the direction straight off that. It
# then measures azimuth as atan2(x, z), and height as the angle away from +Y
# with +Y at the BOTTOM of the panorama.
#
# Two changes of frame get us there from a phone quaternion, and nothing else.

# 1. OpenCV's camera frame (+X right, +Y DOWN, +Z FORWARD) expressed in the
#    phone's DEVICE frame (+X right, +Y up, +Z out of the screen towards the
#    user, so the rear camera looks along -Z). Two axes disagree.
_CAM_TO_DEVICE = np.diag([1.0, -1.0, -1.0]).astype(np.float32)

# 2. The phone's world frame is gravity-aligned, right-handed, +Z UP, and the
#    warper's world has +Y DOWN. Line them up:
#      X_warp = X_phone,  Y_warp = -Z_phone,  Z_warp = +Y_phone
#    so the phone's reference heading becomes the warper's +Z and "up" becomes
#    -Y.
#
#    The vertical sign decides whether the sky ends up overhead or underfoot.
#    The HANDEDNESS decides something subtler, and getting it wrong is the bug
#    this replaced. The old chain transposed the phone rotation and used the
#    inverse of this matrix. That is still a valid rotation, so every geometric
#    check kept passing - the photos were evenly spaced, the spacing was
#    consistent, the poles were the right way up - but azimuth ran BACKWARDS.
#    A photo taken while turning right was placed to the left, so the finished
#    360 was the scene reversed end to end. Each tile's own content stayed
#    upright, which is why it never looked obviously broken; you had to compare
#    it against the room to see it.
#
#    It is pinned now by rendering a synthetic panorama with landmarks at 0,
#    90, 180 and 270 degrees and requiring them to come back in that order.
_PHONE_TO_WARPER = np.array([[1, 0, 0],
                             [0, 0, -1],
                             [0, 1, 0]], dtype=np.float32)


def camera_rotation(quat):
    """Device quaternion -> the camera-to-world matrix OpenCV's warper wants.

    No half-turn correction. An earlier version applied one to stop the user's
    starting direction landing on the wrap seam, but that was only necessary
    because the transposed chain above had put it there. With the frames
    converted correctly the reference heading sits mid-panorama on its own and
    the seam falls behind the user, where it belongs.
    """
    r_world_from_device = quaternion_to_matrix(*quat)
    return (_PHONE_TO_WARPER @ r_world_from_device @ _CAM_TO_DEVICE).astype(np.float32)


def intrinsics(width, height, hfov_deg=DEFAULT_HFOV_DEG):
    """Pinhole camera matrix and focal length in pixels."""
    f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0, width / 2.0],
                     [0, f, height / 2.0],
                     [0, 0, 1]], dtype=np.float32), f


def _predicted_tile_pixels(rotations, K, focal, w, h):
    """How many pixels the warped tiles will occupy, without warping anything.

    warpRoi runs the same projection on the frame's corners only, so this costs
    microseconds and answers the one question that has to be answered BEFORE the
    first allocation: does the whole capture fit?
    """
    warper = cv2.PyRotationWarper("spherical", focal)
    total = 0
    for R in rotations:
        try:
            _, _, rw, rh = warper.warpRoi((w, h), K, R)
        except cv2.error:
            rw, rh = w, h
        total += int(rw) * int(rh)
    return total


def _plan_scale(rotations, w, h, hfov_deg, budget_px):
    """Shrink the whole sphere until every photo fits, rather than dropping some.

    The budget used to be enforced mid-loop: warp until it is exhausted, then
    break and keep whatever had been placed. Photos are ordered by yaw, so what
    survived was an arbitrary arc, and it was arbitrary in the worst possible
    way - a shot aimed at a pole warps to an enormous tile, because a spherical
    projection diverges there, so the ceiling and floor frames ate the budget
    and every photo after them in yaw order was discarded. A multi-ring capture
    lost whole rings that way and had them fabricated back at the finish.

    Dropping a photo removes part of the world permanently. Halving the
    resolution costs detail everywhere and removes nothing. Between those two
    the choice is not close, so the sphere is scaled to fit instead.

    Returns the extra scale factor to apply to the sources (<= 1.0).
    """
    scale = 1.0
    for _ in range(6):                       # 1, 1/2, 1/4 ... plenty of room
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        K, focal = intrinsics(sw, sh, hfov_deg)
        need = _predicted_tile_pixels(rotations, K, focal, sw, sh)
        if need <= budget_px:
            if scale < 1.0:
                log.info("[orbit-worker] %d photos would warp to %.0f Mpx; scaling "
                         "the sphere by %.2f to fit the %.0f Mpx budget with every "
                         "photo kept", len(rotations), need / 1e6, scale, budget_px / 1e6)
            return scale
        scale *= 0.75
    log.warning("[orbit-worker] even at %.2f scale these %d photos exceed the "
                "tile budget; proceeding and letting the canvas guard decide",
                scale, len(rotations))
    return scale


def stitch_with_poses(images, quats, hfov_deg=DEFAULT_HFOV_DEG):
    """Project photos onto a sphere using their recorded rotations.

    images: list of BGR arrays.
    quats:  list of [x,y,z,w] or None, one per image.

    Returns (ok, panorama_or_None, reason_or_None, SphereGeometry_or_None).
    Never raises.
    """
    usable = [i for i, q in enumerate(quats) if q is not None]
    if not usable:
        return False, None, "No photo carries camera-rotation data.", None

    try:
        h, w = images[usable[0]].shape[:2]
        src_w = w

        # Where the phone SAYS each camera was, before the photographs get a
        # say. Everything below - the budget prediction and the warp itself -
        # uses these matrices rather than re-deriving them from the quaternions,
        # so refining them refines what is actually warped.
        rotations = [camera_rotation(quats[i]) for i in usable]
        # Where each photo's optical centre sits, relative to the middle of
        # the frame, in source pixels. Zero until the photos say otherwise.
        shifts = [(0.0, 0.0)] * len(usable)

        # A compass drifts a few degrees over a couple of minutes of turning,
        # and at 4096 pixels around a degree is eleven pixels. Worse, every
        # photo is a stabilised video frame whose centre has been cropped off
        # to one side. Either one puts the same window frame in two places and
        # leaves the blender showing both. The photos constrain both far better
        # than the sensor does, so they are asked - with the sensor still
        # setting which way is up and which way is north. The field of view is
        # measured on the way, and replaces the assumed one.
        if settings.refine_rotations and len(usable) >= 3:
            rotations, shifts, hfov_deg, _ = refine_poses(
                [images[i] for i in usable], rotations, hfov_deg)

        # Shrink the sources so the sphere stays within budget. Done after the
        # field of view is known, because the focal length decides the size.
        _, full_focal = intrinsics(w, h, hfov_deg)
        scale = min(1.0, settings.pose_circumference_px / (2 * math.pi * full_focal))

        # And again for the tile budget, so no photo has to be abandoned later.
        scale *= _plan_scale(rotations, max(1, int(w * scale)),
                             max(1, int(h * scale)), hfov_deg,
                             settings.pose_tile_budget_px)
        if scale < 1.0:
            w, h = max(1, int(w * scale)), max(1, int(h * scale))
            log.info("[orbit-worker] scaling sources by %.2f to %dx%d "
                     "so the sphere stays within %d px around",
                     scale, w, h, settings.pose_circumference_px)

        K, focal = intrinsics(w, h, hfov_deg)
        warper = cv2.PyRotationWarper("spherical", focal)

        warped, masks, corners = [], [], []
        running_px = 0
        k_scale = w / float(src_w)
        for i, R, (dx, dy) in zip(usable, rotations, shifts):
            img = images[i]
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            # No half-turn of the source here either. It used to be needed to
            # cancel one the warper appeared to apply; that apparent rotation
            # was the transposed camera_rotation above, and the two errors hid
            # each other.
            #
            # R comes from the refined set, not from the quaternion, and each
            # photo gets its own camera matrix: the shared focal length, with
            # the centre where stabilisation actually left it.
            K_i = K.copy()
            K_i[0, 2] += dx * k_scale
            K_i[1, 2] += dy * k_scale

            corner, wimg = warper.warp(img, K_i, R, cv2.INTER_LINEAR, cv2.BORDER_REFLECT)
            solid = np.full((h, w), 255, dtype=np.uint8)
            _, wmask = warper.warp(solid, K_i, R, cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)

            warped.append(wimg)
            masks.append(wmask)
            corners.append(corner)

            # _plan_scale already sized the sphere so the whole capture fits,
            # so reaching this is a prediction that went wrong rather than a
            # capture that was always too big. Stopping here still costs the
            # user part of their world, so it is a last resort and it says so
            # loudly - it is not the routine path it used to be.
            running_px += wimg.shape[0] * wimg.shape[1]
            if running_px > settings.pose_tile_budget_px * 1.5:
                log.error("[orbit-worker] warped tiles reached %.0f Mpx after %d of "
                          "%d photos despite planning for %.0f Mpx; stopping to "
                          "avoid an OOM kill, and this capture will be short of "
                          "the sphere",
                          running_px / 1e6, len(warped), len(usable),
                          settings.pose_tile_budget_px / 1e6)
                del warped[-1], masks[-1], corners[-1]
                running_px -= wimg.shape[0] * wimg.shape[1]
                del wimg, wmask
                gc.collect()
                break

        # One is enough. A single photo placed by its recorded rotation is a
        # real, correctly oriented piece of the sphere — the rest of the sphere
        # is simply not photographed, which the viewer already renders as a
        # blur rather than as an error. Requiring two here meant a one-photo
        # capture produced nothing at all.
        if not warped:
            return False, None, ("These photos are too large to place onto a sphere. "
                                 "Try again with fewer or smaller photos."), None

        pano, cover, _, y0 = _blend(warped, masks, corners)
        if pano is None:
            return False, None, "The photos could not be combined onto the sphere.", None

        # Both of these fall straight out of the projection. The warper lays the
        # sphere out at `focal` pixels per radian, so a full turn is 2*pi*focal
        # wide and the horizon - the ray with no vertical component - lands at
        # focal * pi/2. Subtracting the canvas origin puts that in image rows.
        geom = SphereGeometry(circumference_px=2.0 * math.pi * focal,
                              equator_y=focal * math.pi / 2.0 - y0,
                              coverage=cover, hfov_deg=hfov_deg)

        log.info("[orbit-worker] pose stitch placed %d photo(s) from recorded "
                 "rotations; one turn is %.0f px, horizon at row %.0f",
                 len(warped), geom.circumference_px, geom.equator_y)
        return True, pano, None, geom

    except MemoryError:
        log.warning("[orbit-worker] pose stitch ran out of memory")
        return False, None, "These photos are too large to place onto a sphere.", None
    except cv2.error as e:
        log.warning("[orbit-worker] pose stitch cv2 error: %s", e)
        return False, None, "The camera rotations recorded with these photos could not be used.", None
    except Exception as e:
        log.warning("[orbit-worker] pose stitch %s: %s", type(e).__name__, e)
        return False, None, "Something went wrong placing the photos onto the sphere.", None


def _compensate_exposure(warped, masks, corners):
    """Even out brightness between shots.

    Phones re-meter for every photo, so the sunlit side and the shaded side
    arrive at different exposures and every seam shows as a brightness step.

    One gain per photo, not per block. GAIN_BLOCKS builds a grid of gain maps
    across every tile at full resolution, which on a 13-photo sphere - where
    the shots aimed at the ceiling and floor warp into very large tiles -
    exhausted memory and got the worker killed. A single gain is also the right
    correction for the problem we actually have: whole-frame metering drift,
    not vignetting.

    Applied in place; failure here is cosmetic, so it is swallowed.
    """
    try:
        comp = cv2.detail.ExposureCompensator_createDefault(
            cv2.detail.ExposureCompensator_GAIN)
        comp.feed(corners, warped, masks)
        for i in range(len(warped)):
            comp.apply(i, corners[i], warped[i], masks[i])
    except Exception as e:
        log.debug("[orbit-worker] exposure compensation skipped: %s", e)


# Seam finding runs on tiny copies. OpenCV's own stitcher does the same, at
# about a tenth of a megapixel: the cut line only needs to be roughly right,
# and the finder is expensive enough that running it at full size is what
# pushed a 13-photo capture past 8GB.
# Resolution the seam is searched at, per photo. The seam mask is found here
# and scaled back up, so this number decides how finely the cut can follow real
# edges in the scene. At 0.1 the cut was found at roughly a fifth scale and
# arrived back as a visible staircase along building edges.
SEAM_WORK_MEGAPIX = settings.seam_work_megapix


def _find_seams(warped, masks, corners):
    """Choose where each pair of overlapping photos should hand over.

    Without this, every overlap is a wide cross-fade of two photos. Anything
    that does not line up perfectly - and handheld photos never do - is
    averaged into a soft double image, which reads as a blurred band wherever
    two photos meet. A seam finder instead picks a cut through the overlap
    where the two photos agree most closely, so the join is a clean handover.

    Masks are edited in place. Failure is non-fatal and simply leaves the
    plain cross-fade behaviour.
    """
    try:
        total_px = sum(im.shape[0] * im.shape[1] for im in warped)
        scale = min(1.0, math.sqrt(SEAM_WORK_MEGAPIX * 1e6 * len(warped) / max(1, total_px)))

        small_imgs, small_masks, small_corners = [], [], []
        for im, mk, c in zip(warped, masks, corners):
            sw = max(8, int(im.shape[1] * scale))
            sh = max(8, int(im.shape[0] * scale))
            small_imgs.append(cv2.resize(im, (sw, sh), interpolation=cv2.INTER_AREA)
                              .astype(np.float32))
            small_masks.append(cv2.UMat(
                cv2.resize(mk, (sw, sh), interpolation=cv2.INTER_NEAREST)))
            small_corners.append((int(c[0] * scale), int(c[1] * scale)))

        cv2.detail_DpSeamFinder("COLOR_GRAD").find(
            small_imgs, small_corners, small_masks)

        # Scale each seam mask back up and intersect with the real coverage, so
        # the seam can only ever remove pixels, never invent them.
        #
        # The upscaled mask is kept soft rather than thresholded back to black
        # and white. Rounding it to a hard edge re-quantised the boundary onto
        # the low-resolution grid it was found on, which is what put a visible
        # staircase along the join. MultiBandBlender normalises by total weight,
        # so a soft mask simply hands over gradually across those few pixels.
        for i, um in enumerate(small_masks):
            grown = cv2.resize(um.get(), (masks[i].shape[1], masks[i].shape[0]),
                               interpolation=cv2.INTER_LINEAR)
            # Blur by roughly the size of one low-resolution pixel, so the
            # handover spans the uncertainty in where the seam actually is.
            k = max(1, int(round(1.0 / max(scale, 1e-6))))
            if k > 1:
                grown = cv2.GaussianBlur(grown, (0, 0), sigmaX=k / 2.0)
            masks[i] = np.minimum(masks[i], grown).astype(np.uint8)
        return True
    except Exception as e:
        log.debug("[orbit-worker] seam finding skipped: %s", e)
        return False


def _blend(warped, masks, corners):
    """Multi-band blend the warped images onto one canvas.

    Returns (image, coverage, x0, y0). The canvas origin comes back because the
    caller needs it to say where the horizon ended up, and the coverage mask
    because it is the only honest record of which pixels were photographed -
    the blended pixels themselves cannot be told apart from an unlit corner.
    """
    sizes = [(im.shape[1], im.shape[0]) for im in warped]
    x0 = min(c[0] for c in corners)
    y0 = min(c[1] for c in corners)
    x1 = max(c[0] + s[0] for c, s in zip(corners, sizes))
    y1 = max(c[1] + s[1] for c, s in zip(corners, sizes))
    if x1 <= x0 or y1 <= y0:
        return None, None, 0, 0
    # A canvas this large means the geometry is wrong, not that the photo is
    # detailed. Refuse rather than trying to allocate gigabytes.
    if (x1 - x0) * (y1 - y0) > MAX_CANVAS_PX:
        log.warning("[orbit-worker] pose stitch canvas %dx%d is implausible; refusing",
                    x1 - x0, y1 - y0)
        return None, None, 0, 0

    _compensate_exposure(warped, masks, corners)

    # Taken BEFORE seam finding, which erodes these masks down to one photo per
    # pixel. What we want here is the opposite question - was this pixel seen by
    # ANY photo - so it has to be answered while the overlaps are still intact.
    cover = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    for mask, (cx, cy) in zip(masks, corners):
        mh, mw = mask.shape[:2]
        ys, xs = cy - y0, cx - x0
        region = cover[ys:ys + mh, xs:xs + mw]
        np.maximum(region, mask[:region.shape[0], :region.shape[1]], out=region)

    seamed = _find_seams(warped, masks, corners)

    blender = cv2.detail_MultiBandBlender()
    # The number of bands sets how wide the handover between two photos is, in
    # pixels, and it has to scale with the canvas: a fixed 3 gave roughly an
    # eight-pixel transition on a four-thousand-pixel panorama, far too abrupt
    # to hide a join, so every seam stayed visible as a hard line.
    #
    # This is the rule OpenCV's own Stitcher uses - a blend width of 5% of the
    # square root of the canvas area - which lands around 6 bands here.
    blend_width = math.sqrt(float((x1 - x0) * (y1 - y0))) * 0.05
    bands = int(math.ceil(math.log(max(blend_width, 2.0)) / math.log(2.0))) - 1
    bands = max(3, min(7, bands))
    if not seamed:
        # No seam was chosen, so the overlap is a plain cross-fade and wants a
        # wider one to avoid a visible edge.
        bands = min(7, bands + 1)
    log.info("[orbit-worker] blending %d photos onto %dx%d with %d bands",
             len(warped), x1 - x0, y1 - y0, bands)
    blender.setNumBands(bands)
    blender.prepare((x0, y0, x1 - x0, y1 - y0))
    for im, mask, corner in zip(warped, masks, corners):
        blender.feed(im.astype(np.int16), mask, corner)
    result, _ = blender.blend(None, None)
    return cv2.convertScaleAbs(result), cover, x0, y0
