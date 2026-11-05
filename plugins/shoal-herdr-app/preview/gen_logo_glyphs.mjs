// gen_logo_glyphs.mjs: draws the SHOAL logo's liquid letters and writes them to shoal-home/glyphs.js.
//
// The letters are original: each is a handful of brush strokes (a spline with a width at every key point),
// finger splashes on the terminals, drips off the feet and a few droplets. They are stamped into a mask,
// roughened with a little low-frequency noise and blurred, so where strokes meet they run together like water,
// then the half level is traced back into closed outlines and simplified. No font is read or used.
//
//   node plugins/shoal-herdr-app/preview/gen_logo_glyphs.mjs [--out path]

import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name, d) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : d; };
const OUT = opt("out", join(here, "..", "mods", "shoal-home", "glyphs.js"));

// Strokes are written in cap units: y 0 at the cap line, 1 on the baseline, x from the letter's left.
const CAP_EM = 0.84; // cap height in em
const SLANT = 0.17; // x shift per cap unit above the baseline: the brush's lean
const RES = 420; // mask pixels per em
const BLUR = 0.0085; // the goo: box-blur radius in em (three passes)
const NOISE = 0.2; // edge wobble, in mask level
const TOL = 0.0026; // outline simplification tolerance in em
const LITE_TOL = 0.011; // the same for the light outlines of the fallback logo
const LITE_MIN_AREA = 0.002; // em squared: smaller loops are droplets, which the light outlines leave out

// A stroke is [x, y, width] key points. Splashes end round, not pointed: water beads, it does not make horns.
const drip = (x, y, len, w = 0.05) => [[x, y - 0.1, w * 2.6], [x, y + len * 0.3, w * 1.05], [x + 0.004, y + len * 0.8, w * 0.8], [x + 0.006, y + len, w * 1.5]];
const dot = (x, y, r) => [[x, y, r * 2], [x + 0.001, y + 0.001, r * 2]];
// a wave crest on a stroke's top at (x, y): a long lobe curling over to the right, a short bead behind it
const crest = (x, y, s = 1, tall = 1) => [
  [[x, y + 0.08, 0.15], [x + 0.05 * s, y - 0.02 * tall, 0.115], [x + 0.13 * s, y - 0.085 * tall, 0.08], [x + 0.2 * s, y - 0.075 * tall, 0.058]],
  [[x - 0.01, y + 0.06, 0.12], [x - 0.07 * s, y - 0.015 * tall, 0.08], [x - 0.09 * s, y - 0.055 * tall, 0.06]],
];
// a bead pushed out of a stroke's side at (x, y), toward (dx, dy)
const bead = (x, y, dx, dy, w = 0.1) => [[x, y, w], [x + dx * 0.6, y + dy * 0.6, w * 0.7], [x + dx, y + dy, w * 0.72]];

