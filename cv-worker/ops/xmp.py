"""Google Photo Sphere (GPano) XMP metadata.

A 2:1 equirectangular JPEG is just a wide photo until something tells a viewer
what it actually is. GPano is that something: a small XMP packet that marks the
file as a photo sphere, so Google Maps, Google Photos, Facebook and any
photo-sphere viewer open it as a draggable 360 instead of a flat image.

Spec: https://developers.google.com/streetview/spherical-metadata
"""
import logging
import struct
import xml.sax.saxutils as sx

log = logging.getLogger("orbit-worker")

XMP_NS_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_SOI = b"\xff\xd8"
_APP1 = b"\xff\xe1"
_SOS = b"\xff\xda"

# Segments that carry no length field and must not be parsed as if they did.
_STANDALONE = {0xD8, 0xD9} | set(range(0xD0, 0xD8))


def build_gpano_xmp(width, height, source_count=None, heading_deg=None,
                    stitching_software="Orbit", initial_heading_deg=None):
    """XMP packet describing a FULL equirectangular photo sphere.

    The image we ship is already padded to exactly 2:1, so the cropped area is
    the whole image: left and top are 0 and the cropped size equals the full
    pano size.

    heading_deg is the compass bearing of the image centre, clockwise from true
    North. Pass None when it is not genuinely known - a gyroscope alone cannot
    tell you where North is, and a made-up bearing would point Google Maps at
    the wrong part of the world.
    """
    parts = [
        ("GPano:UsePanoramaViewer", "True"),
        ("GPano:ProjectionType", "equirectangular"),
        ("GPano:CroppedAreaImageWidthPixels", str(int(width))),
        ("GPano:CroppedAreaImageHeightPixels", str(int(height))),
        ("GPano:FullPanoWidthPixels", str(int(width))),
        ("GPano:FullPanoHeightPixels", str(int(height))),
        ("GPano:CroppedAreaLeftPixels", "0"),
        ("GPano:CroppedAreaTopPixels", "0"),
    ]
    if stitching_software:
        parts.append(("GPano:StitchingSoftware", sx.escape(str(stitching_software))))
    if source_count:
        parts.append(("GPano:SourcePhotosCount", str(int(source_count))))
    if heading_deg is not None:
        parts.append(("GPano:PoseHeadingDegrees", "%.1f" % (float(heading_deg) % 360.0)))
    if initial_heading_deg is not None:
        parts.append(("GPano:InitialViewHeadingDegrees", str(int(initial_heading_deg) % 360)))

    attrs = "\n      ".join('%s="%s"' % (k, v) for k, v in parts)
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '    <rdf:Description rdf:about=""\n'
        '      xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"\n'
        '      %s/>\n'
        '  </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>' % attrs
    )


def _segments(data):
    """Yield (marker_byte, start, end) for each JPEG segment before the scan."""
    i = 2  # skip SOI
    n = len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            return
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: image data follows, stop here
            yield marker, i, i
            return
        if marker in _STANDALONE:
            yield marker, i, i + 2
            i += 2
            continue
        if i + 4 > n:
            return
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        end = i + 2 + seg_len
        yield marker, i, end
        i = end


def strip_existing_xmp(data):
    """Remove any XMP APP1 segments so we never write a second, conflicting one."""
    out = bytearray(data[:2])
    last = 2
    removed = 0
    for marker, start, end in _segments(data):
        if marker == 0xDA:
            break
        if marker == 0xE1 and data[start + 4:start + 4 + len(XMP_NS_HEADER)] == XMP_NS_HEADER:
            out += data[last:start]
            last = end
            removed += 1
    out += data[last:]
    if removed:
        log.debug("[orbit-worker] removed %d existing XMP segment(s)", removed)
    return bytes(out)


def inject_xmp(jpeg_bytes, xmp_str):
    """Insert an XMP APP1 segment into a JPEG. Returns the new bytes.

    On anything unexpected the original bytes come back unchanged: metadata is
    a nice-to-have and must never cost us the panorama itself.
    """
    try:
        if not jpeg_bytes.startswith(_SOI):
            log.warning("[orbit-worker] not a JPEG; skipping XMP")
            return jpeg_bytes

        data = strip_existing_xmp(jpeg_bytes)
        payload = XMP_NS_HEADER + xmp_str.encode("utf-8")
        seg_len = len(payload) + 2
        if seg_len > 0xFFFF:
            log.warning("[orbit-worker] XMP packet too large for one segment; skipping")
            return jpeg_bytes
        segment = _APP1 + struct.pack(">H", seg_len) + payload

        # Sit after a leading JFIF APP0 if there is one, which is where readers
        # conventionally expect it.
        insert_at = 2
        for marker, start, end in _segments(data):
            if marker == 0xE0:
                insert_at = end
            break

        return data[:insert_at] + segment + data[insert_at:]
    except Exception as e:
        log.warning("[orbit-worker] XMP injection failed (%s: %s); "
                    "writing the panorama without it", type(e).__name__, e)
        return jpeg_bytes


def add_photosphere_metadata(jpeg_bytes, width, height, source_count=None,
                             heading_deg=None):
    """Convenience wrapper: build the GPano packet and inject it."""
    xmp = build_gpano_xmp(width, height, source_count=source_count,
                          heading_deg=heading_deg)
    return inject_xmp(jpeg_bytes, xmp)
