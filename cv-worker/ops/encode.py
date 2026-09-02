"""JPEG encode helpers shared by frame and panorama paths."""
import cv2


def encode_jpeg_bytes(img, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()