const LETTERS = {
  S: {
    adv: 0.83,
    strokes: [
      [[0.68, 0.17, 0.12], [0.58, 0.1, 0.17], [0.4, 0.09, 0.2], [0.2, 0.15, 0.23], [0.13, 0.31, 0.25], [0.3, 0.47, 0.26],
        [0.53, 0.6, 0.26], [0.61, 0.76, 0.24], [0.44, 0.91, 0.19], [0.22, 0.91, 0.16], [0.07, 0.83, 0.1]],
      [[0.62, 0.15, 0.13], [0.7, 0.06, 0.1], [0.79, 0.0, 0.075], [0.86, 0.02, 0.055]],
      bead(0.68, 0.2, 0.08, 0.09, 0.09),
      [[0.13, 0.92, 0.11], [0.03, 0.95, 0.075], [-0.03, 0.9, 0.055]],
      bead(0.06, 0.26, -0.08, 0.05, 0.1),
      drip(0.42, 0.93, 0.22),
      dot(0.92, -0.05, 0.022),
      dot(0.85, 0.2, 0.016),
      dot(-0.07, 0.8, 0.018),
    ],
    gloss: [
      [[0.5, 0.05, 0.012], [0.33, 0.06, 0.03], [0.17, 0.12, 0.036], [0.09, 0.24, 0.012]],
      [[0.34, 0.4, 0.012], [0.48, 0.48, 0.03], [0.6, 0.58, 0.012]],
    ],
  },
  H: {
    adv: 1.02,
    strokes: [
      [[0.26, 0.05, 0.14], [0.25, 0.2, 0.24], [0.22, 0.6, 0.25], [0.2, 0.89, 0.22]],
      ...crest(0.25, 0.03, 0.85),
      [[0.79, 0.05, 0.14], [0.78, 0.2, 0.24], [0.76, 0.6, 0.25], [0.74, 0.89, 0.22]],
      ...crest(0.78, 0.03, 1, 1.25),
      [[0.22, 0.58, 0.11], [0.48, 0.52, 0.14], [0.78, 0.47, 0.11]],
      bead(0.12, 0.72, -0.08, 0.02, 0.1),
      bead(0.86, 0.3, 0.08, 0.03, 0.09),
      drip(0.74, 0.97, 0.25),
      drip(0.17, 0.97, 0.13, 0.042),
      dot(0.04, -0.03, 0.02),
      dot(0.48, -0.13, 0.016),
      dot(1.04, -0.13, 0.019),
    ],
    gloss: [
      [[0.22, 0.12, 0.012], [0.19, 0.3, 0.036], [0.17, 0.5, 0.012]],
      [[0.75, 0.12, 0.012], [0.72, 0.27, 0.036], [0.7, 0.42, 0.012]],
    ],
  },
  O: {
    adv: 0.97,
    strokes: [
      // the ring is drawn as one closed stroke, thick at the sides, thin over the top and bottom
      [[0.48, 0.07, 0.15], [0.28, 0.12, 0.2], [0.12, 0.3, 0.25], [0.09, 0.54, 0.27], [0.18, 0.77, 0.25], [0.38, 0.92, 0.18],
        [0.6, 0.9, 0.19], [0.76, 0.73, 0.25], [0.81, 0.48, 0.27], [0.74, 0.25, 0.23], [0.6, 0.1, 0.15], [0.48, 0.07, 0.15]],
      ...crest(0.62, 0.07, 0.95, 1.1),
      bead(0.0, 0.62, -0.08, 0.03, 0.1),
      bead(0.72, 0.84, 0.1, 0.06, 0.1),
      drip(0.31, 0.92, 0.23),
      dot(0.93, -0.08, 0.02),
      dot(0.42, -0.1, 0.016),
    ],
    gloss: [
      [[0.34, 0.07, 0.012], [0.17, 0.19, 0.034], [0.06, 0.38, 0.038], [0.04, 0.54, 0.012]],
      [[0.74, 0.23, 0.012], [0.78, 0.33, 0.028], [0.8, 0.44, 0.012]],
    ],
  },
  A: {
    adv: 1.0,
    strokes: [
      // the thin stroke up, the heavy stroke down, a low bar so the counter stays open
      [[0.48, 0.06, 0.12], [0.33, 0.42, 0.16], [0.17, 0.78, 0.18], [0.1, 0.91, 0.18]],
      [[0.47, 0.05, 0.14], [0.6, 0.4, 0.23], [0.74, 0.74, 0.25], [0.8, 0.88, 0.24]],
      [[0.19, 0.75, 0.09], [0.45, 0.71, 0.115], [0.72, 0.67, 0.09]],
      ...crest(0.48, 0.05, 1.05, 1.2),
      bead(0.02, 0.84, -0.08, 0.04, 0.09),
      bead(0.72, 0.36, 0.09, -0.01, 0.09),
      drip(0.82, 0.97, 0.23),
      drip(0.09, 0.97, 0.12, 0.042),
      dot(0.8, -0.1, 0.02),
      dot(0.25, -0.06, 0.016),
    ],
    gloss: [
      [[0.45, 0.13, 0.012], [0.34, 0.35, 0.026], [0.22, 0.62, 0.012]],
      [[0.53, 0.18, 0.012], [0.59, 0.34, 0.032], [0.65, 0.5, 0.012]],
    ],
  },
  L: {
    adv: 0.86,
    strokes: [
      [[0.26, 0.05, 0.14], [0.24, 0.2, 0.24], [0.21, 0.6, 0.25], [0.19, 0.88, 0.23]],
      ...crest(0.25, 0.03, 0.95, 1.15),
      [[0.12, 0.905, 0.19], [0.34, 0.915, 0.165], [0.56, 0.9, 0.15], [0.7, 0.81, 0.12], [0.77, 0.7, 0.08], [0.8, 0.66, 0.06]],
      bead(0.68, 0.88, 0.14, -0.01, 0.09),
      bead(0.1, 0.48, -0.08, 0.02, 0.09),
      drip(0.46, 0.96, 0.25),
      drip(0.16, 0.97, 0.13, 0.042),
      dot(0.03, 0.02, 0.018),
      dot(0.86, 0.57, 0.02),
      dot(0.49, -0.08, 0.016),
    ],
    gloss: [
      [[0.22, 0.12, 0.012], [0.18, 0.34, 0.036], [0.16, 0.6, 0.012]],
    ],
  },
};

