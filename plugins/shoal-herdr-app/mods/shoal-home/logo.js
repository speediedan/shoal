// logo.js: the SHOAL logo, with no GPUI here, so it runs under Node.
//
// The letters (glyphs.js) are original brush outlines with a liquid edge: rounded splash terminals, drips off
// the feet, a few droplets. All five stand on one baseline; only the drips run below it. Their full outlines are
// too many native path calls to build on a render, and they never change, so they are pre-rendered:
// preview/gen_logo_image.mjs draws `logoScene` to assets/logo.png, and the mod draws that image at `logoImageBox`,
// exactly on the letters. Until the image is known to be there, or when it is missing, `logoDraw` gives a cheap
// fallback instead: the body alone, from light outlines.

import { GLYPHS, CAP_EM, SLANT } from "./glyphs.js";

export const LOGO = {
  shadow: "#020a18",
  rim: "#062a4f",
  edge: "#0a3f86",
  top: "#b4f7ff",
  // past the logo's old deep blue (#2463ca): the gradient spans the drips too, so that blue lands on the baseline
  bottom: "#0b4fc8",
  gloss: "#eafdff",
};

const r2 = (v) => Math.round(v * 10) / 10;

// each glyph's ink extents, 1/1000 em
const extents = (g) => {
  const xs = g.ink.flatMap((l) => l.filter((_v, i) => i % 2 === 0));
  const ys = g.ink.flatMap((l) => l.filter((_v, i) => i % 2 === 1));
  return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
};
const FIT = 0.985; // the smoothed outline can bulge a little past its traced points

/** The letters of `word` inside box `lg` ({x, y, w, h, font}): each {ch, x, baseline, font, adv}, on one baseline, the
 * word's ink (splashes and drips too) centered in the box. A word too large for the box is drawn smaller, never
 * past it. */
export function logoLetters(lg, word = "SHOAL") {
  const chars = [...word];
  const { x0, x1, y0, y1 } = wordExtents(word);
  let pen = 0;
  const pens = chars.map((ch) => {
    const at = pen;
    pen += GLYPHS[ch].adv;
    return at;
  });
  const font = lg.font * Math.min(1, (FIT * lg.w * 1000) / (lg.font * (x1 - x0)), (FIT * lg.h * 1000) / (lg.font * (y1 - y0)));
  const baseline = lg.y + (lg.h - ((y1 - y0) * font) / 1000) / 2 - (y0 * font) / 1000;
  const left = lg.x + (lg.w - ((x1 - x0) * font) / 1000) / 2 - (x0 * font) / 1000;
  return chars.map((ch, i) => ({ ch, x: r2(left + (pens[i] * font) / 1000), baseline: r2(baseline), font: r2(font), adv: r2((GLYPHS[ch].adv * font) / 1000) }));
}

// the word's ink extents, 1/1000 em from its first letter's pen and the baseline
function wordExtents(word) {
  let pen = 0, x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const ch of word) {
    const e = extents(GLYPHS[ch]);
    x0 = Math.min(x0, pen + e.x0);
    x1 = Math.max(x1, pen + e.x1);
    y0 = Math.min(y0, e.y0);
    y1 = Math.max(y1, e.y1);
    pen += GLYPHS[ch].adv;
  }
  return { x0, x1, y0, y1 };
}

/** The pre-rendered logo: 240 px to the em is twice the largest logo a window asks for (scale 1.5 of 84 px, a little
 * less once the word is fitted), so it stays crisp on a 2x display; `padEm` of clear room around the ink holds the
 * rim and the soft shadow. */
export const LOGO_IMAGE = { file: "logo.png", pxPerEm: 240, padEm: 0.16 };

/** The image's frame in its own pixels: {pxPerEm, w, h, ox, oy}, (ox, oy) being where the first letter's pen meets the
 * baseline. Whole pixels, so the image's shape is exact. */
export function logoImageFrame() {
  const { pxPerEm: P, padEm: pad } = LOGO_IMAGE;
  const e = wordExtents("SHOAL");
  const ox = Math.ceil((pad - e.x0 / 1000) * P), oy = Math.ceil((pad - e.y0 / 1000) * P);
  return { pxPerEm: P, ox, oy, w: ox + Math.ceil((pad + e.x1 / 1000) * P), h: oy + Math.ceil((pad + e.y1 / 1000) * P) };
}

const cents = (v) => Math.round(v * 100) / 100;

/** Where assets/logo.png goes for box `lg`, so that its letters land exactly on `logoLetters(lg)`: {x, y, w, h}. It
 * can pass the box by its clear padding, never by its ink. */
export function logoImageBox(lg) {
  const f = logoImageFrame();
  const first = logoLetters(lg)[0];
  const s = first.font / f.pxPerEm;
  return { x: cents(first.x - f.ox * s), y: cents(first.baseline - f.oy * s), w: cents(f.w * s), h: cents(f.h * s) };
}

/** A point in letter `l`'s own brush units (x from its left, y 0 on the cap line, 1 on the baseline; the letters
 * lean, and so do these units), on the canvas. */
export const capPoint = (l, x, y) => ({
  x: r2(l.x + (x + SLANT * (1 - y)) * CAP_EM * l.font),
  y: r2(l.baseline + (y - 1) * CAP_EM * l.font),
});

/** A closed loop of points as a smooth outline: a Catmull-Rom spline, with each handle held to a third of its own
 * segment so that closely spaced points cannot make it overshoot and cross itself. This rounds every corner of
 * the traced outline, which is the liquid look. */
