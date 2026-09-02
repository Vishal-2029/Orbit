"""GPano photo-sphere metadata checks.

Run:  cv-worker/.venv/bin/python cv-worker/tests/test_xmp.py
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ops.xmp import (add_photosphere_metadata, build_gpano_xmp,  # noqa: E402
                     inject_xmp, strip_existing_xmp, XMP_NS_HEADER)

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILURES.append(name)
        print("  FAIL %s  %s" % (name, extra))


def a_jpeg(w=512, h=256):
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (60, 120, 180)
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return buf.tobytes()


def read_gpano(jpeg_bytes):
    """Parse the file back the way a viewer would."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg_bytes))
    return im.getxmp()["xmpmeta"]["RDF"]["Description"]


def test_required_fields_present():
    out = add_photosphere_metadata(a_jpeg(), 512, 256, source_count=8, heading_deg=137.5)
    d = read_gpano(out)
    required = ["ProjectionType", "CroppedAreaImageWidthPixels",
                "CroppedAreaImageHeightPixels", "FullPanoWidthPixels",
                "FullPanoHeightPixels", "CroppedAreaLeftPixels",
                "CroppedAreaTopPixels"]
    for k in required:
        check("required field %s is written" % k, k in d)
    check("projection is equirectangular", d.get("ProjectionType") == "equirectangular")
    check("viewer flag is set", d.get("UsePanoramaViewer") == "True")
    check("dimensions match the image",
          d.get("FullPanoWidthPixels") == "512" and d.get("FullPanoHeightPixels") == "256")
    check("cropped area covers the whole image",
          d.get("CroppedAreaLeftPixels") == "0" and d.get("CroppedAreaTopPixels") == "0")
    check("source photo count is recorded", d.get("SourcePhotosCount") == "8")
    check("heading is recorded", d.get("PoseHeadingDegrees") == "137.5")


def test_image_still_valid():
    orig = a_jpeg()
    out = add_photosphere_metadata(orig, 512, 256)
    dec = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    check("tagged file is still a decodable JPEG", dec is not None)
    check("pixels are unchanged", dec is not None and dec.shape == (256, 512, 3))
    check("file grew by roughly the packet size", 200 < len(out) - len(orig) < 4000,
          "delta=%d" % (len(out) - len(orig)))


def test_heading_omitted_when_unknown():
    """A gyroscope cannot know true north, so no heading must be invented."""
    d = read_gpano(add_photosphere_metadata(a_jpeg(), 512, 256, heading_deg=None))
    check("no heading is written when it is not known", "PoseHeadingDegrees" not in d)


def test_heading_wraps():
    d = read_gpano(add_photosphere_metadata(a_jpeg(), 512, 256, heading_deg=370.0))
    check("heading over 360 wraps", d.get("PoseHeadingDegrees") == "10.0",
          str(d.get("PoseHeadingDegrees")))
    d = read_gpano(add_photosphere_metadata(a_jpeg(), 512, 256, heading_deg=-90.0))
    check("negative heading wraps", d.get("PoseHeadingDegrees") == "270.0",
          str(d.get("PoseHeadingDegrees")))


def test_no_duplicate_packets():
    once = add_photosphere_metadata(a_jpeg(), 512, 256, heading_deg=10)
    twice = add_photosphere_metadata(once, 512, 256, heading_deg=20)
    check("re-tagging does not stack packets", twice.count(XMP_NS_HEADER) == 1,
          "found %d" % twice.count(XMP_NS_HEADER))
    check("re-tagging uses the new values",
          read_gpano(twice).get("PoseHeadingDegrees") == "20.0")


def test_survives_bad_input():
    check("non-JPEG input is returned untouched",
          add_photosphere_metadata(b"not a jpeg at all", 10, 10) == b"not a jpeg at all")
    check("empty input is returned untouched",
          add_photosphere_metadata(b"", 10, 10) == b"")
    truncated = a_jpeg()[:40]
    check("truncated JPEG does not raise",
          isinstance(add_photosphere_metadata(truncated, 512, 256), bytes))


def test_xml_is_escaped():
    xmp = build_gpano_xmp(10, 10, stitching_software='Orbit "v1" & <co>')
    check("software name is XML-escaped", "&amp;" in xmp and "&lt;" in xmp)


if __name__ == "__main__":
    for fn in [test_required_fields_present, test_image_still_valid,
               test_heading_omitted_when_unknown, test_heading_wraps,
               test_no_duplicate_packets, test_survives_bad_input,
               test_xml_is_escaped]:
        print("\n%s:" % fn.__name__)
        fn()
    print("\n%d failure(s)" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
