"""Working out which photos actually belong to the same scene.

cv2.Stitcher will happily succeed on a SUBSET of the photos it is given: it
finds the largest group that matches, stitches those, and silently discards the
rest. The caller gets status OK and no indication that two thirds of the input
never made it in.

That is how a 12-photo capture turns into a narrow strip stretched across a
whole sphere, which is exactly what "the 360 view is not proper" looks like.

This module runs the matching step first, so we know before stitching which
photos form one connected scene and can tell the user the truth about the rest.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger("orbit-worker")

# Below this many confirmed inlier matches, two photos are not considered to
# show overlapping parts of the same scene.
#
# Two passes are tried. The strict one keeps genuinely unrelated photos apart.
# If that leaves the set fragmented, a looser threshold gets a second chance to
# join groups that overlap only slightly - which is common when a user shoots
# an uneven ring by hand. Joining on weak evidence is still better than
# discarding two thirds of their photos.
MATCH_CONF = 0.35
MATCH_CONF_RELAXED = 0.20


def _detector():
    """SIFT when the build has it (much better on real photos), else ORB."""
    if hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=1200)
    return cv2.ORB_create(nfeatures=1500)


def connected_groups(images, work_width=640):
    """Group photo indices by whether they overlap each other.

    Returns a list of index lists, largest group first. Photos that match
    nothing end up in their own single-element group.

    Runs a strict pass first; if that leaves most of the photos stranded, it
    retries with a looser match threshold before accepting the fragmentation.
    """
    n = len(images)
    if n < 2:
        return [list(range(n))]

    # Matching on downscaled copies: the geometry we need is unaffected and it
    # is several times faster on phone-sized photos.
    small = []
    for img in images:
        h, w = img.shape[:2]
        scale = work_width / float(w)
        small.append(cv2.resize(img, (work_width, max(1, int(h * scale))),
                                interpolation=cv2.INTER_AREA)
                     if scale < 1 else img)

    det = _detector()
    try:
        features = [cv2.detail.computeImageFeatures2(det, im) for im in small]
    except cv2.error as e:
        log.warning("[orbit-worker] coverage check unavailable (%s); "
                    "assuming all photos belong together", e)
        return [list(range(n))]

    out = _match_graph(features, n, MATCH_CONF)

    # If the strict pass stranded most of the photos, give them a second chance
    # on weaker evidence before writing two thirds of the capture off.
    if out and len(out[0]) < n * 0.6:
        relaxed = _match_graph(features, n, MATCH_CONF_RELAXED)
        if relaxed and len(relaxed[0]) > len(out[0]):
            log.info("[orbit-worker] strict matching connected only %d of %d; "
                     "a looser pass connected %d", len(out[0]), n, len(relaxed[0]))
            out = relaxed
    log.info("[orbit-worker] coverage: %d photo(s) form %d group(s): %s",
             n, len(out), [len(g) for g in out])
    return out


def _match_graph(features, n, conf):
    """Union-find over pairs a matcher confirms at the given confidence."""
    matcher = cv2.detail_BestOf2NearestMatcher(False, conf)
    pairwise = matcher.apply2(features)
    matcher.collectGarbage()

    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        for j in range(i + 1, n):
            m = pairwise[i * n + j]
            if m.confidence >= 1.0 and m.num_inliers >= 6:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def describe_leftovers(total, used):
    """Plain-English explanation of why some photos were left out."""
    missing = total - used
    if missing <= 0:
        return None
    return (
        f"Only {used} of your {total} photos could be joined together. "
        f"The other {missing} did not overlap with the rest, so they were left out. "
        "This usually means the photos were taken in more than one place, or with "
        "gaps between them. Shoot one continuous turn from a single spot, letting "
        "each photo overlap the previous one by about a third."
    )


def sphere_coverage(quats, hfov_deg=65.0, aspect=None):
    """Fraction of the whole sphere that at least one photo actually saw.

    This is the number behind the blurred bands. Four photos taken 90 degrees
    apart, from a camera that only sees 65, leave a quarter of the world
    unphotographed - and no amount of processing can put that back. Measuring it
    lets the app say so plainly instead of quietly smearing over the hole.

    A world direction counts as seen when it falls inside a photo's rectangular
    frustum, tested in that camera's own frame. Testing an angular radius
    instead would wrongly count the corners of the field of view as covering
    everything between them.
    """
    import numpy as np

    mats = []
    for q in quats:
        if q is None:
            continue
        x, y, z, w = q
        n = math.sqrt(x * x + y * y + z * z + w * w)
        if n == 0:
            continue
        x, y, z, w = x / n, y / n, z / n, w / n
        # world <- device
        R = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ])
        mats.append(R.T)           # device <- world
    if not mats:
        return 0.0

    vfov = hfov_deg if not aspect else 2 * math.degrees(
        math.atan(math.tan(math.radians(hfov_deg) / 2) * aspect))
    tan_h = math.tan(math.radians(hfov_deg) / 2)
    tan_v = math.tan(math.radians(vfov) / 2)

    n_lat, n_lon = 90, 180
    lats = (np.arange(n_lat) + 0.5) * math.pi / n_lat - math.pi / 2
    lons = (np.arange(n_lon) + 0.5) * 2 * math.pi / n_lon
    LA, LO = np.meshgrid(lats, lons, indexing="ij")
    pts = np.stack([np.cos(LA) * np.cos(LO),
                    np.cos(LA) * np.sin(LO),
                    np.sin(LA)], axis=-1).reshape(-1, 3)
    weights = np.cos(LA).reshape(-1)

    seen = np.zeros(len(pts), dtype=bool)
    for Rt in mats:
        d = pts @ Rt.T                      # direction in the device frame
        depth = -d[:, 2]                    # camera looks along device -Z
        with np.errstate(divide="ignore", invalid="ignore"):
            inside = ((depth > 1e-6)
                      & (np.abs(d[:, 0]) <= tan_h * depth)
                      & (np.abs(d[:, 1]) <= tan_v * depth))
        seen |= inside

    total = weights.sum()
    return float((weights * seen).sum() / total) if total else 0.0