export function loopCmds(pts) {
  const n = pts.length;
  const at = (i) => pts[(i + n) % n];
  const handle = (a, b, c, side) => {
    // at b, between a and c: along a to c, toward `side` (c or a)
    const tx = c[0] - a[0], ty = c[1] - a[1];
    const tl = Math.hypot(tx, ty) || 1;
    const room = Math.hypot(side[0] - b[0], side[1] - b[1]) / 3;
    const k = Math.min(tl / 6, room) / tl;
    return side === c ? [b[0] + tx * k, b[1] + ty * k] : [b[0] - tx * k, b[1] - ty * k];
  };
  const cmds = [["M", r2(pts[0][0]), r2(pts[0][1])]];
  for (let i = 0; i < n; i++) {
    const [p0, p1, p2, p3] = [at(i - 1), at(i), at(i + 1), at(i + 2)];
    const c1 = handle(p0, p1, p2, p2), c2 = handle(p1, p2, p3, p1);
    cmds.push(["C", r2(c1[0]), r2(c1[1]), r2(c2[0]), r2(c2[1]), r2(p2[0]), r2(p2[1])]);
  }
  cmds.push(["Z"]);
  return cmds;
}

const placed = (flat, l) => {
  const pts = [];
  for (let i = 0; i < flat.length; i += 2) pts.push([l.x + (flat[i] * l.font) / 1000, l.baseline + (flat[i + 1] * l.font) / 1000]);
  return pts;
};

const shift = (cmds, dx, dy) => cmds.map((c) => {
  if (c[0] === "M") return ["M", r2(c[1] + dx), r2(c[2] + dy)];
  if (c[0] === "C") return ["C", r2(c[1] + dx), r2(c[2] + dy), r2(c[3] + dx), r2(c[4] + dy), r2(c[5] + dx), r2(c[6] + dy)];
  return c;
});

/** Points along a loop's smoothed outline (8 per segment), for bounds and tests. */
export function sampleLoop(cmds) {
  const out = [];
  let px = 0, py = 0;
  for (const c of cmds) {
    if (c[0] === "M") { px = c[1]; py = c[2]; out.push([px, py]); continue; }
    if (c[0] !== "C") continue;
    for (let i = 1; i <= 8; i++) {
      const t = i / 8, m = 1 - t;
      out.push([m * m * m * px + 3 * m * m * t * c[1] + 3 * m * t * t * c[3] + t * t * t * c[5],
                m * m * m * py + 3 * m * m * t * c[2] + 3 * m * t * t * c[4] + t * t * t * c[6]]);
    }
    px = c[5]; py = c[6];
  }
  return out;
}

function boundsOf(loops) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const cmds of loops) {
    for (const [x, y] of sampleLoop(cmds)) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  }
  return { x: Math.floor(x0), y: Math.floor(y0), w: Math.ceil(x1) - Math.floor(x0), h: Math.ceil(y1) - Math.floor(y0) };
}

// the body's element is sized to its ink, with its commands local to it, so the gradient spans the ink whether a
// renderer maps it to the path or to the element
const bodyItem = (ink, box) => ({ kind: "logo-body", cmds: shift(ink, -box.x, -box.y), fill: true, box, gradient: { angle: 180, from: LOGO.top, to: LOGO.bottom } });

/**
 * The logo's full vector geometry for box `lg`, which assets/logo.png is rendered from, and which tests and the vines'
 * weave are checked against. Too many path commands to draw on a render. Returns {letters, baseline, font, bounds, items}:
 *   letters  each {ch, x, baseline, font, adv, loops: [cmds, ...]}
 *   bounds   the ink's bounds {x, y, w, h} in canvas coordinates
 *   items    the draw order, back to front, each {kind, cmds, fill?, width?, color?, alpha?, gradient?, box?}.
 * The body has a `gradient` and a `box` (its ink bounds), with commands local to that box.
 */
export function logoScene(lg, word = "SHOAL") {
  const letters = logoLetters(lg, word).map((l) => ({ ...l, loops: GLYPHS[l.ch].ink.map((flat) => loopCmds(placed(flat, l))) }));
  const font = letters[0].font;
  const shown = letters.flatMap((l) => l.loops);
  const ink = shown.flat();
  const box = boundsOf(shown);
  const depth = Math.max(2, font / 22);
  const o = Math.max(1.2, font / 34);
  const items = [
    { kind: "logo-shadow", cmds: shift(ink, depth * 1.6, depth * 1.6), fill: true, color: LOGO.shadow, alpha: 0.28 },
    { kind: "logo-shadow", cmds: shift(ink, depth, depth), fill: true, color: LOGO.shadow, alpha: 0.6 },
    { kind: "logo-rim", cmds: ink, width: r2(o * 1.1), color: LOGO.rim, alpha: 1 },
    bodyItem(ink, box),
    { kind: "logo-edge", cmds: ink, width: r2(o * 1.0), color: LOGO.edge, alpha: 0.3 },
  ];
  const gloss = letters.flatMap((l) => GLYPHS[l.ch].gloss.map((flat) => loopCmds(placed(flat, l)))).flat();
  items.push({ kind: "logo-gloss", cmds: gloss, fill: true, color: LOGO.gloss, alpha: 0.5 });
  return { letters, baseline: letters[0].baseline, font, bounds: box, items };
}

/**
 * What a render draws for box `lg`. `art` true (assets/logo.png is known to be there): {image: {file, x, y, w, h}, items:
 * []}. Otherwise, as on the very first render, {image: null, items}: the body alone, from the light outlines.
 */
export function logoDraw(lg, art) {
  if (art) return { image: { file: LOGO_IMAGE.file, ...logoImageBox(lg) }, items: [] };
  const loops = logoLetters(lg).flatMap((l) => GLYPHS[l.ch].lite.map((flat) => loopCmds(placed(flat, l))));
  return { image: null, items: [bodyItem(loops.flat(), boundsOf(loops))] };
}
