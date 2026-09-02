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
