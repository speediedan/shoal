// logo.js: the SHOAL logo, with no GPUI here, so it runs under Node.
//
// Bold, clean, rounded capitals made of glass, each a small aquarium: water in a light blue to blue gradient, a reef
// inside, a bright waterline and specular highlights. The letters (glyphs.js) all stand on one baseline, and nothing
// goes below it. The glass and reef are far too much to draw on a render and they never change, so they are
// pre-rendered: preview/gen_logo_image.mjs draws them to assets/logo.png, and the mod draws that image at
// `logoImageBox`, exactly on the letters. Until the image is known to be there, or when it is missing, `logoDraw` gives
// a cheap fallback instead: the plain outlines, filled with the same gradient, with a light rim.

import { GLYPHS, CAP_EM } from "./glyphs.js";

export const LOGO = {
  top: "#b4f7ff",
  bottom: "#1f62cf",
  rim: "#dff9ff",
};

const r2 = (v) => Math.round(v * 10) / 10;

// a loop's points, end points and controls together: the cap-unit corners of what it can reach
const coords = (cmds) => cmds.filter((c) => c[0] !== "Z").map((c) => c.slice(1));
function loopExtents(cmds) {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const vals of coords(cmds)) {
    for (let i = 0; i < vals.length; i += 2) {
      x0 = Math.min(x0, vals[i]); x1 = Math.max(x1, vals[i]);
      y0 = Math.min(y0, vals[i + 1]); y1 = Math.max(y1, vals[i + 1]);
    }
  }
  return { x0, x1, y0, y1 };
}

// the word's ink extents, in em from its first letter's pen and the baseline (negative up)
function wordExtents(word) {
  let pen = 0, x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const ch of word) {
    for (const cmds of GLYPHS[ch].loops) {
      const e = loopExtents(cmds);
      x0 = Math.min(x0, pen + e.x0 * CAP_EM);
      x1 = Math.max(x1, pen + e.x1 * CAP_EM);
      y0 = Math.min(y0, (e.y0 - 1) * CAP_EM);
      y1 = Math.max(y1, (e.y1 - 1) * CAP_EM);
    }
    pen += GLYPHS[ch].adv;
  }
  return { x0, x1, y0, y1 };
}

const FIT = 0.985; // room for the rim inside the box

/** The letters of `word` inside box `lg` ({x, y, w, h, font}): each {ch, x, baseline, font, adv}, on one baseline, the
 * word centered in the box. A word too large for the box is drawn smaller, never past it. */
export function logoLetters(lg, word = "SHOAL") {
  const chars = [...word];
  const { x0, x1, y0, y1 } = wordExtents(word);
  const font = lg.font * Math.min(1, (FIT * lg.w) / (lg.font * (x1 - x0)), (FIT * lg.h) / (lg.font * (y1 - y0)));
  const baseline = lg.y + (lg.h - (y1 - y0) * font) / 2 - y0 * font;
  let pen = lg.x + (lg.w - (x1 - x0) * font) / 2 - x0 * font;
  return chars.map((ch) => {
    const out = { ch, x: r2(pen), baseline: r2(baseline), font: r2(font), adv: r2(GLYPHS[ch].adv * font) };
    pen += GLYPHS[ch].adv * font;
    return out;
  });
}

/** The pre-rendered logo: 244 px to the em is twice the largest logo a window asks for, so it stays crisp on a 2x
 * display; `padEm` of clear room around the ink holds the rim and the soft shadow. */
export const LOGO_IMAGE = { file: "logo.png", pxPerEm: 244, padEm: 0.16 };

/** The image's frame in its own pixels: {pxPerEm, w, h, ox, oy}, (ox, oy) being where the first letter's pen meets the
 * baseline. Whole pixels, so the image's shape is exact. */
export function logoImageFrame() {
  const { pxPerEm: P, padEm: pad } = LOGO_IMAGE;
  const e = wordExtents("SHOAL");
  const ox = Math.ceil((pad - e.x0) * P), oy = Math.ceil((pad - e.y0) * P);
  return { pxPerEm: P, ox, oy, w: ox + Math.ceil((pad + e.x1) * P), h: oy + Math.ceil((pad + e.y1) * P) };
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

/** A point in letter `l`'s own cap units (x from its left, y 0 on the cap line, 1 on the baseline), on the canvas. */
export const capPoint = (l, x, y) => ({
  x: r2(l.x + x * CAP_EM * l.font),
  y: r2(l.baseline + (y - 1) * CAP_EM * l.font),
});

/** A glyph's cap-unit commands placed for letter `l`, on the canvas. */
function placed(cmds, l) {
  const k = CAP_EM * l.font;
  return cmds.map((c) => {
    if (c[0] === "Z") return c;
    const out = [c[0]];
    for (let i = 1; i < c.length; i += 2) out.push(r2(l.x + c[i] * k), r2(l.baseline + (c[i + 1] - 1) * k));
    return out;
  });
}

const shift = (cmds, dx, dy) => cmds.map((c) => {
  if (c[0] === "Z") return c;
  const out = [c[0]];
  for (let i = 1; i < c.length; i += 2) out.push(r2(c[i] + dx), r2(c[i + 1] + dy));
  return out;
});

/** Points along a loop's outline (8 per curve, the ends of a line), for bounds and tests. */
export function sampleLoop(cmds) {
  const out = [];
  let px = 0, py = 0;
  for (const c of cmds) {
    if (c[0] === "M" || c[0] === "L") { px = c[1]; py = c[2]; out.push([px, py]); continue; }
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

/** The letters' geometry for box `lg`, which the image lines up with and the vines' weave is checked against:
 * {letters, baseline, font, bounds}, `letters` each {ch, x, baseline, font, adv, loops: [cmds, ...]} on the canvas,
 * `bounds` the ink's {x, y, w, h}. */
export function logoScene(lg, word = "SHOAL") {
  const letters = logoLetters(lg, word).map((l) => ({ ...l, loops: GLYPHS[l.ch].loops.map((cmds) => placed(cmds, l)) }));
  return { letters, baseline: letters[0].baseline, font: letters[0].font, bounds: boundsOf(letters.flatMap((l) => l.loops)) };
}

/**
 * What a render draws for box `lg`. `art` true (assets/logo.png is known to be there): {image: {file, x, y, w, h}, items:
 * []}. Otherwise, as on the very first render, {image: null, items}: the outlines filled with the gradient, and a light
 * rim. The body's element is sized to its ink, with its commands local to it, so the gradient spans the ink whether a
 * renderer maps it to the path or to the element.
 */
export function logoDraw(lg, art) {
  if (art) return { image: { file: LOGO_IMAGE.file, ...logoImageBox(lg) }, items: [] };
  const sc = logoScene(lg);
  const ink = sc.letters.flatMap((l) => l.loops).flat();
  const box = sc.bounds;
  return { image: null, items: [
    { kind: "logo-body", cmds: shift(ink, -box.x, -box.y), fill: true, box, gradient: { angle: 180, from: LOGO.top, to: LOGO.bottom } },
    { kind: "logo-rim", cmds: ink, width: r2(Math.max(1, sc.font / 40)), color: LOGO.rim, alpha: 0.85 },
  ] };
}
