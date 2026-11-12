// Smooth, directional globe shading using paths supported by both GPUI and the SVG preview.
// A light contour is a projected small circle, clipped by the visible hemisphere.
// Short cubic arcs keep the smooth lighting without thousands of native line calls per globe.
//
// A globe is glass: the nested contours each add a little alpha, so the shadow side is nearly clear and the lit side a
// tinted veil, with the floor, stems and the globes behind showing through. Opacity is the only thing hover changes.
// The per-shoal view's cutout sphere (a bowl seen from above) reuses the same light, seen from inside, and every floor
// shadow falls from it too.
import { conjugateEllipseCmds, mix, withAlpha } from "./geom.js";

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
// zero the near wall's top shows from outside, as a thin crescent under the lip.
const CUT_MARGIN = (-9 * Math.PI) / 180;
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
export function bowlFrame(alpha, d = BOWL_DIST, beta = BOWL_TURN) {
  const sa = Math.sin(alpha), ca = Math.cos(alpha), sb = Math.sin(beta), cb = Math.cos(beta);
  const pole = { x: -ca * sb, y: sa, z: -ca * cb };
  const e1 = { x: cb, y: 0, z: -sb };
  const e2 = { x: sa * sb, y: ca, z: sa * cb };
  // The cut. A point of the unit sphere faces the camera when u.z > 1 / d, and a cut whose normal is `toward` off the
  // line of sight keeps none when acos(h) >= toward + acos(1 / d) (kappa, less a little: CUT_MARGIN). Its normal leans
  // `lean` from the bowl's up toward the camera, so the rim's polar angle runs from pi - tilt - kappa on the near side to
  // that plus 2 lean on the far side: the lean that keeps the far wall up to the equator (a hemisphere's rim) cuts away
  // only the near wall. A camera that already sees all of the hemisphere's inside keeps it whole.
  const tilt = Math.acos(Math.max(-1, Math.min(1, -pole.z))), kappa = Math.acos(1 / d) + CUT_MARGIN;
  const lean = Math.max(0, (tilt + kappa - Math.PI / 2) / 2), toward = tilt - lean;
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

/** The inside's shade from its own lip: the light comes in through the opening from above and the upper left
 * (bowlShadowLight), so the wall under the lip on the light's side is out of it, a crescent under the upper-left rim.
 * The part of the rim facing the light (r . L > 0) casts it: its shadow edge is that rim reflected through the light's
 * line, r - 2 (r . L) L. One closed path, rim arc out and shadow edge back, and the screen line its gradient runs
 * along, dark at the rim. */
export function rimShade(fr, cx, cy, R) {
  const L = bowlShadowLight(fr), { c, rho, f1, f2 } = fr.cut, [cut, ct] = cutCircle(fr);
  const A = Math.hypot(dot(f1, L), dot(f2, L)), psi0 = Math.atan2(dot(f2, L), dot(f1, L)), k = -dot(c, L) / (rho * A);
  if (!(k < 1 - 1e-6)) return null;
  const del = Math.acos(Math.max(-1 + 1e-6, k));
  const fold = (u) => comb(1, u, -2 * dot(u, L), L);
  const cmds = [startAt(fr, cx, cy, R, cut(psi0 - del))];
  bowlArc(cmds, fr, cx, cy, R, cut, ct, psi0 - del, psi0 + del);
  bowlArc(cmds, fr, cx, cy, R, (p) => fold(cut(p)), (p) => fold(ct(p)), psi0 + del, psi0 - del);
  cmds.push(["Z"]);
  const from = bowlProject(fr, cut(psi0)), to = bowlProject(fr, fold(cut(psi0)));
  return { cmds, angle: angleOf(to.x - from.x, to.y - from.y) };
}
/** Whether a point of the inner surface is out of the light: it faces away from it, or the wall is in the way. */
export function inRimShade(fr, u) {
  const L = bowlShadowLight(fr), uL = dot(u, L);
  if (uL >= 0) return true;
  return bowlKept(fr, comb(1, u, -2 * uL, L));
}

/** The inside's depth: rings of latitude round the bottom (polar angles from the pole, all below the rim's lowest
 * point), each the edge of a cap that darkens what is below it a little more, so the inside recedes to the bottom and
 * every step in the shading sits under a ring. A camera orbited far round the side cuts the near wall low, so only the
 * rings that stay whole there are drawn (bowlRings): at the presets, all four. */
export const BOWL_RINGS = [1, 2, 3, 4].map((i) => (i * Math.PI) / 12);
export function bowlRings(fr) {
  if (!fr.rings) {
    const low = Math.min(...Array.from({ length: 72 }, (_, j) => rimTheta(fr, (2 * Math.PI * j) / 72)));
    fr.rings = BOWL_RINGS.filter((t) => t < low - 0.03);
  }
  return fr.rings;
}
export const bowlDepthAt = (fr, u) => bowlRings(fr).filter((t) => dot(u, fr.pole) >= Math.cos(t)).length;

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

/** The inside's light: the rim's ellipse again and again, shrinking toward the spot that faces the light (lower right,
 * as a hollow is lit opposite to a ball), each adding a little alpha. Many faint layers, so no step between two of them
 * shows: {x, y, a, b, s}, a conjugate ellipse each (s: its scale of the rim). */
export const BOWL_GLOW = 44;
/** The bowl's light: LIGHT as it falls in the view the inside's shading was made for (28 degrees tilt, 30 turn), held in
 * the bowl's own frame, so the lit spot stays on the far wall and the bottom stays darkest as the camera orbits (lit
 * from the camera's fixed LIGHT, the bottom of the 2026-11-12 opening view faced it and read lighter than the walls). */
const LIGHT_REF = (() => {
  const sa = Math.sin(28 * Math.PI / 180), ca = Math.cos(28 * Math.PI / 180), sb = Math.sin(30 * Math.PI / 180), cb = Math.cos(30 * Math.PI / 180);
  const pole = { x: -ca * sb, y: sa, z: -ca * cb }, e1 = { x: cb, y: 0, z: -sb }, e2 = { x: sa * sb, y: ca, z: sa * cb };
  return [dot(LIGHT, pole), dot(LIGHT, e1), dot(LIGHT, e2)];
})();
export const bowlLight = (fr) => unit(comb(LIGHT_REF[0], fr.pole, LIGHT_REF[1], fr.e1, LIGHT_REF[2], fr.e2));
export function bowlGlow(fr, cx, cy, R) {
  const { cx: mx, cy: my, a, b } = fr.rim;
  // the inner point whose normal faces the light: u = -bowlLight, seen through the opening
  const Lb = bowlLight(fr), sp = bowlProject(fr, { x: -Lb.x, y: -Lb.y, z: -Lb.z });
  const spot = { x: mx + (sp.x - mx) * 0.92, y: my + (sp.y - my) * 0.92 };
  return Array.from({ length: BOWL_GLOW }, (_, i) => {
    const s = 1 - (i / BOWL_GLOW) * 0.88;
    return { x: cx + R * (spot.x + s * (mx - spot.x)), y: cy + R * (spot.y + s * (my - spot.y)),
      a: { x: R * s * a.x, y: R * s * a.y }, b: { x: R * s * b.x, y: R * s * b.y }, s };
  });
}

/** The shadow you (a ball of radius rb, in sphere radii, at `at`: depth.js bowlYou, up the axis from the middle; the
 * middle by default) cast on the inner surface: from above the opening and the upper left (bowlShadowLight: the bowl's
 * up and the one light, SHADOW_UP to 1, which the lip's shade falls from too), so it falls near the bottom, just down
 * and to the right of your foot on the axis, not on the lit far wall. It lands where the light's line through you
 * leaves the sphere, so the higher you float the further it falls from you on screen. It is a disc of radius rb there,
 * shrunk a little and soft (its penumbra wide), seen in perspective. Its layers, outermost first: {x, y, a, b, alpha},
 * a conjugate ellipse each. */
export const BOWL_SHADOW_STEPS = 10;
const SHADOW_UP = 1.6;
export const bowlShadowLight = (fr) => unit(comb(-SHADOW_UP, fr.pole, 1, LIGHT));
/** Where the light's line through `at` (inside the sphere) meets the inner surface, away from the light. */
export function bowlShadowAt(fr, at = { x: 0, y: 0, z: 0 }) {
  const L = bowlShadowLight(fr), pl = dot(at, L), t = pl + Math.sqrt(Math.max(0, pl * pl - dot(at, at) + 1));
  return comb(1, at, -t, L);
}
export function bowlShadow(fr, cx, cy, R, rb, at) {
  const L = bowlShadowLight(fr), t1 = unit({ x: L.y, y: -L.x, z: 0 });
  const t2 = { x: L.y * t1.z - L.z * t1.y, y: L.z * t1.x - L.x * t1.z, z: L.x * t1.y - L.y * t1.x };
  const c = bowlShadowAt(fr, at);
  return shadowLayers(0.4, 0.6, BOWL_SHADOW_STEPS).map((l) => {
    const e = projectedCircle(fr.d, c, t1, t2, 0.8 * rb * l.s);
    return { x: cx + R * e.cx, y: cy + R * e.cy, a: { x: R * e.a.x, y: R * e.a.y }, b: { x: R * e.b.x, y: R * e.b.y }, alpha: l.alpha };
  });
}

/** The bowl's look: the inside's base, each glow layer's alpha (2 of 255, so the step at a layer's edge is under one and
 * a half of the 255 color levels between the base and the light, and does not show), a host's wash over it (light, so
 * edges over it read) and how dark it gets at the bottom, a host's outline, the lip's shade under it at the rim and at
 * its edge. */
export const BOWL = { base: 0.12, glow: 2 / 255, wash: 0.08, deep: 0.055, outline: 0.5, shade: 0.18, shadeEdge: 0 };
const BOWL_BASE = "#16243a", BOWL_LIT = "#c9d8e6", BOWL_SHADE = "#01040a";
/** What the eye sees at a point inside the bowl (nx, ny: radii from its middle), in a host's tint: the layers the
 * scene draws there, blended in order. */
export function bowlColorAt(fr, nx, ny, tint, under = "#1d2a3c") {
  if (rimNorm(fr, { x: nx, y: ny }) > 1) return under;
  let out = over(under, withAlpha(BOWL_BASE, BOWL.base));
  const m = fr.rim.m;
  for (const e of bowlGlow(fr, 0, 0, 1)) {
    const x = nx - e.x, y = ny - e.y;
    if (m[0] * x * x + 2 * m[1] * x * y + m[2] * y * y <= e.s * e.s) out = over(out, withAlpha(BOWL_LIT, BOWL.glow));
  }
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
