// Smooth, directional globe shading using paths supported by both GPUI and the SVG preview.
// A light contour is a projected small circle, clipped by the visible hemisphere.
// Short cubic arcs keep the smooth lighting without thousands of native line calls per globe.
//
// A globe is glass: the nested contours each add a little alpha, so the shadow side is nearly clear and the lit side a
// tinted veil, with the floor, stems and the globes behind showing through. Opacity is the only thing hover changes.
// Each globe is shaded from the direction of the one light (a point in the world, depth.js) as the camera sees it from
// that globe; the per-shoal view's cutout sphere has its own point light, fixed to the bowl (bowlLightAt), and every
// shadow in either view is cast from its view's light by following the ray from it through the caster.
import { conjugateEllipseCmds, mix, withAlpha } from "./geom.js";

/** The light as the camera sees it at the opening views (x right, y down the screen, z toward you): up and to the left,
 * toward you. A globe or the glass you is shaded from the light's actual direction at it, which is this at the opening
 * views and moves as the camera orbits (the light stays put in the world). */
export const LIGHT = { x: -0.46, y: -0.54, z: Math.sqrt(1 - 0.46 ** 2 - 0.54 ** 2) };
// Layers stack, so each adds 1 to 6 of the 255 alpha steps: 48 of them cover the range in steps finer than the old
// 128 opaque levels did, at under half their path commands.
export const STEPS = 48;
/** A camera-space light direction, rounded so contours can be cached (to a thousandth) and kept on the camera's side. */
export function lightOf(L = LIGHT) {
  const x = Math.round(L.x * 1000) / 1000, y = Math.round(L.y * 1000) / 1000;
  const side = Math.hypot(x, y);
  // straight at the camera the contours are concentric circles, any way round: lean it a hair so they have an axis
  const [lx, ly] = side < 1e-3 ? [-1e-3, -1e-3] : side > 0.999 ? [(x / side) * 0.999, (y / side) * 0.999] : [x, y];
  return { x: lx, y: ly, z: Math.sqrt(Math.max(0, 1 - lx * lx - ly * ly)) };
}
export const lightAt = (x, y, L = LIGHT) => { const l = lightOf(L); return l.x * x + l.y * y + l.z * Math.sqrt(Math.max(0, 1 - x * x - y * y)); };

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