// --- the mask

function catmull(pts, samples) {
  const out = [];
  const closed = pts.length > 3 && pts[0][0] === pts.at(-1)[0] && pts[0][1] === pts.at(-1)[1];
  const n = pts.length;
  const at = (i) => (closed ? pts[((i % (n - 1)) + (n - 1)) % (n - 1)] : pts[Math.max(0, Math.min(n - 1, i))]);
  for (let i = 0; i < n - 1; i++) {
    const [p0, p1, p2, p3] = [at(i - 1), at(i), at(i + 1), at(i + 2)];
    for (let s = 0; s < samples; s++) {
      const t = s / samples, t2 = t * t, t3 = t2 * t;
      const ch = (k) => 0.5 * (2 * p1[k] + (p2[k] - p0[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2 + (3 * p1[k] - p0[k] - 3 * p2[k] + p3[k]) * t3);
      out.push([ch(0), ch(1), Math.max(0.001, p1[2] + (p2[2] - p1[2]) * t)]);
    }
  }
  out.push(pts.at(-1));
  return out;
}

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Smooth value noise on a coarse grid, -1..1. */
function noiseField(w, h, cell, rand) {
  const cols = Math.ceil(w / cell) + 2, rows = Math.ceil(h / cell) + 2;
  const grid = Float32Array.from({ length: cols * rows }, () => rand() * 2 - 1);
  const f = new Float32Array(w * h);
  const sm = (t) => t * t * (3 - 2 * t);
  for (let y = 0; y < h; y++) {
    const gy = y / cell, iy = Math.floor(gy), ty = sm(gy - iy);
    for (let x = 0; x < w; x++) {
      const gx = x / cell, ix = Math.floor(gx), tx = sm(gx - ix);
      const a = grid[iy * cols + ix], b = grid[iy * cols + ix + 1], c = grid[(iy + 1) * cols + ix], d = grid[(iy + 1) * cols + ix + 1];
      f[y * w + x] = (a + (b - a) * tx) * (1 - ty) + (c + (d - c) * tx) * ty;
    }
  }
  return f;
}

function boxBlur(src, w, h, r) {
  const pass = (from, to, step, lines, len) => {
    for (let l = 0; l < lines; l++) {
      const base = step === 1 ? l * w : l;
      let sum = 0;
      const get = (i) => from[base + Math.max(0, Math.min(len - 1, i)) * step];
      for (let i = -r; i <= r; i++) sum += get(i);
      for (let i = 0; i < len; i++) {
        to[base + i * step] = sum / (2 * r + 1);
        sum += get(i + r + 1) - get(i - r);
      }
    }
  };
  let a = src, b = new Float32Array(w * h);
  for (let p = 0; p < 3; p++) {
    pass(a, b, 1, h, w);
    [a, b] = [b, a];
    pass(a, b, w, w, h);
    [a, b] = [b, a];
  }
  return a;
}

/** The outlines of `f` at level 0.5: closed loops in mask pixels, ink on one consistent side. */
function trace(f, w, h) {
  const segs = new Map();
  const key = (cx, cy, e) => `${cx},${cy},${e}`; // an edge: e 0 is the top of cell (cx, cy), e 1 its left
  const inter = (a, b) => (0.5 - a) / (b - a);
  for (let y = 0; y < h - 1; y++) {
    for (let x = 0; x < w - 1; x++) {
      const tl = f[y * w + x], tr = f[y * w + x + 1], bl = f[(y + 1) * w + x], br = f[(y + 1) * w + x + 1];
      const c = (tl > 0.5 ? 8 : 0) | (tr > 0.5 ? 4 : 0) | (br > 0.5 ? 2 : 0) | (bl > 0.5 ? 1 : 0);
      if (c === 0 || c === 15) continue;
      const T = { k: key(x, y, 0), p: () => [x + inter(tl, tr), y] };
      const B = { k: key(x, y + 1, 0), p: () => [x + inter(bl, br), y + 1] };
      const Le = { k: key(x, y, 1), p: () => [x, y + inter(tl, bl)] };
      const R = { k: key(x + 1, y, 1), p: () => [x + 1, y + inter(tr, br)] };
      const add = (a, b) => segs.set(a.k, { to: b.k, p: a.p() });
      // each segment runs with the ink on its right (on screen, y down)
      switch (c) {
        case 1: add(Le, B); break;
        case 2: add(B, R); break;
        case 3: add(Le, R); break;
        case 4: add(R, T); break;
        case 5: add(Le, T); add(R, B); break;
        case 6: add(B, T); break;
        case 7: add(Le, T); break;
        case 8: add(T, Le); break;
        case 9: add(T, B); break;
        case 10: add(T, R); add(B, Le); break;
        case 11: add(T, R); break;
        case 12: add(R, Le); break;
        case 13: add(R, B); break;
        case 14: add(B, Le); break;
        default: break;
      }
    }
  }
  const loops = [];
  while (segs.size) {
    const start = segs.keys().next().value;
    const loop = [];
    let k = start;
    while (segs.has(k)) {
      const s = segs.get(k);
      segs.delete(k);
      loop.push(s.p);
      k = s.to;
    }
    if (loop.length > 8) loops.push(loop);
  }
  return loops;
}

const area = (pts) => {
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length];
    a += x0 * y1 - x1 * y0;
  }
  return a / 2;
};

