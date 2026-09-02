"""Ring ordering helpers used before panorama stitching."""


def sort_ring_by_yaw(frames):
    """frames: list of dicts with at least 'yaw'. Returns a new list sorted by
    yaw ascending, which is the order cv2.Stitcher wants for a ring capture.
    """
    return sorted(frames, key=lambda f: f.get("yaw", 0.0))
