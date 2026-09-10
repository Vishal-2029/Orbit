// Make a downloaded panorama open as a 360, not as a stretched photo.
//
// The file we serve is a plain equirectangular JPEG. Nothing in it says so, and
// every consumer of 360s - Google Photos, Facebook, a headset's gallery, most
// desktop viewers - decides by reading Google's GPano XMP block. Without it the
// picture is shown flat, which is exactly the "wrong view" you get after
// pressing download. So before saving we splice that block into the JPEG.
//
// This is done in the browser rather than on the server because the panorama is
// static and may be cached or served straight from object storage; tagging the
// bytes on their way to disk needs no re-upload and no second copy of the file.
const GPano = (() => {
  "use strict";

  const XMP_NS = "http://ns.adobe.com/xap/1.0/\0";
  const APP1 = 0xe1;

  // Frame markers carry the image size in bytes 5..8 of their payload. The
  // three excluded ones (DHT, JPG, DAC) share the 0xC0-0xCF range but are not
  // frames.
  function isSOF(m) {
    return m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc;
  }

  function ascii(bytes, at, len) {
    let s = "";
    for (let i = 0; i < len; i++) s += String.fromCharCode(bytes[at + i]);
    return s;
  }

  // Walk the segment chain once: find the size, the place to insert, and any
  // XMP block already there (a second one would be ambiguous, so it goes).
  function scan(bytes) {
    const out = { width: 0, height: 0, insertAt: 2, drop: [] };
    let p = 2;
    let sawInsert = false;
    while (p + 3 < bytes.length && bytes[p] === 0xff) {
      const marker = bytes[p + 1];
      if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) { p += 2; continue; }
      if (marker === 0xda) break;                       // scan data: segments are over
      const len = (bytes[p + 2] << 8) | bytes[p + 3];
      if (len < 2) break;
      const end = p + 2 + len;
      if (marker === APP1 && ascii(bytes, p + 4, XMP_NS.length) === XMP_NS) {
        out.drop.push([p, end]);
      } else if (!sawInsert && !(marker === 0xe0 || marker === APP1)) {
        // Keep JFIF/Exif first, as readers expect, and land right after them.
        out.insertAt = p;
        sawInsert = true;
      }
      if (isSOF(marker) && p + 9 < bytes.length) {
        out.height = (bytes[p + 5] << 8) | bytes[p + 6];
        out.width = (bytes[p + 7] << 8) | bytes[p + 8];
      }
      p = end;
    }
    if (!sawInsert) out.insertAt = p;
    return out;
  }

  function packet(width, height, heading) {
    return '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>' +
      '<x:xmpmeta xmlns:x="adobe:ns:meta/">' +
      '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">' +
      '<rdf:Description rdf:about="" xmlns:GPano="http://ns.google.com/photos/1.0/panorama/" ' +
      'GPano:ProjectionType="equirectangular" ' +
      'GPano:UsePanoramaViewer="True" ' +
      'GPano:CroppedAreaImageWidthPixels="' + width + '" ' +
      'GPano:CroppedAreaImageHeightPixels="' + height + '" ' +
      'GPano:FullPanoWidthPixels="' + width + '" ' +
      'GPano:FullPanoHeightPixels="' + height + '" ' +
      'GPano:CroppedAreaLeftPixels="0" ' +
      'GPano:CroppedAreaTopPixels="0" ' +
      'GPano:InitialViewHeadingDegrees="' + Math.round(heading || 0) + '" ' +
      'GPano:InitialViewPitchDegrees="0" ' +
      'GPano:InitialViewRollDegrees="0" ' +
      'GPano:InitialHorizontalFOVDegrees="90"/>' +
      '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>';
  }

  function utf8(str) {
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(str);
    return Uint8Array.from(unescape(encodeURIComponent(str)), (c) => c.charCodeAt(0));
  }

  // Returns tagged bytes, or the input untouched if this is not a JPEG we
  // understand - a download that is merely untagged beats a download that is
  // corrupt.
  function tagBytes(bytes, opts) {
    opts = opts || {};
    if (!(bytes instanceof Uint8Array)) bytes = new Uint8Array(bytes);
    if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return bytes;

    const found = scan(bytes);
    const width = opts.width || found.width;
    const height = opts.height || found.height;
    if (!width || !height) return bytes;

    const body = utf8(XMP_NS + packet(width, height, opts.heading));
    const segLen = body.length + 2;
    if (segLen > 0xffff) return bytes;                  // will not fit in one APP1

    const keep = [];                                    // [start, end) runs to copy
    let cursor = 0;
    for (const [from, to] of found.drop) {
      keep.push([cursor, from]);
      cursor = to;
    }
    keep.push([cursor, bytes.length]);

    // insertAt is an offset into the original file; find which kept run holds it.
    const seg = new Uint8Array(4 + body.length);
    seg[0] = 0xff; seg[1] = APP1; seg[2] = segLen >> 8; seg[3] = segLen & 0xff;
    seg.set(body, 4);

    const parts = [];
    let inserted = false;
    for (const [from, to] of keep) {
      if (from >= to && from !== found.insertAt) continue;
      if (!inserted && found.insertAt >= from && found.insertAt <= to) {
        parts.push(bytes.subarray(from, found.insertAt), seg, bytes.subarray(found.insertAt, to));
        inserted = true;
      } else {
        parts.push(bytes.subarray(from, to));
      }
    }
    if (!inserted) parts.push(seg);

    let size = 0;
    for (const part of parts) size += part.length;
    const out = new Uint8Array(size);
    let at = 0;
    for (const part of parts) { out.set(part, at); at += part.length; }
    return out;
  }

  // Blob in, blob out. Anything that is not a JPEG comes back as it went in.
  async function tagBlob(blob, opts) {
    if (!/jpe?g/i.test(blob.type || "") && blob.type) return blob;
    const bytes = tagBytes(new Uint8Array(await blob.arrayBuffer()), opts);
    return new Blob([bytes], { type: "image/jpeg" });
  }

  function hasGPano(bytes) {
    const window = 65536 * 4;                            // XMP lives near the front
    return ascii(bytes, 0, Math.min(bytes.length, window))
      .indexOf("ns.google.com/photos/1.0/panorama") !== -1;
  }

  function decode(blob) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(blob);
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("could not decode the panorama")); };
      img.src = url;
    });
  }

  // What the download button should hand to the browser.
  //
  // Two things can be wrong with the stored file. It may predate the stitcher
  // writing GPano at all, and - the case that bit here - it may be a hair off
  // 2:1: 4093x2046 is 2.0005:1, and a strict viewer either refuses that or
  // wraps it onto the sphere slightly skewed. Redrawing to an exact 2:1 canvas
  // costs one re-encode and makes the file valid everywhere.
  //
  // A file that is already square-on and already tagged is passed through
  // untouched, which keeps the stitcher's own metadata (source count, heading)
  // and avoids a pointless generation loss.
  async function prepareDownload(blob, opts) {
    opts = opts || {};
    if (!/jpe?g/i.test(blob.type || "") && blob.type) return blob;
    let bytes;
    try {
      bytes = new Uint8Array(await blob.arrayBuffer());
    } catch (e) {
      return blob;
    }
    const found = scan(bytes);
    if (found.width && found.height === Math.round(found.width / 2) &&
        found.width % 2 === 0 && hasGPano(bytes)) {
      return blob;
    }
    if (!found.width || found.height !== Math.round(found.width / 2) ||
        found.width % 2) {
      try {
        const img = await decode(blob);
        const w = (img.naturalWidth || img.width) & ~1;   // even, so w/2 is whole
        const h = w / 2;
        if (w >= 2) {
          const canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext("2d");
          ctx.imageSmoothingQuality = "high";
          ctx.drawImage(img, 0, 0, w, h);
          const redrawn = await new Promise((res) =>
            canvas.toBlob(res, "image/jpeg", opts.quality || 0.92));
          if (redrawn) {
            return tagBlob(redrawn, { width: w, height: h, heading: opts.heading });
          }
        }
      } catch (e) {
        // Fall through: a tagged original still beats no download at all.
      }
    }
    return tagBlob(blob, opts);
  }

  return { tagBytes, tagBlob, prepareDownload };
})();

if (typeof module !== "undefined" && module.exports) module.exports = GPano;
