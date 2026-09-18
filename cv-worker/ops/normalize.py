"""Frame-level image ops: EXIF-safe decode, resize, colour normalisation,
centroid alignment for spin mode, and JPEG/thumb encoding.
"""
import io
import logging
import math

import cv2
import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger("orbit-worker")


def decode_with_exif_rotation(raw_bytes: bytes) -> np.ndarray:
    """Decode JPEG bytes, apply EXIF orientation, strip all metadata, return BGR ndarray."""
    with Image.open(io.BytesIO(raw_bytes)) as im:
        im = ImageOps.exif_transpose(im)  # bakes in rotation, drops orientation tag need
        im = im.convert("RGB")
        arr = np.array(im)  # RGB
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return bgr


# The diagonal of a 35mm frame, which is what "35mm equivalent focal length"
# is measured against.
_FILM_DIAGONAL_MM = 43.27


def exif_hfov(raw_bytes):
    """The photo's horizontal field of view in degrees, from its EXIF, or None.

    A phone records the 35mm-equivalent focal length of the lens it actually
    used, which settles in one number what the photographs themselves can only
    guess at: on a real capture the field of view measured from overlaps was
    flat from 73 to 82 degrees - no peak to find - while the EXIF said 78.1, and
    the same lens on another capture agreed.

    Horizontal means across the SHORT side for an upright photo, because that is
    what intrinsics() calls width.
    """
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(raw_bytes)) as im:
            exif = im.getexif()
            w, h = im.size
            focal35 = None
            try:
                focal35 = exif.get_ifd(0x8769).get(0xA405)
            except Exception:
                focal35 = exif.get(0xA405)
        if not focal35 or not w or not h:
            return None
        focal35 = float(focal35)
        if focal35 <= 1:
            return None
        short = min(w, h)
        sensor = _FILM_DIAGONAL_MM * short / math.hypot(w, h)
        return math.degrees(2.0 * math.atan(sensor / 2.0 / focal35))
    except Exception:
        return None


def resize_to_width(img: np.ndarray, target_width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if w == 0 or h == 0:
        raise ValueError("decoded image has zero dimension")
    if w == target_width:
        return img
    scale = target_width / float(w)
    new_h = max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4
    return cv2.resize(img, (target_width, new_h), interpolation=interp)


def normalize_color(img: np.ndarray) -> np.ndarray:
    """Simple, robust colour normalisation: CLAHE on the L channel of LAB.
    Evens out exposure/white-balance drift between shots without distorting hue.
    """
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l2 = clahe.apply(l)
    merged = cv2.merge((l2, a, b))
    return cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)


def centroid(img: np.ndarray):
    """Rough foreground centroid via Otsu threshold on grayscale, used to
    re-center a spin-mode subject that drifted slightly between shots."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    m = cv2.moments(mask)
    if m["m00"] == 0:
        h, w = gray.shape
        return w / 2.0, h / 2.0
    cx = m["m10"] / m["m00"]
    cy = m["m01"] / m["m00"]
    return cx, cy


def align_to_centroid(img: np.ndarray, ref_cx: float, ref_cy: float) -> np.ndarray:
    """Shift the image so its subject centroid lands on the reference centroid.
    Cheap stand-in for full feature-based registration; good enough for a
    turntable subject that jitters a few pixels between shots.
    """
    h, w = img.shape[:2]
    cx, cy = centroid(img)
    dx, dy = ref_cx - cx, ref_cy - cy
    # Clamp translation so a bad threshold read can't fling the frame off-canvas.
    max_shift = 0.15 * max(w, h)
    dx = float(np.clip(dx, -max_shift, max_shift))
    dy = float(np.clip(dy, -max_shift, max_shift))
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)


def encode_jpeg(img: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def make_thumb(img: np.ndarray, target_width: int) -> np.ndarray:
    return resize_to_width(img, target_width)
