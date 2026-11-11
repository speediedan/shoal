// Smooth, directional globe shading using paths supported by both GPUI and the SVG preview.
// A light contour is a projected small circle, clipped by the visible hemisphere.
// Short cubic arcs keep the smooth lighting without thousands of native line calls per globe.
//
// A globe is glass: the nested contours each add a little alpha, so the shadow side is nearly clear and the lit side a
// tinted veil, with the floor, stems and the globes behind showing through. Opacity is the only thing hover changes.
// The per-shoal view's cutout sphere (a bowl seen from above) reuses the same light, seen from inside, and every floor
// shadow falls from it too.
import { mix, withAlpha } from "./geom.js";

export const LIGHT = { x: -0.46, y: -0.54, z: Math.sqrt(1 - 0.46 ** 2 - 0.54 ** 2) };
// Layers stack, so each adds 1 to 6 of the 255 alpha steps: 48 of them cover the range in steps finer than the old
// 128 opaque levels did, at under half their path commands.
export const STEPS = 48;
export const lightAt = (x, y) => LIGHT.x * x + LIGHT.y * y + LIGHT.z * Math.sqrt(Math.max(0, 1 - x * x - y * y));

/** How opaque a globe is, on its shadow side (`body`) and at its lit pole (`lit`): much less with nothing on it in
 * focus, a little more when the pointer is on it or on one of its roles. */
export const GLASS = { idle: { body: 0.05, lit: 0.27 }, focus: { body: 0.16, lit: 0.56 } };
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

// ---- floor shadows
//
// Every globe, the bowl and every lifted seat casts its shadow on the floor from the one LIGHT above (depth.js turns it
// into the world and finds where each shadow lands). A shadow is a stack of concentric ellipses, each adding a little
// alpha, so its edge falls off smoothly: a dark core where the floor is in full shadow, then a soft penumbra, wider the
// higher the caster floats. Like the glass, each layer's alpha is solved from the total wanted at its radius.
export const SHADOW_STEPS = 16;

/** How dark a shadow is at rho (1: the edge of the geometric shadow), for its darkest `peak` and penumbra `soft`. */
export function shadowAt(rho, peak, soft) {
  const t = Math.min(1, Math.max(0, (rho - (1 - soft)) / (2 * soft)));
  return peak * (1 - t * t * (3 - 2 * t)) * (1 - 0.25 * Math.min(1, rho) ** 2);
}

/** A shadow's layers, outermost first: {s, alpha}, s the layer's scale of the geometric shadow. Alpha is 8-bit, so
 * each layer is solved against what the rounded layers outside it already add up to; a layer that rounds to nothing
 * is left out. Three in four layers go to the penumbra, where the shadow changes fastest, the rest to the core. */
export function shadowLayers(peak, soft, n = SHADOW_STEPS) {
  const pen = Math.max(1, Math.round(n * 0.75)), core = n - pen;
  const radii = [...Array.from({ length: pen }, (_, i) => 1 + soft - (2 * soft * i) / pen),
    ...Array.from({ length: core }, (_, i) => (1 - soft) * (1 - i / core)), 0];
  const out = [];
  let below = 0;
  for (let i = 0; i < n; i++) {
    const s = radii[i], total = shadowAt((s + radii[i + 1]) / 2, peak, soft);
    const alpha = Math.round(Math.max(0, 1 - (1 - total) / (1 - below)) * 255) / 255;
    if (alpha <= 0) continue;
    out.push({ s, alpha });
    below = 1 - (1 - below) * (1 - alpha);
  }
  return out;
}

// ---- the cutout sphere (per shoal, depth mode)
//
// A sphere around the operator with its upper half cut away through its middle, so what is left is a bowl (a
// hemisphere) seen from above: the camera hangs `d` radii from the middle, `alpha` off the bowl's axis toward the bottom
// of the screen, and looks down into it over the near rim, so the near wall hides next to nothing. In perspective you,
// at the middle, are nearer than any point of the bowl and so larger, and the rim projects to an ellipse. Hosts are
// lunes of the inner surface (a range of azimuth around the bowl's axis, pole to rim) that tile it: together they are
// the whole bowl, each in its host color.
// Unit vectors are in the camera's frame: x right, y down the screen, z toward you; the camera sits at (0, 0, d).

/** The camera's distance from the bowl's middle, in sphere radii: smaller is stronger perspective. */
export const BOWL_DIST = 2.4;

/** The bowl's frame for a tilt `alpha` and a camera `d` radii away: its pole (the bottom), the two rim axes, and the
 * rim's ellipse on screen (in radii from the middle: symmetric about the vertical, its middle a little below yours). */
export function bowlFrame(alpha, d = BOWL_DIST) {
  const sa = Math.sin(alpha), ca = Math.cos(alpha), q = d * d - sa * sa;
  return {
    alpha, d,
    pole: { x: 0, y: sa, z: -ca },
    e1: { x: 1, y: 0, z: 0 },
    e2: { x: 0, y: ca, z: sa },
    rim: { cy: (d * sa * ca) / q, rx: d / Math.sqrt(q), ry: (d * d * ca) / q },
  };
}