function dpOpen(pts, tol) {
  const keep = new Uint8Array(pts.length);
  keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    const [ax, ay] = pts[a], [bx, by] = pts[b];
    const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1;
    let best = -1, bi = -1;
    for (let i = a + 1; i < b; i++) {
      const d = Math.abs((pts[i][0] - ax) * dy - (pts[i][1] - ay) * dx) / len;
      if (d > best) { best = d; bi = i; }
    }
    if (best > tol) { keep[bi] = 1; stack.push([a, bi], [bi, b]); }
  }
  return pts.filter((_p, i) => keep[i]);
}

/** Douglas-Peucker on a closed loop, split at its two farthest-apart points. */
function simplify(loop, tol) {
  let far = 0, fd = -1;
  for (let i = 0; i < loop.length; i++) {
    const d = Math.hypot(loop[i][0] - loop[0][0], loop[i][1] - loop[0][1]);
    if (d > fd) { fd = d; far = i; }
  }
  const a = dpOpen(loop.slice(0, far + 1), tol);
  const b = dpOpen([...loop.slice(far), loop[0]], tol);
  return [...a.slice(0, -1), ...b.slice(0, -1)];
}

function drawGlyph(ch, spec, seed) {
  const margin = 0.14; // em
  const x0 = -0.2, y0 = -0.3; // cap-unit origin of the mask, in cap units
  const wEm = (1.2 + 0.4) * CAP_EM + 2 * margin, hEm = 1.65 * CAP_EM + 2 * margin;
  const w = Math.ceil(wEm * RES), h = Math.ceil(hEm * RES);
  // cap units to mask pixels (the slant leans everything right as it rises)
  const toX = (x, y) => (margin + (x - x0 + SLANT * (1 - y)) * CAP_EM) * RES;
  const toY = (y) => (margin + (y - y0) * CAP_EM) * RES;
  const stamp = (mask, strokes) => {
    for (const st of strokes) {
      for (const [x, y, wd] of catmull(st, 40)) {
        const cx = toX(x, y), cy = toY(y), r = (wd * CAP_EM * RES) / 2;
        const ix0 = Math.max(0, Math.floor(cx - r)), ix1 = Math.min(w - 1, Math.ceil(cx + r));
        const iy0 = Math.max(0, Math.floor(cy - r)), iy1 = Math.min(h - 1, Math.ceil(cy + r));
        for (let iy = iy0; iy <= iy1; iy++) {
          for (let ix = ix0; ix <= ix1; ix++) {
            if ((ix - cx) ** 2 + (iy - cy) ** 2 <= r * r) mask[iy * w + ix] = 1;
          }
        }
      }
    }
  };
  const fromPx = ([px, py]) => {
    // back to em, y 0 on the baseline (negative up), x 0 at the letter's unslanted left
    const x = px / RES - margin + x0 * CAP_EM;
    const y = py / RES - margin + y0 * CAP_EM - CAP_EM;
    return [Math.round(x * 1000), Math.round(y * 1000)];
  };
  const toLoops = (f, tol, minArea) => trace(f, w, h)
    // a small loop keeps enough points to stay open: the A's counter must survive the light tolerance
    .map((l) => { const a = simplify(l, tol * RES); return a.length >= 6 ? a : simplify(l, (tol * RES) / 4); })
    .filter((l) => l.length >= 4 && Math.abs(area(l)) > minArea * RES * RES)
    .map((l) => l.map(fromPx).flat());

  const mask = new Float32Array(w * h);
  stamp(mask, spec.strokes);
  const noise = noiseField(w, h, Math.round(0.07 * RES), rng(seed));
  for (let i = 0; i < mask.length; i++) mask[i] = Math.min(1, Math.max(0, mask[i] + NOISE * noise[i] * (mask[i] > 0 ? -1 : 1)));
  const f = boxBlur(mask, w, h, Math.max(1, Math.round(BLUR * RES)));
  const ink = toLoops(f, TOL, 0.00006);
  const lite = toLoops(f, LITE_TOL, LITE_MIN_AREA);

  const glossMask = new Float32Array(w * h);
  stamp(glossMask, spec.gloss ?? []);
  const gloss = toLoops(boxBlur(glossMask, w, h, 2), TOL * 1.3, 0.00004);
  return { adv: Math.round(spec.adv * CAP_EM * 1000), ink, lite, gloss };
}

