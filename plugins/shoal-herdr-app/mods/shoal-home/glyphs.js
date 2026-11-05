// glyphs.js: the SHOAL logo's letter outlines, original geometry built here, no font. Bold, clean, rounded capitals,
// upright, the shape of small glass aquariums.
//
// Coordinates are cap units: x from the letter's left, y 0 on the cap line, 1 on the baseline. Nothing goes below the
// baseline. A letter's `loops` are closed outlines as display-list commands (M, L, C, Z). Loops never touch, and a
// counter winds against its outline, so either fill rule fills them the same.

/** Cap height, in em. */
export const CAP_EM = 0.86;
const GAP = 0.06; // between letters, in cap units

/** A closed polygon of [x, y, radius] corners, each rounded by one cubic. */
function roundedPoly(pts) {
  const n = pts.length;
  const corner = (i) => {
    const [x, y, r] = pts[i];
    const a = pts[(i + n - 1) % n], b = pts[(i + 1) % n];
    const la = Math.hypot(a[0] - x, a[1] - y), lb = Math.hypot(b[0] - x, b[1] - y);
    const ua = [(a[0] - x) / la, (a[1] - y) / la], ub = [(b[0] - x) / lb, (b[1] - y) / lb];
    const half = Math.acos(Math.max(-1, Math.min(1, ua[0] * ub[0] + ua[1] * ub[1]))) / 2;
    const t = Math.min(r / Math.tan(half), 0.48 * la, 0.48 * lb);
    return { from: [x + ua[0] * t, y + ua[1] * t], to: [x + ub[0] * t, y + ub[1] * t], at: [x, y] };
  };
  const cs = pts.map((_p, i) => corner(i));
  const K = 0.55; // how far each control runs toward the corner
  const cmds = [["M", ...cs[0].from]];
  cs.forEach((c, i) => {
    if (i) cmds.push(["L", ...c.from]);
    cmds.push(["C", c.from[0] + (c.at[0] - c.from[0]) * K, c.from[1] + (c.at[1] - c.from[1]) * K,
      c.to[0] + (c.at[0] - c.to[0]) * K, c.to[1] + (c.at[1] - c.to[1]) * K, c.to[0], c.to[1]]);
  });
  cmds.push(["Z"]);
  return cmds;
}

/** A closed rounded oval from (x0, y0) to (x1, y1); `k` above 0.55 is squarer than an ellipse. Clockwise on screen, or
 * counterclockwise for a counter. */
function oval(x0, y0, x1, y1, k, counter = false) {
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, rx = (x1 - x0) / 2, ry = (y1 - y0) / 2;
  const q = [[cx, y0], [x1, cy], [cx, y1], [x0, cy]];
  if (counter) q.reverse();
  const cmds = [["M", ...q[0]]];
  for (let i = 0; i < 4; i++) {
    const a = q[i], b = q[(i + 1) % 4];
    // from an end at the middle of an edge, the control runs along that edge
    const along = (p, other) => (p[1] === cy ? [p[0], p[1] + Math.sign(other[1] - p[1]) * ry * k] : [p[0] + Math.sign(other[0] - p[0]) * rx * k, p[1]]);
    cmds.push(["C", ...along(a, b), ...along(b, a), ...b]);
  }
  cmds.push(["Z"]);
  return cmds;
}

/** A closed loop of points as a smooth outline: a Catmull-Rom spline, each handle held to a third of its own segment so
 * that it cannot overshoot and cross itself. */
function smoothLoop(pts) {
  const n = pts.length;
  const at = (i) => pts[(i + n) % n];
  const handle = (a, b, c, side) => {
    const tx = c[0] - a[0], ty = c[1] - a[1];
    const tl = Math.hypot(tx, ty) || 1;
    const room = Math.hypot(side[0] - b[0], side[1] - b[1]) / 3;
    const k = Math.min(tl / 6, room) / tl;
    return side === c ? [b[0] + tx * k, b[1] + ty * k] : [b[0] - tx * k, b[1] - ty * k];
  };
  const cmds = [["M", ...pts[0]]];
  for (let i = 0; i < n; i++) {
    const [p0, p1, p2, p3] = [at(i - 1), at(i), at(i + 1), at(i + 2)];
    cmds.push(["C", ...handle(p0, p1, p2, p2), ...handle(p1, p2, p3, p1), ...p2]);
  }
  cmds.push(["Z"]);
  return cmds;
}

