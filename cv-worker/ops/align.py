"""Ring ordering helpers used before panorama stitching."""


def sort_ring_by_yaw(frames):
    """frames: list of dicts with at least 'yaw'. Returns a new list sorted by
    yaw ascending, which is the order cv2.Stitcher wants for a ring capture.

    A missing or null yaw sorts as 0. The key has to coerce rather than rely on
    dict.get's default: a frame that recorded no heading arrives with the key
    present and set to null, which the default never sees, and comparing None
    against a float would take the whole finalize job down with a TypeError.
    """
    return sorted(frames, key=lambda f: f.get("yaw") or 0.0)