const glyphs = {};
let seed = 7;
for (const [ch, spec] of Object.entries(LETTERS)) glyphs[ch] = drawGlyph(ch, spec, seed++ * 1009);

const count = Object.values(glyphs).map((g) => g.ink.reduce((a, l) => a + l.length / 2, 0));
const lines = [
  "// glyphs.js: the SHOAL logo's letters, written by preview/gen_logo_glyphs.mjs (edit the strokes there and run it).",
  "// Original brush outlines, drawn for this mod. Units: 1/1000 em, y 0 on the baseline and negative up. `ink`, `lite`",
  "// and `gloss` are lists of closed loops [x0, y0, x1, y1, ...], smoothed when drawn (logo.js). `ink` is the full",
  "// outline that assets/logo.png is rendered from; `lite` is far fewer points and no droplets, for the fallback logo.",
  "// Loops never touch, and a counter winds opposite to its outline, so either fill rule fills them the same.",
  "",
  `export const CAP_EM = ${CAP_EM};`,
  `export const SLANT = ${SLANT};`,
  `export const GLYPHS = {`,
];
for (const [ch, g] of Object.entries(glyphs)) {
  lines.push(`  ${ch}: {`, `    adv: ${g.adv},`, "    ink: [");
  for (const l of g.ink) lines.push(`      [${l.join(",")}],`);
  lines.push("    ],", "    lite: [");
  for (const l of g.lite) lines.push(`      [${l.join(",")}],`);
  lines.push("    ],", "    gloss: [");
  for (const l of g.gloss) lines.push(`      [${l.join(",")}],`);
  lines.push("    ],", "  },");
}
lines.push("};", "");
writeFileSync(OUT, lines.join("\n"));
console.log(`${OUT}: ${count.join(" ")} points`);
