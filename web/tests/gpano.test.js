// GPano tagging of a downloaded panorama.
// Run with:  node web/tests/gpano.test.js
const G = require('../gpano.js');
let pass = 0, fail = 0;
function ok(name, cond, extra = '') { if (cond) pass++; else { fail++; console.log('  FAIL:', name, extra); } }

// A minimal but well-formed JPEG: SOI, JFIF APP0, SOF0 carrying the size, SOS,
// a byte of "scan data", EOI.
function jpeg(width, height, extraSegments = []) {
  const app0 = [0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 1, 1, 0, 0, 1, 0, 1, 0, 0];
  const sof = [0xff, 0xc0, 0x00, 0x11, 8, height >> 8, height & 0xff, width >> 8, width & 0xff,
               3, 1, 0x11, 0, 2, 0x11, 0, 3, 0x11, 0];
  const sos = [0xff, 0xda, 0x00, 0x08, 1, 1, 0, 0, 63, 0, 0x42, 0xff, 0xd9];
  return Uint8Array.from([0xff, 0xd8, ...app0, ...extraSegments, ...sof, ...sos]);
}
function app1(payload) {
  const body = Array.from(payload, (c) => c.charCodeAt(0));
  const len = body.length + 2;
  return [0xff, 0xe1, len >> 8, len & 0xff, ...body];
}
const text = (bytes) => Buffer.from(bytes).toString('latin1');

const tagged = G.tagBytes(jpeg(4096, 2048));
const s = text(tagged);
ok('the packet is written', s.indexOf('GPano:ProjectionType="equirectangular"') !== -1);
ok('the size comes from the frame header', /FullPanoWidthPixels="4096"/.test(s) && /FullPanoHeightPixels="2048"/.test(s));
ok('viewers are asked to treat it as a sphere', /UsePanoramaViewer="True"/.test(s));
ok('the file still starts and ends as a JPEG',
   tagged[0] === 0xff && tagged[1] === 0xd8 && tagged[tagged.length - 2] === 0xff && tagged[tagged.length - 1] === 0xd9);
ok('JFIF stays first', tagged[2] === 0xff && tagged[3] === 0xe0);

// Tagging twice must not leave two packets fighting over the same file.
const twice = text(G.tagBytes(G.tagBytes(jpeg(4096, 2048))));
ok('re-tagging replaces rather than appends', (twice.match(/GPano:ProjectionType/g) || []).length === 1);

// An unrelated APP1 (Exif) is none of our business.
const withExif = G.tagBytes(jpeg(4096, 2048, app1('Exif\0\0hello')));
ok('Exif survives', text(withExif).indexOf('hello') !== -1);

// Callers can override the size, which is what the 2:1 redraw does.
ok('an explicit size wins', /FullPanoWidthPixels="4092"/.test(text(G.tagBytes(jpeg(4093, 2046), { width: 4092, height: 2046 }))));

// Anything we do not understand must come back untouched, not mangled.
const notJpeg = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);
ok('a non-JPEG is returned as-is', text(G.tagBytes(notJpeg)) === text(notJpeg));

console.log(`\n  ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
