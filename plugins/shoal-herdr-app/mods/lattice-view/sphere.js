// Smooth, directional globe shading using paths supported by both GPUI and the SVG preview.
// A light contour is a projected small circle, clipped by the visible hemisphere.
// Short cubic arcs keep the smooth lighting without thousands of native line calls per globe.
//
// A globe is glass: the nested contours each add a little alpha, so the shadow side is nearly clear and the lit side a
// tinted veil, with the floor, stems and the globes behind showing through. Opacity is the only thing hover changes.
// The per-shoal view's cutout sphere (a bowl opened toward you) reuses the same light, seen from inside.
import { mix, withAlpha } from "./geom.js";

export const LIGHT = { x: -0.46, y: -0.54, z: Math.sqrt(1 - 0.46 ** 2 - 0.54 ** 2) };
// Layers stack, so each adds 1 to 6 of the 255 alpha steps: 48 of them cover the range in steps finer than the old
// 128 opaque levels did, at under half their path commands.
export const STEPS = 48;
export const lightAt = (x, y) => LIGHT.x * x + LIGHT.y * y + LIGHT.z * Math.sqrt(Math.max(0, 1 - x * x - y * y));

/** How opaque a globe is, on its shadow side (`body`) and at its lit pole (`lit`): much less with nothing on it in
 * focus, a little more when the pointer is on it or on one of its roles. */
export const GLASS = { idle: { body: 0.07, lit: 0.34 }, focus: { body: 0.16, lit: 0.56 } };
export const glassOf = (focused) => GLASS[focused ? "focus" : "idle"];

function arc(cmds, point, tangent, start, end) {
  const count = Math.ceil(Math.abs(end - start) / (Math.PI / 2)); // either way round
  for (let i = 0; i < count; i++) {
    const a = start + (end - start) * i / count, b = start + (end - start) * (i + 1) / count;
    const p = point(a), q = point(b), u = tangent(a), v = tangent(b);
    const k = 4 / 3 * Math.tan((b - a) / 4);
    cmds.push(["C", p.x + k * u.x, p.y + k * u.y, q.x - k * v.x, q.y - k * v.y, q.x, q.y]);
  }
}

function contour(level) {
  const side = Math.hypot(LIGHT.x, LIGHT.y), radius = Math.sqrt(1 - level * level);
  const u = { x: -LIGHT.y / side, y: LIGHT.x / side };
  const v = { x: -LIGHT.z * LIGHT.x / side, y: -LIGHT.z * LIGHT.y / side };
  const point = (a) => ({ x: level * LIGHT.x + radius * (u.x * Math.cos(a) + v.x * Math.sin(a)),
    y: level * LIGHT.y + radius * (u.y * Math.cos(a) + v.y * Math.sin(a)) });
  const tangent = (a) => ({ x: radius * (-u.x * Math.sin(a) + v.x * Math.cos(a)),
    y: radius * (-u.y * Math.sin(a) + v.y * Math.cos(a)) });
  const clipped = level < side;
  const start = clipped ? -Math.asin(level * LIGHT.z / (radius * side)) : -Math.PI / 2;
  const end = clipped ? Math.PI - start : start + 2 * Math.PI;
  const p = point(start), q = point(end), cmds = [["M", p.x, p.y]];
  arc(cmds, point, tangent, start, end);
  if (clipped) {
    const a = Math.atan2(q.y, q.x), b = Math.atan2(p.y, p.x);
    arc(cmds, (t) => ({ x: Math.cos(t), y: Math.sin(t) }), (t) => ({ x: -Math.sin(t), y: Math.cos(t) }),
      a, a + (b - a + 2 * Math.PI) % (2 * Math.PI));
  }
  cmds.push(["Z"]);
  return cmds;
}

const CONTOURS = Array.from({ length: STEPS }, (_, i) => contour(i / STEPS));

/** The body's color and each contour's, alpha included: layer i lifts the total opacity toward `lit` on a curve
 * that saves most of the rise for the lit cap, so the shadow side stays clear. */
function glassLayers(tint, fog, focused) {
  const g = glassOf(focused);
  const dark = mix(tint, "#050b17", 0.8), lit = mix(tint, "#ffffff", 0.55);
  const total = (t) => fog * (g.body + (g.lit - g.body) * t ** 1.2);
  const layers = [];
  for (let i = 0; i < STEPS; i++) {
    const a = 1 - (1 - total((i + 1) / STEPS)) / (1 - total(i / STEPS));
    layers.push(withAlpha(mix(dark, lit, (i / (STEPS - 1)) ** 0.7), a));
  }
  return { dark, body: withAlpha(dark, total(0)), layers };
}