/** Where a point of the camera's frame lands on screen, in radii from the middle, and its perspective scale `f`. */
export function bowlProject(fr, u) {
  const f = fr.d / Math.max(fr.d * 0.1, fr.d - u.z);
  return { x: u.x * f, y: u.y * f, f };
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

/** How far inside the rim a screen point (radii from the middle) is: under 1 is seen through the opening. */
export const rimNorm = (fr, s) => Math.hypot(s.x / fr.rim.rx, (s.y - fr.rim.cy) / fr.rim.ry);
/** The same for a point of the bowl: under 1 it is seen, over 1 it is behind the near wall. */
export const bowlSeen = (fr, u) => rimNorm(fr, bowlProject(fr, u));
/** A point's azimuth about the bowl's axis. */
export const bowlAzimuth = (fr, u) => Math.atan2(u.x * fr.e2.x + u.y * fr.e2.y + u.z * fr.e2.z, u.x * fr.e1.x + u.y * fr.e1.y + u.z * fr.e1.z);

/** The inner surface seen at a screen point (radii from the middle), or null outside the rim: where the line of sight,
 * in through the opening, leaves the sphere. */
export function bowlSurfaceAt(fr, sx, sy) {
  if (rimNorm(fr, { x: sx, y: sy }) > 1) return null;
  // from the camera C = (0, 0, d) along (sx, sy, -d): |C + t (sx, sy, -d)| = 1, the far root
  const d = fr.d, dd = sx * sx + sy * sy + d * d, disc = d ** 4 - dd * (d * d - 1);
  if (disc < 0) return null;
  const t = (d * d + Math.sqrt(disc)) / dd;
  return { x: t * sx, y: t * sy, z: d - t * d };
}

/** The largest polar angle at azimuth phi whose point is still seen, `margin` inside the rim (bisection). */
export function bowlReach(fr, phi, margin = 0.86) {
  let lo = 0, hi = Math.PI / 2;
  if (bowlSeen(fr, bowlPoint(fr, hi, phi)) <= margin) return hi;
  for (let i = 0; i < 30; i++) {
    const mid = (lo + hi) / 2;
    if (bowlSeen(fr, bowlPoint(fr, mid, phi)) <= margin) lo = mid; else hi = mid;
  }
  return lo;
}

/** The rim at azimuth phi, on screen (radii from the middle). */
export const bowlRimAt = (fr, phi) => bowlProject(fr, bowlPoint(fr, Math.PI / 2, phi));

// A circle on the bowl projects to an ellipse in perspective too, but not at an even pace round it: eighth turns keep
// each cubic within a fraction of a pixel of it.
const EIGHTH = Math.PI / 4;
function bowlArc(cmds, fr, cx, cy, R, point, tangent, a, b) {
  const n = Math.ceil(Math.abs(b - a) / EIGHTH - 1e-9);
  for (let i = 0; i < n; i++) {
    const t0 = a + ((b - a) * i) / n, t1 = a + ((b - a) * (i + 1)) / n;
    const at = (t) => { const s = bowlProject(fr, point(t)); return { x: cx + R * s.x, y: cy + R * s.y }; };
    // d/dt of the projection: the point's own motion, scaled, plus the scale changing as it nears or leaves the camera
    const speed = (t) => {
      const u = point(t), du = tangent(t), f = bowlProject(fr, u).f, df = (f * f * du.z) / fr.d;
      return { x: R * (du.x * f + u.x * df), y: R * (du.y * f + u.y * df) };
    };
    const p = at(t0), q = at(t1), u = speed(t0), v = speed(t1), k = (4 / 3) * Math.tan((t1 - t0) / 4);
    cmds.push(["C", p.x + k * u.x, p.y + k * u.y, q.x - k * v.x, q.y - k * v.y, q.x, q.y]);
  }
}
const meridian = (fr, phi) => [(t) => bowlPoint(fr, t, phi), (t) => {
  const c = Math.cos(t), s = Math.sin(t), cp = Math.cos(phi), sp = Math.sin(phi);
  const w = (k) => -s * fr.pole[k] + c * (cp * fr.e1[k] + sp * fr.e2[k]);
  return { x: w("x"), y: w("y"), z: w("z") };
}];
const latitude = (fr, theta) => [(p) => bowlPoint(fr, theta, p), (p) => {
  const s = Math.sin(theta), cp = Math.cos(p), sp = Math.sin(p);
  const w = (k) => s * (-sp * fr.e1[k] + cp * fr.e2[k]);
  return { x: w("x"), y: w("y"), z: w("z") };
}];
const startAt = (fr, cx, cy, R, u) => { const s = bowlProject(fr, u); return ["M", cx + R * s.x, cy + R * s.y]; };

/** A host's lune, pole to rim between azimuths a0 and a1 (a whole turn: the rim ellipse). */
export function luneCmds(fr, cx, cy, R, a0, a1) {
  if (a1 - a0 >= 2 * Math.PI - 1e-6) return latitudeCmds(fr, cx, cy, R, Math.PI / 2);
  const cmds = [startAt(fr, cx, cy, R, fr.pole)];
  const [m0, t0] = meridian(fr, a0), [m1, t1] = meridian(fr, a1), [rim, rt] = latitude(fr, Math.PI / 2);
  bowlArc(cmds, fr, cx, cy, R, m0, t0, 0, Math.PI / 2);
  bowlArc(cmds, fr, cx, cy, R, rim, rt, a0, a1);
  bowlArc(cmds, fr, cx, cy, R, m1, t1, Math.PI / 2, 0);
  cmds.push(["Z"]);
  return cmds;
}

/** A whole circle of latitude (theta from the pole), closed. */
export function latitudeCmds(fr, cx, cy, R, theta) {
  const [p, t] = latitude(fr, theta), cmds = [startAt(fr, cx, cy, R, p(0))];
  bowlArc(cmds, fr, cx, cy, R, p, t, 0, 2 * Math.PI);
  cmds.push(["Z"]);
  return cmds;
}

/** The meridian at azimuth phi, pole to rim, as an open stroke. */
export function meridianCmds(fr, cx, cy, R, phi) {
  const [p, t] = meridian(fr, phi), cmds = [startAt(fr, cx, cy, R, p(0))];
  bowlArc(cmds, fr, cx, cy, R, p, t, 0, Math.PI / 2);
  return cmds;
}

/** The sphere's outline on screen: a circle about the middle, in radii (the camera looks at the middle). */
export const bowlSilhouette = (fr) => fr.d / Math.sqrt(fr.d * fr.d - 1);

/** The near wall's outside, where it shows below the rim: none while the camera is over the opening (d sin alpha <= 1),
 * else a thin crescent between the sphere's outline and the near rim. */
export function crescentCmds(fr, cx, cy, R) {
  const h = fr.d * Math.sin(fr.alpha);
  if (h <= 1 + 1e-6) return [];
  const p1 = Math.asin(1 / h), p2 = Math.PI - p1, rs = bowlSilhouette(fr);
  const s = bowlRimAt(fr, p1), psi = Math.atan2(s.y, s.x);
  const cmds = [["M", cx + R * s.x, cy + R * s.y]];
  arc(cmds, (t) => ({ x: cx + R * rs * Math.cos(t), y: cy + R * rs * Math.sin(t) }), (t) => ({ x: -R * rs * Math.sin(t), y: R * rs * Math.cos(t) }), psi, Math.PI - psi);
  const [rim, rt] = latitude(fr, Math.PI / 2);
  bowlArc(cmds, fr, cx, cy, R, rim, rt, p2, p1);
  cmds.push(["Z"]);
  return cmds;
}

/** The inside's light: nested ellipses shrinking from the rim toward the spot that faces the light (lower right, as a
 * hollow is lit opposite to a ball), each adding a little alpha. `n` layers of `ellipse` commands. */
export const BOWL_GLOW = 20;
export function bowlGlow(fr, cx, cy, R) {
  const { cy: my, rx, ry } = fr.rim;
  // the inner point whose normal faces the light: u = -LIGHT, seen through the opening at the tilts in use
  const sp = bowlProject(fr, { x: -LIGHT.x, y: -LIGHT.y, z: -LIGHT.z });
  const spot = { x: sp.x * 0.92, y: my + (sp.y - my) * 0.92 };
  return Array.from({ length: BOWL_GLOW }, (_, i) => {
    const s = 1 - (i / BOWL_GLOW) * 0.88;
    return { x: cx + R * spot.x * (1 - s), y: cy + R * (my + (spot.y - my) * (1 - s)), rx: R * rx * s, ry: R * ry * s };
  });
}

/** The bowl's look: the inside's base, its glow layers, a host's wash over it (light, so edges over it read), the near
 * wall's outside. */
export const BOWL = { base: 0.16, glow: 0.018, wash: 0.1, outline: 0.5, shell: 0.72 };
const BOWL_BASE = "#16243a", BOWL_LIT = "#c9d8e6";

/** What the eye sees at a point inside the bowl (nx, ny: radii from its middle), in a host's tint. */
export function bowlColorAt(fr, nx, ny, tint, under = "#1d2a3c") {
  if (rimNorm(fr, { x: nx, y: ny }) > 1) return under;
  let out = over(under, withAlpha(BOWL_BASE, BOWL.base));
  for (const e of bowlGlow(fr, 0, 0, 1)) if (((nx - e.x) / e.rx) ** 2 + ((ny - e.y) / e.ry) ** 2 <= 1) out = over(out, withAlpha(BOWL_LIT, BOWL.glow));
  return tint ? over(out, withAlpha(tint, BOWL.wash)) : out;
}
export { BOWL_BASE, BOWL_LIT };
