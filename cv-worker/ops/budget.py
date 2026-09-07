"""Deciding how big the source photos may be, before any of them are loaded.

Every other memory guard in this worker caps something DURING the stitch - the
warped tiles, the compositing resolution, the seam search. None of them help
with the thing that actually killed the job: a stitch needs every source photo
resident at once, so the floor under the whole operation is

    photo count  x  width  x  height  x  3 bytes

and nothing downstream can claw that back. Thirty-one photos at 1600px wide is
317MB before the stitcher has allocated anything of its own, on an instance
whose entire limit is 512MB and which is also running the API. The job was
OOM-killed, retried, killed again, and the capture came out as a frame sequence
with "the photos were too large to process".

The fix is to decide the working size from the memory that exists and the number
of photos there are, rather than from a fixed setting that was chosen when
captures were eight photos. A ring of thirty at 900px still stitches - there is
far more overlap than the stitcher needs - and it fits.

This is deliberately pessimistic. Being restarted mid-job costs the user their
capture; a slightly softer panorama costs them almost nothing.
"""
import logging
import math

log = logging.getLogger("orbit-worker")

# What the rest of the box needs while the worker is stitching, in MiB.
#
#   ~60   the Go API next to us in the same container (measured at 23MB idle,
#         with headroom for concurrent share-link traffic)
#   ~140  the Python interpreter, numpy and OpenCV's own libraries, before a
#         single photo is decoded
#
# Anything left over is what a capture may actually use.
HOST_RESERVE_MB = 200

# Share of what remains that the SOURCE PHOTOS may occupy. The stitcher needs
# the rest for warped tiles, masks, the blend pyramid and the finished
# panorama, all of which are already capped against the same limit.
SOURCE_SHARE = 0.35

# Never go below this: a photo this small stops carrying enough detail for
# feature matching to work at all, and a failed stitch is worse than a soft one.
MIN_WIDTH = 640

# Never bother going above what the capture asked for.
DEFAULT_MAX_WIDTH = 1600

# Assumed portrait aspect, height / width. Phone captures are portrait, and
# guessing high is the safe direction: it over-estimates the memory each photo
# needs and therefore picks a smaller width.
ASSUMED_ASPECT = 4.0 / 3.0


def source_budget_bytes(memory_limit_mb):
    """How much memory the decoded source photos may occupy in total."""
    if memory_limit_mb is None:
        return None                                   # unconstrained host
    usable = max(0, memory_limit_mb - HOST_RESERVE_MB)
    return int(usable * SOURCE_SHARE * 1024 * 1024)


def source_width(count, want_width=DEFAULT_MAX_WIDTH, memory_limit_mb=None,
                 attempt=1):
    """The width to load each source photo at.

    count           how many photos the stitch needs resident at once
    want_width      the width they were stored at
    memory_limit_mb the container's ceiling, or None when there is not one
    attempt         1 on the first try; higher after a memory failure, which
                    halves the budget each time so a retry is actually
                    different from the attempt that just died

    Returns want_width unchanged when there is room, so the common case - a
    normal capture on a normal host - is untouched.
    """
    budget = source_budget_bytes(memory_limit_mb)
    if budget is None or count <= 0:
        return want_width

    # Each retry after a memory failure gets half the room. Retrying at the
    # same size just fails the same way, three times, slowly.
    budget = int(budget / (2 ** max(0, attempt - 1)))

    per_photo = budget / float(count)
    # bytes = w * (w * aspect) * 3
    width = int(math.sqrt(per_photo / (3.0 * ASSUMED_ASPECT)))

    # Down to a multiple of 16. Codecs and resizes are happier, and it stops the
    # number looking more precise than the estimate behind it.
    width = (width // 16) * 16
    return max(MIN_WIDTH, min(want_width, width))


def plan(count, want_width=DEFAULT_MAX_WIDTH, memory_limit_mb=None, attempt=1):
    """source_width, plus a line worth logging about why."""
    width = source_width(count, want_width, memory_limit_mb, attempt)
    if width >= want_width:
        return width, None
    est = count * width * width * ASSUMED_ASPECT * 3 / (1024 * 1024)
    was = count * want_width * want_width * ASSUMED_ASPECT * 3 / (1024 * 1024)
    return width, (
        "%d photos at %dpx would need about %.0f MB resident at once, which "
        "does not fit in a %s MB instance; loading them at %dpx instead "
        "(about %.0f MB)" % (count, want_width, was,
                             memory_limit_mb or "?", width, est))