/** A glass globe at (x, y), radius r: its body (the whole disc) and the light contours over it. */
export function sphereShade(id, x, y, r, tint, fog = 1, focused = false) {
  const g = glassLayers(tint, fog, focused);
  const paths = CONTOURS.map((cmds, i) => ({
    t: "path", id: `sphere-lit-${id}-${i}`, fill: true,
    cmds: cmds.map((c) => [c[0], ...c.slice(1).map((n, j) => (j % 2 ? y : x) + r * n)]),
    bg: { kind: "solid", color: g.layers[i] },
  }));
  return { dark: g.dark, body: g.body, paths };
}

const rgba = (hex) => {
  const s = String(hex).replace("#", "");
  return { c: [0, 2, 4].map((i) => parseInt(s.slice(i, i + 2), 16)), a: s.length >= 8 ? parseInt(s.slice(6, 8), 16) / 255 : 1 };
};
const over = (under, color) => {
  const { c, a } = rgba(color), u = rgba(under).c;
  return "#" + u.map((v, i) => Math.round(v + (c[i] - v) * a).toString(16).padStart(2, "0")).join("");
};

/**
 * What the eye sees at a point of a globe (nx, ny: unit offsets from its middle) over the color `under` it: the same
 * layers the scene draws, blended in order. The scene picks a label's ink against this.
 */
export function glassColorAt(tint, nx, ny, { fog = 1, focused = false, under = "#1d2a3c" } = {}) {
  if (Math.hypot(nx, ny) > 1) return under;
  const g = glassLayers(tint, fog, focused);
  const level = lightAt(nx, ny);
  let out = over(under, g.body);
  for (let i = 0; i < STEPS && i / STEPS < level; i++) out = over(out, g.layers[i]);
  return out;
}

// ---- the cutout sphere (per shoal, depth mode)
//
// A sphere around the operator, its near cap cut away by a plane through its middle that faces the camera tilted
// `alpha` toward the top of the screen, so you look into a bowl: the rim is an ellipse, the near wall's outside a
// crescent under it, and everything inside the rim is the inner surface of the far wall. Hosts are lunes of that
// inner surface (a range of azimuth around the bowl's axis, pole to rim), each in its host color.
// Unit vectors are in the camera's frame: x right, y down the screen, z toward you.

/** The bowl's frame for a tilt `alpha`: its pole (the bottom, away from you) and the two rim axes. */
export function bowlFrame(alpha) {
  return {
    alpha,
    pole: { x: 0, y: Math.sin(alpha), z: -Math.cos(alpha) },
    e1: { x: 1, y: 0, z: 0 },
    e2: { x: 0, y: Math.cos(alpha), z: Math.sin(alpha) },
  };
}

/** The point of the bowl at polar angle `theta` from its pole (pi/2: the rim) and azimuth `phi` (0 right, pi/2 down). */
export function bowlPoint(fr, theta, phi) {
  const c = Math.cos(theta), s = Math.sin(theta), cp = Math.cos(phi), sp = Math.sin(phi);
  return {
    x: c * fr.pole.x + s * (cp * fr.e1.x + sp * fr.e2.x),
    y: c * fr.pole.y + s * (cp * fr.e1.y + sp * fr.e2.y),
    z: c * fr.pole.z + s * (cp * fr.e1.z + sp * fr.e2.z),
  };
}

/** How far inside the rim a projected point is: under 1 is seen through the opening, over 1 is behind the near wall. */
export const rimNorm = (fr, u) => Math.hypot(u.x, u.y / Math.cos(fr.alpha));

/** The largest polar angle at azimuth phi whose point is still seen, `margin` inside the rim (bisection). */
export function bowlReach(fr, phi, margin = 0.86) {
  let lo = 0, hi = Math.PI / 2;
  if (rimNorm(fr, bowlPoint(fr, hi, phi)) <= margin) return hi;
  for (let i = 0; i < 30; i++) {
    const mid = (lo + hi) / 2;
    if (rimNorm(fr, bowlPoint(fr, mid, phi)) <= margin) lo = mid; else hi = mid;
  }
  return lo;
}

// A circle on the bowl projects to an ellipse (the projection is affine), so one cubic per quarter keeps it exact.
const at = (cx, cy, R, u) => ({ x: cx + R * u.x, y: cy + R * u.y });
function bowlArc(cmds, cx, cy, R, point, tangent, a, b) {
  arc(cmds, (t) => at(cx, cy, R, point(t)), (t) => { const d = tangent(t); return { x: R * d.x, y: R * d.y }; }, a, b);
}
const meridian = (fr, phi) => [(t) => bowlPoint(fr, t, phi), (t) => {
  const c = Math.cos(t), s = Math.sin(t), cp = Math.cos(phi), sp = Math.sin(phi);
  return { x: -s * fr.pole.x + c * (cp * fr.e1.x + sp * fr.e2.x), y: -s * fr.pole.y + c * (cp * fr.e1.y + sp * fr.e2.y) };
}];
const latitude = (fr, theta) => [(p) => bowlPoint(fr, theta, p), (p) => {
  const s = Math.sin(theta), cp = Math.cos(p), sp = Math.sin(p);
  return { x: s * (-sp * fr.e1.x + cp * fr.e2.x), y: s * (-sp * fr.e1.y + cp * fr.e2.y) };
}];