function contour(level, L) {
  const side = Math.hypot(L.x, L.y), radius = Math.sqrt(1 - level * level);
  const u = { x: -L.y / side, y: L.x / side };
  const v = { x: -L.z * L.x / side, y: -L.z * L.y / side };
  const point = (a) => ({ x: level * L.x + radius * (u.x * Math.cos(a) + v.x * Math.sin(a)),
    y: level * L.y + radius * (u.y * Math.cos(a) + v.y * Math.sin(a)) });
  const tangent = (a) => ({ x: radius * (-u.x * Math.sin(a) + v.x * Math.cos(a)),
    y: radius * (-u.y * Math.sin(a) + v.y * Math.cos(a)) });
  const clipped = level < side;
  const start = clipped ? -Math.asin(level * L.z / (radius * side)) : -Math.PI / 2;
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

// the contours for a light, unit-sized, cached by its rounded direction (a few lights a scene: one per globe)
const contourCache = new Map();
export function contoursFor(L = LIGHT) {
  const l = lightOf(L), key = `${l.x} ${l.y}`;
  let c = contourCache.get(key);
  if (!c) {
    if (contourCache.size >= 64) contourCache.clear();
    c = Array.from({ length: STEPS }, (_, i) => contour(i / STEPS, l));
    contourCache.set(key, c);
  }
  return c;
}

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

/** A glass globe at (x, y), radius r, lit from `light` (camera space): its body (the whole disc) and the light
 * contours over it. */
export function sphereShade(id, x, y, r, tint, fog = 1, focused = false, light = LIGHT) {
  const g = glassLayers(tint, fog, focused);
  const paths = contoursFor(light).map((cmds, i) => ({
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
export function glassColorAt(tint, nx, ny, { fog = 1, focused = false, under = "#1d2a3c", light = LIGHT } = {}) {
  if (Math.hypot(nx, ny) > 1) return under;
  const g = glassLayers(tint, fog, focused);
  const level = lightAt(nx, ny, light);
  let out = over(under, g.body);
  for (let i = 0; i < STEPS && i / STEPS < level; i++) out = over(out, g.layers[i]);
  return out;
}

// ---- floor shadows
//
// Every globe and every lifted seat casts its shadow on the floor from the one light, a point above the scene (depth.js
// lightPoint and castShadow find where the ray through each lands), and the bowl on its own floor from its light
// (bowlFloor). GPUI has no blur and no radial gradient, so a shadow is a stack of nested ellipses, each adding a little
// alpha: a dark core where the floor is in full shadow, then a soft penumbra, wider the higher the caster floats. The
// stack is cut so every step between two layers is the same small step in how dark the shadow is as drawn,
// SHADOW_STEP: so the layers crowd where the falloff is steep, the penumbra, and spread where it is flat, the core, and
// no ring shows (2026-11-13: 16 layers, three in four of them in the penumbra, stepped up to 5 percent at a time and
// read as concentric rings). The step cannot be made as fine as one likes: GPUI blends each layer into an 8-bit
// target, rounding every channel, so a layer that would move a channel by under half a level moves it not at all (a
// stack of 1 percent layers stalls part way and leaves the shadow pale). SHADOW_STEP is about two of the 8-bit levels
// between the depth canvas and the shadow's ink, and each layer's alpha is solved by blending the rounded layers
// outside it over that canvas color as the renderer does (shadowLayers), so the shadow as drawn darkens evenly to its
// darkest. A small shadow needs fewer: a band under SHADOW_BAND_PX wide does not read as a band, so its count is held
// to one per that much of its radius, which keeps the many small ones (roles, seats) within the render budget. Each
// layer is two arcs (geom.js ellipseArcCmds), four path commands.
export const SHADOW_STEP = 0.022;
export const SHADOW_BAND_PX = 3;
export const SHADOW_MAX = 48;
export const SHADOW_INK = "#01040a";
/** The color a shadow's alphas are solved over: the depth canvas's middle (scene.js DEPTH_UNDER). */
export const SHADOW_UNDER = "#2e3f57";

/** How dark a shadow is at rho (1: the edge of the geometric shadow), for its darkest `peak` and penumbra `soft`. */
export function shadowAt(rho, peak, soft) {
  const t = Math.min(1, Math.max(0, (rho - (1 - soft)) / (2 * soft)));
  return peak * (1 - t * t * (3 - 2 * t)) * (1 - 0.25 * Math.min(1, rho) ** 2);
}

/** How many layers a shadow of on-screen radius `px` (its geometric shadow's, the mean of its two semi-axes) is drawn
 * with: one per SHADOW_STEP of its darkest, or one per SHADOW_BAND_PX of its outer radius if that is fewer; 3 at least. */
export function shadowSteps(peak, soft, px = Infinity) {
  const byTone = Math.ceil(peak / SHADOW_STEP - 1e-9), byPx = Math.ceil(((1 + soft) * px) / SHADOW_BAND_PX);
  return Math.max(3, Math.min(SHADOW_MAX, byTone, byPx));
}

/** An #rrggbb color's 8-bit channels. */
export const rgb8 = (hex) => [1, 3, 5].map((i) => parseInt(String(hex).slice(i, i + 2), 16));
const luma = (c) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
/** One layer of `ink` at 8-bit alpha A over the 8-bit color c, as an 8-bit target stores it (each channel rounded). */
export const blend8 = (c, ink, A) => c.map((v, i) => Math.round(v + ((ink[i] - v) * A) / 255));

const layerCache = new Map();
/** A shadow's layers, outermost first: {s, alpha}, s the layer's scale of the geometric shadow. The n layers step the
 * shadow's darkness up from nothing at its soft edge to its darkest in n equal steps: layer j holds the j-th, and its
 * edge is where the falloff (shadowAt) crosses the middle of that step, so the band between two edges is drawn at the
 * falloff's own darkness there, within half a step. Each layer's alpha is the 8-bit one that brings the color as
 * drawn (the rounded layers outside it blended over `under`, blend8) nearest the falloff's color at its step; a layer
 * that would change nothing, or take the color further from it, is left out, and the next one catches up. */
export function shadowLayers(peak, soft, n = shadowSteps(peak, soft), under = SHADOW_UNDER) {
  const key = `${peak.toFixed(4)} ${soft.toFixed(4)} ${n} ${under}`;
  if (layerCache.has(key)) return layerCache.get(key);
  const top = shadowAt(0, peak, soft), ink = rgb8(SHADOW_INK), base = rgb8(under), yInk = luma(ink);
  // the falloff only falls, so the radius where it is `level` is found by bisection
  const rhoAt = (level) => {
    let lo = 0, hi = 1 + soft;
    for (let i = 0; i < 30; i++) { const mid = (lo + hi) / 2; if (shadowAt(mid, peak, soft) > level) lo = mid; else hi = mid; }
    return (lo + hi) / 2;
  };
  const out = [];
  let c = base;
  for (let j = 1; j <= n; j++) {
    const want = luma(base) + (yInk - luma(base)) * ((top * j) / n), now = luma(c);
    const guess = Math.round(255 * (1 - (want - yInk) / Math.max(1e-9, now - yInk)));
    let best = null;
    for (let A = Math.max(1, guess - 3); A <= Math.min(255, guess + 3); A++) {
      const d = blend8(c, ink, A), err = Math.abs(luma(d) - want);
      if (d.some((v, i) => v !== c[i]) && (!best || err < best.err)) best = { A, d, err };
    }
    if (!best || best.err >= Math.abs(now - want)) continue;
    out.push({ s: rhoAt((top * (j - 0.5)) / n), alpha: best.A / 255 });
    c = best.d;
  }
  if (layerCache.size >= 256) layerCache.clear();
  layerCache.set(key, out);
  return out;
}

// ---- the cutout sphere (per shoal, depth mode)
//
// A sphere around the operator with the part nearest the camera cut away, so what is left is a bowl seen from above
// and from the side: the camera hangs `d` radii from the middle and sees the bowl's axis turned `alpha` off the line of
// sight toward the bottom of the screen (pitch) and `beta` about the screen's vertical (yaw), so its bottom (the pole)
// sits below and to one side of you and the two read apart in depth. The cut is a plane slanted toward the camera:
// the far wall stays high and the near wall is cut lower, to just past where the camera would see over it, so the near
// wall's outside shows only as a thin crescent below the lip and hides next to nothing of the inside. The wall has a
// thickness (BOWL_LIP), so the cut face is a lip, a band between the rim and its outer edge, and the opening reads as
// one. In perspective you, at the middle, are nearer than any role on the inside and so larger, and the rim (the cut
// circle) projects to an ellipse turned by the yaw. Hosts are lunes of the inner surface (a range of azimuth around the
// bowl's axis, pole to rim) that tile it: together they are the whole bowl, each in its host color.
// Unit vectors are in the camera's frame: x right, y down the screen, z toward you; the camera sits at (0, 0, d).

/** The camera's distance from the bowl's middle, in sphere radii: smaller is stronger perspective. */
export const BOWL_DIST = 2.4;
/** The bowl's turn about the screen's vertical, so the camera sees into it from the side as well as from above. */
export const BOWL_TURN = (30 * Math.PI) / 180;
// How far past the last cut that would keep no point of the inner sphere facing the camera the cut is made: below
// zero the near wall's top shows from outside, as a crescent under the lip. 17 degrees (9 before, 2026-11-13): at the
// opening view, turned far round to the side, the sphere's outer wall shows as a shell beside the opening, from its top
// round to its bottom, so the bowl reads as a sphere seen from behind and to the side; past about 20 the near wall rose
// far enough to crowd the roles on the bottom's side of the inside.
const CUT_MARGIN = (-17 * Math.PI) / 180;
/** How far past the equator the far wall rises (polar angle from the bottom, above a hemisphere's): more of the back
 * shows over the inside, so it reads as more than a hemisphere, not a dish (maintainer, 2026-11-13: 14 degrees, then
 * 22 with the opening view turned to the side, so more of the far wall stands behind the opening). The near wall's cut
 * does not move with it, so nothing more of the inside is hidden. */
export const BOWL_FAR_EXTRA = (22 * Math.PI) / 180;
/** The wall's thickness, in sphere radii: the lip's width. */
export const BOWL_LIP = 0.035;

const dot = (a, b) => a.x * b.x + a.y * b.y + a.z * b.z;
const comb = (...terms) => {
  const out = { x: 0, y: 0, z: 0 };
  for (let i = 0; i < terms.length; i += 2) for (const k of ["x", "y", "z"]) out[k] += terms[i] * terms[i + 1][k];
  return out;
};
const unit = (v) => { const l = Math.hypot(v.x, v.y, v.z) || 1; return { x: v.x / l, y: v.y / l, z: v.z / l }; };

/** The ellipse a circle (middle c, unit axes f1 and f2, radius rho) projects to: its middle, two conjugate semi-axes
 * {a, b} (its principal ones), its quadratic form m ((s - c)' m (s - c) = 1 on it), and its half-width and half-height.
 * The circle's homogeneous image H (cos t, sin t, 1) is a conic whose matrix is H^-T diag(1, 1, -1) H^-1. */
function projectedCircle(d, c, f1, f2, rho) {
  const H = [[d * rho * f1.x, d * rho * f2.x, d * c.x], [d * rho * f1.y, d * rho * f2.y, d * c.y], [-rho * f1.z, -rho * f2.z, d - c.z]];
  const [[a, b, e], [f, g, k], [l, m, n]] = H;
  const det = a * (g * n - k * m) - b * (f * n - k * l) + e * (f * m - g * l);
  const I = [[g * n - k * m, e * m - b * n, b * k - e * g], [k * l - f * n, a * n - e * l, e * f - a * k], [f * m - g * l, b * l - a * m, a * g - b * f]]
    .map((row) => row.map((v) => v / det));
  const D = [1, 1, -1];
  const Q = (i, j) => D.reduce((t, s, r) => t + I[r][i] * s * I[r][j], 0);
  const p = Q(0, 0), q = Q(0, 1), r = Q(1, 1), bx = Q(0, 2), by = Q(1, 2), q0 = Q(2, 2);
  const dm = p * r - q * q, cx = (-r * bx + q * by) / dm, cy = (q * bx - p * by) / dm;
  const scale = p * cx * cx + 2 * q * cx * cy + r * cy * cy - q0;
  const M = [p / scale, q / scale, r / scale];
  const mid = (M[0] + M[2]) / 2, rad = Math.hypot((M[0] - M[2]) / 2, M[1]);
  const th = 0.5 * Math.atan2(2 * M[1], M[0] - M[2]), l1 = 1 / Math.sqrt(mid + rad), l2 = 1 / Math.sqrt(mid - rad);
  const ax = { x: l1 * Math.cos(th), y: l1 * Math.sin(th) }, bxy = { x: -l2 * Math.sin(th), y: l2 * Math.cos(th) };
  return { cx, cy, a: ax, b: bxy, m: M, w: Math.hypot(ax.x, bxy.x), h: Math.hypot(ax.y, bxy.y) };
}

/** The bowl's frame for a pitch `alpha`, a camera `d` radii away and a yaw `beta`: its pole (the bottom), the two axes
 * across it, the cut plane {u . n = h} with its circle (middle h n, radius rho, axes f1 and f2), and the rim's ellipse
 * on screen (in radii from the middle). */
function bowlAxes(alpha, beta) {
  const sa = Math.sin(alpha), ca = Math.cos(alpha), sb = Math.sin(beta), cb = Math.cos(beta);
  return { pole: { x: -ca * sb, y: sa, z: -ca * cb }, e1: { x: cb, y: 0, z: -sb }, e2: { x: sa * sb, y: ca, z: sa * cb } };
}
export function bowlFrame(alpha, d = BOWL_DIST, beta = BOWL_TURN) {
  const { pole, e1, e2 } = bowlAxes(alpha, beta);
  // The cut. A point of the unit sphere faces the camera when u.z > 1 / d, and a cut whose normal is `toward` off the
  // line of sight keeps none when acos(h) >= toward + acos(1 / d) (kappa, less a little: CUT_MARGIN). Its normal leans
  // `lean` from the bowl's up toward the camera, so the rim's polar angle runs from pi - tilt - kappa on the near side to
  // that plus 2 lean on the far side: the lean that keeps the far wall up to BOWL_FAR_EXTRA past the equator (a little
  // over a hemisphere's rim) cuts away only the near wall. A camera that already sees all of that inside keeps it whole.
  const tilt = Math.acos(Math.max(-1, Math.min(1, -pole.z))), kappa = Math.acos(1 / d) + CUT_MARGIN;
  const lean = Math.max(0, (tilt + kappa - Math.PI / 2 + BOWL_FAR_EXTRA) / 2), toward = tilt - lean;
  const side = Math.hypot(pole.x, pole.y) > 1e-9 ? unit({ x: -pole.x, y: -pole.y, z: 0 }) : { x: 0, y: -1, z: 0 };
  const n = comb(Math.cos(toward), { x: 0, y: 0, z: 1 }, Math.sin(toward), side);
  const h = lean > 0 ? Math.cos(toward + kappa) : 0;
  const f1 = unit(comb(1, e1, -dot(e1, n), n));
  const f2 = unit(comb(1, e2, -dot(e2, n), n, -dot(e2, f1), f1));
  const rho = Math.sqrt(1 - h * h), c = comb(h, n);
  const cut = { n, h, c, rho, f1, f2, pn: dot(pole, n), e1n: dot(e1, n), e2n: dot(e2, n) };
  const outer = 1 + BOWL_LIP;
  return { alpha, beta, d, pole, e1, e2, cut, outer, rim: projectedCircle(d, c, f1, f2, rho),
    lip: projectedCircle(d, c, f1, f2, Math.sqrt(outer * outer - h * h)) };
}

/** Where a point of the camera's frame lands on screen, in radii from the middle, and its perspective scale `f`. */
export function bowlProject(fr, u) {
  const f = fr.d / Math.max(fr.d * 0.1, fr.d - u.z);
  return { x: u.x * f, y: u.y * f, f };
}

/** The point of the bowl at polar angle `theta` from its pole and azimuth `phi` about its axis (e1 at 0, e2 at pi/2). */
export function bowlPoint(fr, theta, phi) {
  const c = Math.cos(theta), s = Math.sin(theta), cp = Math.cos(phi), sp = Math.sin(phi);
  return {
    x: c * fr.pole.x + s * (cp * fr.e1.x + sp * fr.e2.x),
    y: c * fr.pole.y + s * (cp * fr.e1.y + sp * fr.e2.y),
    z: c * fr.pole.z + s * (cp * fr.e1.z + sp * fr.e2.z),
  };
}

/** The polar angle of the rim at azimuth phi: where the meridian meets the cut, A cos(theta) + B sin(theta) = h. */
export function rimTheta(fr, phi) {
  const { pn, e1n, e2n, h } = fr.cut, B = Math.cos(phi) * e1n + Math.sin(phi) * e2n;
  return Math.atan2(-B, -pn) + Math.acos(Math.max(-1, Math.min(1, -h / Math.hypot(pn, B))));
}
/** Whether a point of the sphere is kept (on the bowl's side of the cut). */
export const bowlKept = (fr, u) => dot(u, fr.cut.n) <= fr.cut.h + 1e-9;

/** How far inside the rim a screen point (radii from the middle) is, by the rim ellipse's own measure: 1 on the rim,
 * under 1 seen through the opening. */
export function rimNorm(fr, s) {
  const { cx, cy, m } = fr.rim, x = s.x - cx, y = s.y - cy;
  return Math.sqrt(Math.max(0, m[0] * x * x + 2 * m[1] * x * y + m[2] * y * y));
}
/** For the bowl drawn at screen point o, radius R: whether an ellipse on screen (middle c, conjugate semi-axes a and b)
 * lies whole inside the rim, where the bowl's opaque inside covers it (24 points round it, a little inside the rim). */
export const bowlCovers = (fr, o, R) => (c, a, b) => {
  for (let i = 0; i < 24; i++) {
    const t = (i * Math.PI) / 12, x = c.x + a.x * Math.cos(t) + b.x * Math.sin(t), y = c.y + a.y * Math.cos(t) + b.y * Math.sin(t);
    if (rimNorm(fr, { x: (x - o.x) / R, y: (y - o.y) / R }) > 0.98) return false;
  }
  return true;
};
/** The same for a point of the bowl: 1 on the rim, under 1 inside it. */
export const bowlSeen = (fr, u) => rimNorm(fr, bowlProject(fr, u));
/** A point's azimuth about the bowl's axis. */
export const bowlAzimuth = (fr, u) => Math.atan2(dot(u, fr.e2), dot(u, fr.e1));

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

/** The largest polar angle at azimuth phi whose point is still `margin` inside the rim on screen and no nearer the
 * camera than `zMax` (bisection: the pole meets both, and so does what it returns). */
export function bowlReach(fr, phi, margin = 0.86, zMax = Infinity) {
  const ok = (t) => { const u = bowlPoint(fr, t, phi); return bowlSeen(fr, u) <= margin && u.z <= zMax; };
  let lo = 0, hi = rimTheta(fr, phi);
  if (ok(hi)) return hi;
  for (let i = 0; i < 30; i++) {
    const mid = (lo + hi) / 2;
    if (ok(mid)) lo = mid; else hi = mid;
  }
  return lo;
}

/** The rim at azimuth phi, on screen (radii from the middle). */
export const bowlRimAt = (fr, phi) => bowlProject(fr, bowlPoint(fr, rimTheta(fr, phi), phi));
/** A ring k times the cut circle, in its plane, at the rim's azimuth phi (a point of the camera's frame). */
export function bowlRing(fr, phi, k) {
  const r = bowlPoint(fr, rimTheta(fr, phi), phi), c = fr.cut.c;
  return comb(1, c, k, comb(1, r, -1, c));
}

// A circle on the bowl projects to an ellipse in perspective too, but not at an even pace round it: eighth turns keep
// each cubic within a fraction of a pixel of it.
const EIGHTH = Math.PI / 4;
function bowlArc(cmds, fr, cx, cy, R, point, tangent, a, b) {
  const n = Math.max(1, Math.ceil(Math.abs(b - a) / EIGHTH - 1e-9));
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
// the cut circle, by its own angle psi from f1 toward f2
const cutCircle = (fr) => {
  const { c, rho, f1, f2 } = fr.cut;
  return [(p) => comb(1, c, rho * Math.cos(p), f1, rho * Math.sin(p), f2), (p) => comb(-rho * Math.sin(p), f1, rho * Math.cos(p), f2)];
};
const cutAngle = (fr, u) => Math.atan2(dot(u, fr.cut.f2), dot(u, fr.cut.f1));
const startAt = (fr, cx, cy, R, u) => { const s = bowlProject(fr, u); return ["M", cx + R * s.x, cy + R * s.y]; };

/** The rim: the cut circle's ellipse on screen, closed. */
export function rimCmds(fr, cx, cy, R) {
  const { cx: mx, cy: my, a, b } = fr.rim;
  return conjugateEllipseCmds(cx + R * mx, cy + R * my, { x: R * a.x, y: R * a.y }, { x: R * b.x, y: R * b.y });
}

/** How far up the meridian at phi the inside is seen: to the rim, or on the near side to where the near wall's top
 * comes between (the meridian leaves the rim's ellipse on screen there). */
export function seenTheta(fr, phi) {
  const top = rimTheta(fr, phi), n = 32;
  let prev = 0;
  for (let i = 1; i < n; i++) {
    const t = (top * i) / n;
    if (bowlSeen(fr, bowlPoint(fr, t, phi)) > 1) {
      let lo = prev, hi = t;
      for (let k = 0; k < 30; k++) { const mid = (lo + hi) / 2; if (bowlSeen(fr, bowlPoint(fr, mid, phi)) <= 1) lo = mid; else hi = mid; }
      return lo;
    }
    prev = t;
  }
  return top;
}
// The cut circle's angle at a screen point on the rim's ellipse: where the line of sight through it meets the cut.
function cutAngleAt(fr, s) {
  const { n, h } = fr.cut, D = { x: s.x, y: s.y, z: -fr.d }, t = (h - fr.d * n.z) / dot(D, n);
  return cutAngle(fr, { x: t * s.x, y: t * s.y, z: fr.d + t * D.z });
}

/** A host's lune, pole to rim between azimuths a0 and a1, along the cut between them (a whole turn: the rim). Each side
 * runs up its meridian as far as it is seen, and the top along the rim between where they leave it. */
export function luneCmds(fr, cx, cy, R, a0, a1) {
  if (a1 - a0 >= 2 * Math.PI - 1e-6) return rimCmds(fr, cx, cy, R);
  const cmds = [startAt(fr, cx, cy, R, fr.pole)];
  const th0 = seenTheta(fr, a0), th1 = seenTheta(fr, a1);
  const [m0, t0] = meridian(fr, a0), [m1, t1] = meridian(fr, a1), [cut, ct] = cutCircle(fr);
  // the cut circle's angle at each end, unwrapped to turn the same way, and about as far, as the azimuths do
  const end = (th, a) => cutAngleAt(fr, bowlProject(fr, bowlPoint(fr, th, a)));
  const p0 = end(th0, a0), raw = end(th1, a1) - p0 - (a1 - a0);
  const p1 = p0 + (a1 - a0) + raw - 2 * Math.PI * Math.round(raw / (2 * Math.PI));
  bowlArc(cmds, fr, cx, cy, R, m0, t0, 0, th0);
  bowlArc(cmds, fr, cx, cy, R, cut, ct, p0, p1);
  bowlArc(cmds, fr, cx, cy, R, m1, t1, th1, 0);
  cmds.push(["Z"]);
  return cmds;
}

/** A whole circle of latitude (theta from the pole), closed: below the rim's lowest point, all of it is kept. */
export function latitudeCmds(fr, cx, cy, R, theta) {
  // a whole circle projects to an exact ellipse: four cubics, not eight
  const e = projectedCircle(fr.d, comb(Math.cos(theta), fr.pole), fr.e1, fr.e2, Math.sin(theta));
  return conjugateEllipseCmds(cx + R * e.cx, cy + R * e.cy, { x: R * e.a.x, y: R * e.a.y }, { x: R * e.b.x, y: R * e.b.y });
}

/** The meridian at azimuth phi, pole to rim (as far as it is seen), as an open stroke. */
export function meridianCmds(fr, cx, cy, R, phi) {
  const [p, t] = meridian(fr, phi), cmds = [startAt(fr, cx, cy, R, p(0))];
  bowlArc(cmds, fr, cx, cy, R, p, t, 0, seenTheta(fr, phi));
  return cmds;
}

/** The lip, the wall's cut face: a band between the rim's ellipse and the outer edge's, one closed path (out round the
 * outer edge, back round the inner). */
export function lipCmds(fr, cx, cy, R) {
  const at = (e, sign) => conjugateEllipseCmds(cx + R * e.cx, cy + R * e.cy, { x: R * e.a.x, y: R * e.a.y }, { x: sign * R * e.b.x, y: sign * R * e.b.y });
  const out = at(fr.lip, 1), inn = at(fr.rim, -1);
  return [...out.slice(0, -1), ["L", inn[0][1], inn[0][2]], ...inn.slice(1, -1), ["Z"]];
}

/** The near wall's outside, where the cut leaves its top facing the camera: a thin crescent between the lip's outer
 * edge and the outer wall's outline on screen (a circle about the middle), or none. */
export function crescentCmds(fr, cx, cy, R) {
  const { c, h, f1, f2 } = fr.cut, Ro = fr.outer, rho = Math.sqrt(Ro * Ro - h * h), d = fr.d;
  // the lip's outer edge c + rho (cos psi f1 + sin psi f2) faces the camera where its z > Ro^2 / d
  const A = Math.hypot(f1.z, f2.z), psi0 = Math.atan2(f2.z, f1.z), k = (Ro * Ro / d - c.z) / (rho * A);
  if (!(k < 1 - 1e-6)) return [];
  const del = Math.acos(Math.max(-1, k));
  const point = (p) => comb(1, c, rho * Math.cos(p), f1, rho * Math.sin(p), f2);
  const tangent = (p) => comb(-rho * Math.sin(p), f1, rho * Math.cos(p), f2);
  const cmds = [startAt(fr, cx, cy, R, point(psi0 - del))];
  bowlArc(cmds, fr, cx, cy, R, point, tangent, psi0 - del, psi0 + del);
  // back along the outline, the short way, from where the edge leaves it to where it met it
  const rs = (Ro * d) / Math.sqrt(d * d - Ro * Ro), p1 = bowlProject(fr, point(psi0 + del)), p0 = bowlProject(fr, point(psi0 - del));
  const a1 = Math.atan2(p1.y, p1.x), a0 = Math.atan2(p0.y, p0.x), sweep = ((a0 - a1 + 3 * Math.PI) % (2 * Math.PI)) - Math.PI;
  arc(cmds, (t) => ({ x: cx + R * rs * Math.cos(t), y: cy + R * rs * Math.sin(t) }), (t) => ({ x: -R * rs * Math.sin(t), y: R * rs * Math.cos(t) }), a1, a1 + sweep);
  cmds.push(["Z"]);
  return cmds;
}

// ---- the bowl's one light
//
// The bowl's world is its own frame (its bottom down the axis, e1 and e2 across it), so the light, a point in that world,
// stays put on the bowl as the camera orbits round it, as a lamp over a real bowl would. It is placed from the view
// BOWL_LIGHT.tilt and .turn (10 and -40: the bowl's old opening view), where it hangs BOWL_LIGHT.dist radii from the
// middle in the camera direction BOWL_LIGHT.home: above the opening, up and a little left of the camera there. It stayed
// there when the opening view moved round to the side (depth.js BOWL_HOME_TILT and BOWL_HOME_TURN, 2026-11-13), so the
// opening view is lit as the maintainer saw it when they orbited there and picked it: the light above and behind the
// opening, your shadow on the bottom's wall and the bowl's shadow on the floor to its right. Everything
// in the bowl is lit and shadowed from it by following the ray from it: the inside's light and its falloff, the lip's
// shade, your shadow and the edges' on the wall, the glass you's highlight, the bowl's shadow on its floor.
export const BOWL_LIGHT = { tilt: (10 * Math.PI) / 180, turn: (-40 * Math.PI) / 180, home: unit({ x: 0.03, y: -0.36, z: 0.93 }), dist: 4 };
const BOWL_LIGHT_K = (() => {
  const { pole, e1, e2 } = bowlAxes(BOWL_LIGHT.tilt, BOWL_LIGHT.turn), h = BOWL_LIGHT.home;
  return [dot(h, pole), dot(h, e1), dot(h, e2)].map((v) => v * BOWL_LIGHT.dist);
})();
const ORIGIN = { x: 0, y: 0, z: 0 };
/** Where the light is, in the camera's frame for `fr` (sphere radii from the middle). */
export function bowlLightAt(fr) {
  if (!fr.lamp) fr.lamp = comb(BOWL_LIGHT_K[0], fr.pole, BOWL_LIGHT_K[1], fr.e1, BOWL_LIGHT_K[2], fr.e2);
  return fr.lamp;
}
/** The unit way to the light from a point u (the middle by default). */
export const bowlLightDir = (fr, u = ORIGIN) => unit(comb(1, bowlLightAt(fr), -1, u));
/** Where the ray from the light through a point inside the sphere (`at`) leaves it: the point's shadow on the inner
 * wall. The far root of |Lp + t (at - Lp)| = 1. */
export function bowlCast(fr, at) {
  const Lp = bowlLightAt(fr), D = comb(1, at, -1, Lp), a = dot(D, D), b = 2 * dot(Lp, D), c = dot(Lp, Lp) - 1;
  return comb(1, Lp, (-b + Math.sqrt(Math.max(0, b * b - 4 * a * c))) / (2 * a), D);
}
const cross = (a, b) => ({ x: a.y * b.z - a.z * b.y, y: a.z * b.x - a.x * b.z, z: a.x * b.y - a.y * b.x });
const len = (v) => Math.hypot(v.x, v.y, v.z);
// two axes across a unit normal n, the first across the way `ray` comes in (any, when it comes in square)
function acrossOf(ray, n) {
  let a = cross(ray, n);
  if (len(a) < 1e-6) a = cross(Math.abs(n.x) < 0.9 ? { x: 1, y: 0, z: 0 } : { x: 0, y: 1, z: 0 }, n);
  a = unit(a);
  return [a, unit(cross(n, a))];
}
/**
 * The shadow a ball of radius rb at `at` casts where the light's ray through its middle meets a surface at `c`, whose unit
 * normal there is n: an ellipse in the surface's tangent plane, rb across the ray and rb / cos(incidence) along it, both
 * grown by how much further from the light the surface is than the ball (a point light's shadow spreads). {c, A, B}: its
 * middle and two semi-axes, in the camera's frame.
 */
export function ballShadow(Lp, at, rb, c, n) {
  const ray = unit(comb(1, c, -1, Lp)), grow = len(comb(1, c, -1, Lp)) / Math.max(1e-9, len(comb(1, at, -1, Lp)));
  const [across, along] = acrossOf(ray, n), cosI = Math.max(0.35, Math.abs(dot(ray, n)));
  return { c, A: comb(rb * grow, across), B: comb((rb * grow) / cosI, along) };
}

/** The inside's shade from its own lip: the light comes in through the opening from above, so the wall under the lip on
 * the light's side is out of it, a crescent under the rim. The part of the rim facing the light (r . l > 0) casts it: its
 * shadow edge is where the ray from the light past each rim point meets the wall again, r - 2 (r . l) l, l the way from r
 * to the light. One closed path, rim arc out and shadow edge back, and the screen line its gradient runs along, dark at
 * the rim. */
export function rimShade(fr, cx, cy, R) {
  const L = bowlLightDir(fr), { f1, f2 } = fr.cut, [cut, ct] = cutCircle(fr);
  // the arc of the rim that faces the light: about the way to it from the middle, out to where each rim point's own
  // way to the light grazes it (so the shadow edge leaves the rim there), found by bisection
  const psi0 = Math.atan2(dot(f2, L), dot(f1, L)), faces = (p) => { const u = cut(p); return dot(u, bowlLightDir(fr, u)) > 0; };
  if (!faces(psi0) || faces(psi0 + Math.PI)) return null;
  const reach = (dir) => {
    let lo = 0, hi = Math.PI;
    for (let i = 0; i < 40; i++) { const mid = (lo + hi) / 2; if (faces(psi0 + dir * mid)) lo = mid; else hi = mid; }
    return lo;
  };
  const a0 = reach(-1), a1 = reach(1), mid = psi0 + (a1 - a0) / 2, del = (a0 + a1) / 2;
  const fold = (u) => { const l = bowlLightDir(fr, u); return comb(1, u, -2 * Math.max(0, dot(u, l)), l); };
  // where the fold lands on wall the near wall hides (orbited round the side, a low cut), the edge runs along the rim
  // instead, on the wall seen just inside it, so the shade never spills over the outside
  const { cx: rx, cy: ry } = fr.rim;
  const edge = (p) => {
    const w = fold(cut(p)), s = bowlProject(fr, w), rn = rimNorm(fr, s);
    if (rn <= 0.999) return w;
    return bowlSurfaceAt(fr, rx + ((s.x - rx) * 0.999) / rn, ry + ((s.y - ry) * 0.999) / rn) ?? w;
  };
  const edgeT = (p) => comb(5e3, edge(p + 1e-4), -5e3, edge(p - 1e-4));
  const cmds = [startAt(fr, cx, cy, R, cut(mid - del))];
  bowlArc(cmds, fr, cx, cy, R, cut, ct, mid - del, mid + del);
  bowlArc(cmds, fr, cx, cy, R, edge, edgeT, mid + del, mid - del);
  cmds.push(["Z"]);
  const from = bowlProject(fr, cut(mid)), to = bowlProject(fr, edge(mid));
  return { cmds, angle: angleOf(to.x - from.x, to.y - from.y) };
}
/** Whether a point of the inner surface is out of the light: it faces away from it, or the wall is in the way. */
export function inRimShade(fr, u) {
  const l = bowlLightDir(fr, u), uL = dot(u, l);
  if (uL >= 0) return true;
  return bowlKept(fr, comb(1, u, -2 * uL, l));
}
/** How brightly the light falls on the inner surface at u: the cosine of its angle to the wall's inward normal over
 * the square of its distance (1 at the sphere's point nearest the light), none in the lip's shade. */
export function bowlLitAt(fr, u) {
  const Lp = bowlLightAt(fr), v = comb(1, Lp, -1, u), dist = len(v), cos = -dot(u, v) / dist;
  if (cos <= 0 || inRimShade(fr, u)) return 0;
  return cos * ((len(Lp) - 1) / dist) ** 2;
}

/** The inside's depth: BOWL_DEPTH faint caps of latitude round the bottom, nested, up to just under the rim's lowest
 * point, each darkening what it holds by BOWL.deep (a few of 255), so the inside darkens smoothly toward the bottom (the
 * light falls off with distance there, and the walls hide more of the opening) and no step between two caps shows. 16 of
 * them, not 4 at 14 of 255: those steps read as topographic rings. Polar angles, outermost first. */
export const BOWL_DEPTH = 18;
export function bowlCaps(fr) {
  if (!fr.caps) {
    const low = Math.min(...Array.from({ length: 72 }, (_, j) => rimTheta(fr, (2 * Math.PI * j) / 72)));
    const top = Math.min(low - 0.03, (80 * Math.PI) / 180);
    fr.caps = Array.from({ length: BOWL_DEPTH }, (_, i) => (top * (BOWL_DEPTH - i)) / BOWL_DEPTH);
  }
  return fr.caps;
}
export const bowlDepthAt = (fr, u) => bowlCaps(fr).filter((t) => dot(u, fr.pole) >= Math.cos(t) - 1e-12).length;
/** Faint guide rings of latitude round the bottom (every 15 degrees, a hollow's contour lines), only those whole below
 * the rim's lowest point: a camera orbited far round the side cuts the near wall low. They are strokes, not steps. */
export const BOWL_RINGS = [1, 2, 3, 4].map((i) => (i * Math.PI) / 12);
export function bowlRings(fr) {
  if (!fr.rings) {
    const low = Math.min(...Array.from({ length: 72 }, (_, j) => rimTheta(fr, (2 * Math.PI * j) / 72)));
    fr.rings = BOWL_RINGS.filter((t) => t < low - 0.03);
  }
  return fr.rings;
}

/** The plumb line from you (`at`, up the axis) down the bowl's axis to its bottom (the pole), on screen in radii from
 * the middle, and the small ring it stands in at the bottom (a circle of radius `r` round the pole, in perspective):
 * {from, to, foot: {cx, cy, a, b}}. What shows that you float off the inside, not on it. */
export function bowlPlumb(fr, at, r = 0.05) {
  const from = bowlProject(fr, at), to = bowlProject(fr, fr.pole);
  const foot = projectedCircle(fr.d, comb(1 - 1e-3, fr.pole), fr.e1, fr.e2, r);
  return { from, to, foot };
}

// A linear gradient's angle (degrees, 0 up, clockwise, as GPUI takes it) for a screen direction, and where a point
// falls along one over a path's bounds (0 at the start, 1 at the end, as CSS spans them): the lip's shade runs so.
const angleOf = (dx, dy) => (Math.atan2(dx, -dy) * 180) / Math.PI;
function boundsOf(cmds) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const c of cmds) for (let i = 1; i + 1 < c.length; i += 2) {
    x0 = Math.min(x0, c[i]); x1 = Math.max(x1, c[i]); y0 = Math.min(y0, c[i + 1]); y1 = Math.max(y1, c[i + 1]);
  }
  return { x0, y0, x1, y1 };
}
export function gradientAt(cmds, angle, x, y) {
  const b = boundsOf(cmds), a = (angle * Math.PI) / 180, dx = Math.sin(a), dy = -Math.cos(a);
  const len = Math.abs((b.x1 - b.x0) * dx) + Math.abs((b.y1 - b.y0) * dy) || 1;
  return Math.min(1, Math.max(0, 0.5 + ((x - (b.x0 + b.x1) / 2) * dx + (y - (b.y0 + b.y1) / 2) * dy) / len));
}

/** The inside's light: the rim's ellipse again and again, shrinking toward the lit spot (where the light falls
 * brightest, bowlLitAt), each adding a little alpha. Many faint layers, so no step between two of them shows: {x, y, a,
 * b, s}, a conjugate ellipse each (s: its scale of the rim). Half the light comes from one linear gradient over the rim
 * toward the spot (bowlSweep, no steps at all). */
export const BOWL_GLOW = 16;
/** The lit spot: the point of the inner surface, seen well inside the rim, that the light falls on brightest; its place
 * on screen. */
export function bowlSpot(fr) {
  if (!fr.spot) {
    let best = { lit: -1, u: fr.pole };
    for (let i = 0; i <= 18; i++) for (let j = 0; j < 36; j++) {
      const phi = (2 * Math.PI * j) / 36, u = bowlPoint(fr, (rimTheta(fr, phi) * i) / 18, phi);
      if (bowlSeen(fr, u) > 0.85) continue;
      const lit = bowlLitAt(fr, u);
      if (lit > best.lit) best = { lit, u };
    }
    const s = bowlProject(fr, best.u);
    fr.spot = { x: s.x, y: s.y, u: best.u };
  }
  return fr.spot;
}
/** The inside's sweep of light: a linear gradient over the rim's ellipse, clear on the side away from the lit spot and
 * BOWL.sweep of BOWL_LIT at the side toward it. Its angle (GPUI's: 0 up, clockwise), and where a screen point (radii
 * from the middle) falls along it, 0 to 1 over the rim's bounds as CSS spans a gradient. */
export function bowlSweep(fr) {
  const { cx: mx, cy: my, w, h } = fr.rim, spot = bowlSpot(fr);
  const dx = spot.x - mx, dy = spot.y - my, l = Math.hypot(dx, dy) || 1, ux = dx / l, uy = dy / l;
  return { angle: angleOf(ux, uy), at: (x, y) => Math.min(1, Math.max(0, 0.5 + ((x - mx) * ux + (y - my) * uy) / (2 * (w * Math.abs(ux) + h * Math.abs(uy))))) };
}
export function bowlGlow(fr, cx, cy, R) {
  const { cx: mx, cy: my, a, b } = fr.rim, spot = bowlSpot(fr);
  return Array.from({ length: BOWL_GLOW }, (_, i) => {
    const s = 1 - (i / BOWL_GLOW) * 0.88;
    return { x: cx + R * (spot.x + s * (mx - spot.x)), y: cy + R * (spot.y + s * (my - spot.y)),
      a: { x: R * s * a.x, y: R * s * a.y }, b: { x: R * s * b.x, y: R * s * b.y }, s };
  });
}

/** The shadow you (a ball of radius rb, in sphere radii, at `at`: depth.js bowlYou, out from the middle toward the
 * opening; the middle by default) cast on the inner surface, from the one light: where its ray through you leaves the
 * sphere (bowlCast), a soft ellipse there (ballShadow: YOU_SHADOW.size times your radius, grown with the distance and
 * longer where the light comes in low), seen in perspective, in as many layers as its size on screen asks for
 * (shadowSteps). Its layers, outermost first: {x, y, a, b, alpha}, a conjugate ellipse each; none where the ray leaves
 * the sphere outside the opening. */
/** Your shadow's darkest (in its core), penumbra and size (of your radius): dark and large enough to be seen on the lit
 * wall, the clearest sign that you float in front of it. */
export const YOU_SHADOW = { peak: 0.55, soft: 0.5, size: 1 };
/** Where the light's ray through `at` (inside the sphere) meets the inner surface, away from the light. */
export const bowlShadowAt = (fr, at = ORIGIN) => bowlCast(fr, at);
/** Whether your shadow lands on the inside, seen through the opening. */
export const bowlShadowSeen = (fr, at) => { const u = bowlCast(fr, at); return bowlKept(fr, u) && bowlSeen(fr, u) <= 1; };
export function bowlShadow(fr, cx, cy, R, rb, at) {
  if (!bowlShadowSeen(fr, at)) return [];
  const c = bowlCast(fr, at), { A, B } = ballShadow(bowlLightAt(fr), at, YOU_SHADOW.size * rb, c, c);
  const e1 = projectedCircle(fr.d, c, A, B, 1), px = R * Math.sqrt(Math.hypot(e1.a.x, e1.a.y) * Math.hypot(e1.b.x, e1.b.y));
  // solved over the wall where it falls (its light and depth, no host's wash)
  const wall = bowlColorAt(fr, e1.cx, e1.cy, null);
  return shadowLayers(YOU_SHADOW.peak, YOU_SHADOW.soft, shadowSteps(YOU_SHADOW.peak, YOU_SHADOW.soft, px), wall).map((l) => {
    const e = projectedCircle(fr.d, c, comb(l.s, A), comb(l.s, B), 1);
    return { x: cx + R * e.cx, y: cy + R * e.cy, a: { x: R * e.a.x, y: R * e.a.y }, b: { x: R * e.b.x, y: R * e.b.y }, alpha: l.alpha };
  });
}

/** The floor the bowl stands over: the plane BOWL_FLOOR radii below its middle, square to its axis, in the bowl's own
 * world (so it turns with the bowl as the camera orbits, and the light that lights the inside lights it). Its grid on
 * screen (radii from the middle; `n` + 1 lines each way, `half` radii either side of the foot of the axis, cut off where
 * they would come near the camera), where the axis meets it, and the bowl's shadow on it: the sphere's (its outer wall),
 * where the light's rays past it meet the floor, as a conjugate ellipse on screen. */
export const BOWL_FLOOR = 1.45;
export function bowlFloor(fr, n = 10, half = 2.4) {
  const foot = comb(BOWL_FLOOR, fr.pole), zMax = fr.d * 0.55, lines = [];
  const lerp = (p, q, t) => comb(1 - t, p, t, q);
  for (let i = 0; i <= n; i++) {
    const t = -half + (2 * half * i) / n;
    for (const [a, b] of [[comb(1, foot, t, fr.e1, -half, fr.e2), comb(1, foot, t, fr.e1, half, fr.e2)],
      [comb(1, foot, -half, fr.e1, t, fr.e2), comb(1, foot, half, fr.e1, t, fr.e2)]]) {
      if (a.z > zMax && b.z > zMax) continue;
      const p = a.z > zMax ? lerp(a, b, (a.z - zMax) / (a.z - b.z)) : a, q = b.z > zMax ? lerp(b, a, (b.z - zMax) / (b.z - a.z)) : b;
      const s0 = bowlProject(fr, p), s1 = bowlProject(fr, q);
      lines.push({ x0: s0.x, y0: s0.y, x1: s1.x, y1: s1.y, edge: i === 0 || i === n });
    }
  }
  const Lp = bowlLightAt(fr), ray = unit(comb(-1, Lp)), along = dot(ray, fr.pole);
  const c = comb(1, Lp, (BOWL_FLOOR - dot(Lp, fr.pole)) / along, ray);
  const sh = ballShadow(Lp, ORIGIN, fr.outer, c, comb(-1, fr.pole));
  // orbited far round, the light comes in low and the shadow runs long toward the camera: none where any of it comes
  // near the camera (its image would not be an ellipse)
  const near = [1, -1].some((i) => [1, -1].some((j) => comb(1, c, i, sh.A, j, sh.B).z > zMax));
  const e = near ? null : projectedCircle(fr.d, c, sh.A, sh.B, 1);
  const ok = e && [e.cx, e.cy, e.a.x, e.a.y, e.b.x, e.b.y].every(Number.isFinite);
  return { lines, foot, top: comb(fr.outer, fr.pole), shadow: ok ? { x: e.cx, y: e.cy, a: e.a, b: e.b, at: c, h: dot(fr.pole, c) } : null };
}

/** The bowl's look: the inside's base (opaque, so the floor does not show through the bowl), each glow layer's alpha (2
 * of 255, so the step at a layer's edge is under one and a half of the 255 color levels between the base and the light,
 * and does not show), the sweep's, a host's wash over it (strong enough that two hosts read apart, light enough that
 * edges over it read), each depth cap's (5 of 255), a host's outline, the lip's shade under it at the rim and at its edge. */
export const BOWL = { glow: 2 / 255, sweep: 0.46, wash: 0.17, deep: 6 / 255, outline: 0.75, shade: 0.45, shadeEdge: 0 };
const BOWL_BASE = "#252c38", BOWL_LIT = "#e6ebf0", BOWL_SHADE = "#01040a";
/** What the eye sees at a point inside the bowl (nx, ny: radii from its middle), in a host's tint: the layers the
 * scene draws there, blended in order. */
export function bowlColorAt(fr, nx, ny, tint, under = "#1d2a3c") {
  if (rimNorm(fr, { x: nx, y: ny }) > 1) return under;
  let out = BOWL_BASE;
  const m = fr.rim.m;
  for (const e of bowlGlow(fr, 0, 0, 1)) {
    const x = nx - e.x, y = ny - e.y;
    if (m[0] * x * x + 2 * m[1] * x * y + m[2] * y * y <= e.s * e.s) out = over(out, withAlpha(BOWL_LIT, BOWL.glow));
  }
  out = over(out, withAlpha(BOWL_LIT, BOWL.sweep * bowlSweep(fr).at(nx, ny)));
  if (tint) out = over(out, withAlpha(tint, BOWL.wash));
  const u = bowlSurfaceAt(fr, nx, ny), sh = rimShade(fr, 0, 0, 1);
  if (u) for (let i = bowlDepthAt(fr, u); i > 0; i--) out = over(out, withAlpha(BOWL_SHADE, BOWL.deep));
  if (u && sh && inRimShade(fr, u)) {
    const t = gradientAt(sh.cmds, sh.angle, nx, ny);
    out = over(out, withAlpha(BOWL_SHADE, BOWL.shade + (BOWL.shadeEdge - BOWL.shade) * t));
  }
  return out;
}
export { BOWL_BASE, BOWL_LIT, BOWL_SHADE };

// ---- you, in the bowl: a small glass ball
//
// You float in front of the bowl's inside, so you are drawn as a thing in front of it, not a ring on it: a glass ball lit
// by the bowl's one light from where it hangs as seen from you (bowlLightDir: up and a little left at the opening view). Its body is clear glass, a linear gradient from nearly
// clear on the lit side to a deeper tint at the far limb (no steps at all), with the wall showing through; the limb
// darkens in three soft strokes (glass is most reflective at a grazing angle); YOU_GLASS circles shrinking toward the
// lit point each add a little light, so it rounds smoothly (each step under 2 percent, about 3 of 255 levels: no band shows);
// then a specular highlight where the light strikes, the light the glass gathers glowing low on the far side inside
// the limb, a limb stroked light to dark, and a dark hairline outside it, so it stands off the wall behind it. About
// 92 path commands (YOU_GLASS_CMDS).
export const YOU_GLASS = 12;
export const YOU_GLASS_CMDS = 4 * (YOU_GLASS + 10) + 4;
const circle = (x, y, r) => [["M", x + r, y], ["A", r, r, 0, false, true, x - r, y], ["A", r, r, 0, false, true, x + r, y], ["Z"]];
/** The glass ball's paths at (x, y), radius r, in `tint`, `op` its opacity (a faded you fades it), lit from `light` (the
 * camera-space way to the light from you): {t: "path"} items. */
export function glassBall(id, x, y, r, tint, op = 1, light = LIGHT) {
  const LIGHT = lightOf(light);
  const away = angleOf(-LIGHT.x, -LIGHT.y), fill = (color, a) => ({ kind: "solid", color: withAlpha(color, a * op) });
  const deep = mix(tint, "#050b17", 0.72), lit = mix(tint, "#ffffff", 0.62);
  const path = (part, cmds, bg, stroke = 0) => ({ t: "path", id: `you-glass-${part}-${id}`, fill: !stroke, ...(stroke ? { width: stroke } : {}), cmds, bg });
  const paths = [path("body", circle(x, y, r), { kind: "linear", angle: away, from: withAlpha(mix(tint, "#ffffff", 0.3), 0.06 * op), to: withAlpha(deep, 0.72 * op) })];
  // the limb: darker toward the edge, soft
  [[0.89, 0.22, 0.14], [0.94, 0.12, 0.2], [0.975, 0.05, 0.28]].forEach(([k, w, a], i) => paths.push(path(`limb${i}`, circle(x, y, k * r), fill(deep, a), Math.max(0.6, w * r))));
  // the roundness: homothetic about the lit point, so each lies inside the one before and inside the ball
  const lx = x + r * LIGHT.x * 0.5, ly = y + r * LIGHT.y * 0.5, step = 1 - (1 - 0.18) ** (1 / YOU_GLASS);
  for (let i = 0; i < YOU_GLASS; i++) {
    const s = 1 - ((i + 1) / (YOU_GLASS + 1)) * 0.9;
    paths.push(path(`lit${i}`, circle(lx + s * (x - lx), ly + s * (y - ly), s * r), fill(lit, step)));
  }
  // the light the glass gathers, a bright crescent low on the far side just inside the limb (two arcs, soft and sharp)
  const th = Math.atan2(-LIGHT.y, -LIGHT.x);
  [[0.84, 0.13, 0.16, 0.95], [0.86, 0.045, 0.3, 0.6]].forEach(([k, w, a, half], i) => {
    const t0 = th - half, t1 = th + half, rr = k * r;
    paths.push(path(`glow${i}`, [["M", x + rr * Math.cos(t0), y + rr * Math.sin(t0)], ["A", rr, rr, 0, false, true, x + rr * Math.cos(t1), y + rr * Math.sin(t1)]],
      fill(lit, a), Math.max(0.8, w * r)));
  });
  // the specular highlight: soft round a bright core
  const hx = x + r * LIGHT.x * 0.52, hy = y + r * LIGHT.y * 0.52;
  [[0.26, 0.08], [0.16, 0.14], [0.09, 0.3], [0.045, 0.6]].forEach(([k, a], i) => paths.push(path(`spec${i}`, circle(hx, hy, k * r), fill("#ffffff", a))));
  paths.push(path("rim", circle(x, y, r - 0.7), { kind: "linear", angle: away, from: withAlpha("#ffffff", 0.65 * op), to: withAlpha(deep, 0.8 * op) }, 1.4));
  paths.push(path("edge", circle(x, y, r + 0.5), fill("#01040a", 0.55), 1));
  return paths;
}
