// png.mjs: read and write 8-bit RGBA PNGs with Node alone, for the logo image's generator and its tests.

import { deflateSync, inflateSync } from "node:zlib";

const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const paeth = (a, b, c) => {
  const p = a + b - c;
  const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
};
// the predictor for filter type f, from the bytes left, up and up-left
const predict = (f, a, b, c) => (f === 1 ? a : f === 2 ? b : f === 3 ? (a + b) >> 1 : f === 4 ? paeth(a, b, c) : 0);

/** {width, height, rgba} of a non-interlaced 8-bit RGBA PNG in `buf`. */
export function readPng(buf) {
  if (!buf.subarray(0, 8).equals(SIGNATURE)) throw new Error("not a PNG");
  let width = 0, height = 0;
  const idat = [];
  for (let at = 8; at < buf.length;) {
    const len = buf.readUInt32BE(at);
    const type = buf.toString("latin1", at + 4, at + 8);
    const data = buf.subarray(at + 8, at + 8 + len);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      if (data[8] !== 8 || data[9] !== 6 || data[12] !== 0) throw new Error("only non-interlaced 8-bit RGBA");
    } else if (type === "IDAT") idat.push(data);
    at += 12 + len;
  }
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * 4;
  const rgba = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const f = raw[y * (stride + 1)];
    const line = y * (stride + 1) + 1;
    for (let i = 0; i < stride; i++) {
      const a = i >= 4 ? rgba[y * stride + i - 4] : 0;
      const b = y ? rgba[(y - 1) * stride + i] : 0;
      const c = y && i >= 4 ? rgba[(y - 1) * stride + i - 4] : 0;
      rgba[y * stride + i] = (raw[line + i] + predict(f, a, b, c)) & 255;
    }
  }
  return { width, height, rgba };
}

const CRC = Int32Array.from({ length: 256 }, (_v, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c;
});
const crc32 = (buf) => {
  let c = -1;
  for (const b of buf) c = CRC[(c ^ b) & 255] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
};

const chunk = (type, data) => {
  const body = Buffer.concat([Buffer.from(type, "latin1"), data]);
  const out = Buffer.alloc(body.length + 8);
  out.writeUInt32BE(data.length, 0);
  body.copy(out, 4);
  out.writeUInt32BE(crc32(body), body.length + 4);
  return out;
};

/** An RGBA image as a compact PNG: each row takes whichever filter leaves it smallest, nothing but pixels is kept. */
export function writePng({ width, height, rgba }) {
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  const row = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    let best = Infinity, bestF = 0;
    const line = y * (stride + 1);
    for (let f = 0; f <= 4; f++) {
      let score = 0;
      for (let i = 0; i < stride; i++) {
        const a = i >= 4 ? rgba[y * stride + i - 4] : 0;
        const b = y ? rgba[(y - 1) * stride + i] : 0;
        const c = y && i >= 4 ? rgba[(y - 1) * stride + i - 4] : 0;
        const v = (rgba[y * stride + i] - predict(f, a, b, c)) & 255;
        row[i] = v;
        score += v < 128 ? v : 256 - v;
      }
      if (score < best) { best = score; bestF = f; row.copy(raw, line + 1); }
    }
    raw[line] = bestF;
  }
  const head = Buffer.alloc(13);
  head.writeUInt32BE(width, 0);
  head.writeUInt32BE(height, 4);
  head[8] = 8;
  head[9] = 6;
  return Buffer.concat([SIGNATURE, chunk("IHDR", head), chunk("IDAT", deflateSync(raw, { level: 9 })), chunk("IEND", Buffer.alloc(0))]);
}