/** The outline of a constant-width stroke with round ends along a fine polyline, as a smooth loop of `n` points a side. */
function strokeLoop(fine, width, n) {
  const lens = [0];
  for (let i = 1; i < fine.length; i++) lens.push(lens[i - 1] + Math.hypot(fine[i][0] - fine[i - 1][0], fine[i][1] - fine[i - 1][1]));
  const at = (d) => {
    const i = Math.max(1, lens.findIndex((v) => v >= d - 1e-9));
    const u = (d - lens[i - 1]) / (lens[i] - lens[i - 1] || 1);
    const dx = fine[i][0] - fine[i - 1][0], dy = fine[i][1] - fine[i - 1][1];
    const l = Math.hypot(dx, dy) || 1;
    return { x: fine[i - 1][0] + dx * u, y: fine[i - 1][1] + dy * u, tx: dx / l, ty: dy / l };
  };
  const hw = width / 2;
  const total = lens.at(-1);
  const left = [], right = [];
  for (let i = 0; i <= n; i++) {
    const p = at((total * i) / n);
    left.push([p.x + p.ty * hw, p.y - p.tx * hw]);
    right.push([p.x - p.ty * hw, p.y + p.tx * hw]);
  }
  // a round end: three points around the half circle past the spine's end. The far end turns from the left side to the
  // right, past the end of the spine; the near end, `s` -1, from the right side back to the left, behind its start.
  const cap = (p, s) => [Math.PI / 4, Math.PI / 2, (3 * Math.PI) / 4].map((a) => [
    p.x + s * hw * (Math.cos(a) * p.ty + Math.sin(a) * p.tx),
    p.y + s * hw * (-Math.cos(a) * p.tx + Math.sin(a) * p.ty),
  ]);
  return smoothLoop([...left, ...cap(at(total), 1), ...right.reverse(), ...cap(at(0), -1)]);
}

/** The S's spine: an oval run from its upper right, over the top and round the left to its bottom, which is the middle
 * of the letter; the lower half is the same turned about that point. */
function sSpine(cx, rx, width, startDeg) {
  const ry = (0.5 - width / 2) / 2;
  const cy = 0.5 - ry;
  const upper = [];
  for (let i = 0; i <= 60; i++) {
    const a = ((startDeg + ((-270 - startDeg) * i) / 60) * Math.PI) / 180;
    upper.push([cx + rx * Math.cos(a), cy + ry * Math.sin(a)]);
  }
  return [...upper, ...upper.slice(0, -1).reverse().map(([x, y]) => [2 * cx - x, 1 - y])];
}

const R = 0.1, r = 0.035; // a corner of the glass, and an inner corner
const OUTLINES = {
  S: { width: 0.76, loops: [strokeLoop(sSpine(0.38, 0.25, 0.26, -32), 0.26, 16)] },
  H: { width: 0.94, loops: [roundedPoly([[0, 0, R], [0.3, 0, R], [0.3, 0.37, r], [0.64, 0.37, r], [0.64, 0, R], [0.94, 0, R], [0.94, 1, R], [0.64, 1, R], [0.64, 0.63, r], [0.3, 0.63, r], [0.3, 1, R], [0, 1, R]])] },
  O: { width: 1.0, loops: [oval(0, 0, 1.0, 1, 0.62), oval(0.29, 0.26, 0.71, 0.74, 0.6, true)] },
  A: { width: 1.04, loops: [
    roundedPoly([[0.37, 0, R], [0.67, 0, R], [1.04, 1, R], [0.72, 1, R], [0.665, 0.8, r], [0.375, 0.8, r], [0.32, 1, R], [0, 1, R]]),
    // the counter: counterclockwise on screen
    roundedPoly([[0.52, 0.24, r], [0.405, 0.6, r], [0.635, 0.6, r]]),
  ] },
  L: { width: 0.72, loops: [roundedPoly([[0, 0, R], [0.3, 0, R], [0.3, 0.7, r], [0.72, 0.7, R], [0.72, 1, R], [0, 1, R]])] },
};

/** Each letter: {adv, width, loops}. `adv` is in em, the others in cap units. */
export const GLYPHS = Object.fromEntries(Object.entries(OUTLINES).map(([ch, g]) => [ch, { ...g, adv: (g.width + GAP) * CAP_EM }]));