/** A host's lune, pole to rim between azimuths a0 and a1 (a whole turn: the rim ellipse). */
export function luneCmds(fr, cx, cy, R, a0, a1) {
  if (a1 - a0 >= 2 * Math.PI - 1e-6) return latitudeCmds(fr, cx, cy, R, Math.PI / 2);
  const p0 = at(cx, cy, R, fr.pole), cmds = [["M", p0.x, p0.y]];
  const [m0, t0] = meridian(fr, a0), [m1, t1] = meridian(fr, a1), [rim, rt] = latitude(fr, Math.PI / 2);
  bowlArc(cmds, cx, cy, R, m0, t0, 0, Math.PI / 2);
  bowlArc(cmds, cx, cy, R, rim, rt, a0, a1);
  bowlArc(cmds, cx, cy, R, m1, t1, Math.PI / 2, 0);
  cmds.push(["Z"]);
  return cmds;
}

/** A whole circle of latitude (theta from the pole), closed. */
export function latitudeCmds(fr, cx, cy, R, theta) {
  const [p, t] = latitude(fr, theta), s = at(cx, cy, R, p(0)), cmds = [["M", s.x, s.y]];
  bowlArc(cmds, cx, cy, R, p, t, 0, 2 * Math.PI);
  cmds.push(["Z"]);
  return cmds;
}

/** The meridian at azimuth phi, pole to rim, as an open stroke. */
export function meridianCmds(fr, cx, cy, R, phi) {
  const [p, t] = meridian(fr, phi), s = at(cx, cy, R, p(0)), cmds = [["M", s.x, s.y]];
  bowlArc(cmds, cx, cy, R, p, t, 0, Math.PI / 2);
  return cmds;
}

/** The near wall's outside: between the sphere's lower silhouette and the rim's lower half. */
export function crescentCmds(fr, cx, cy, R) {
  const cmds = [["M", cx - R, cy]];
  arc(cmds, (t) => ({ x: cx + R * Math.cos(t), y: cy + R * Math.sin(t) }), (t) => ({ x: -R * Math.sin(t), y: R * Math.cos(t) }), Math.PI, 0);
  const [rim, rt] = latitude(fr, Math.PI / 2);
  bowlArc(cmds, cx, cy, R, rim, rt, 0, Math.PI);
  cmds.push(["Z"]);
  return cmds;
}

/** The inside's light: nested ellipses shrinking from the rim toward the spot that faces the light (lower right, as a
 * hollow is lit opposite to a ball), each adding a little alpha. `n` layers of `ellipse` commands. */
export const BOWL_GLOW = 20;
export function bowlGlow(fr, cx, cy, R) {
  const ry = Math.cos(fr.alpha);
  // the inner point whose normal faces the light: u = -LIGHT, inside the rim for the tilts in use
  const spot = { x: -LIGHT.x * 0.92, y: -LIGHT.y * 0.92 };
  return Array.from({ length: BOWL_GLOW }, (_, i) => {
    const s = 1 - (i / BOWL_GLOW) * 0.88;
    return { x: cx + R * spot.x * (1 - s), y: cy + R * spot.y * (1 - s), rx: R * s, ry: R * ry * s };
  });
}

/** The bowl's look: the inside's base, its glow layers, a host's wash over it, the near wall's outside. */
export const BOWL = { base: 0.22, glow: 0.02, wash: 0.2, shell: 0.72 };
const BOWL_BASE = "#16243a", BOWL_LIT = "#c9d8e6";

/** What the eye sees at a point inside the bowl (nx, ny: unit offsets from its middle), in a host's tint. */
export function bowlColorAt(fr, nx, ny, tint, under = "#1d2a3c") {
  if (rimNorm(fr, { x: nx, y: ny }) > 1) return under;
  let out = over(under, withAlpha(BOWL_BASE, BOWL.base));
  for (const e of bowlGlow(fr, 0, 0, 1)) if (((nx - e.x) / e.rx) ** 2 + ((ny - e.y) / e.ry) ** 2 <= 1) out = over(out, withAlpha(BOWL_LIT, BOWL.glow));
  return tint ? over(out, withAlpha(tint, BOWL.wash)) : out;
}
export { BOWL_BASE, BOWL_LIT };
