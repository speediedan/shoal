// scene.js: the view model and layout as a display list in screen space. Pure: no Herdr or GPUI
// imports. main.js turns each item into a GPUI element (paths through window.paint_path, boxes and
// text as absolute divs) and the preview harness turns the same list into SVG, so the two agree.
//
// Items, in paint order:
//   {t: "path", id, fill, width, dash, cmds, bg}  bg: {kind: "solid", color} |
//       {kind: "linear", angle, from, to} | {kind: "slash", color, width, interval}
//   {t: "box", id, x, y, w, h, radius, bg, border, opacity, loops, transitions}
//   {t: "text", id, x, y, w, h, text, size, color, align, weight, frames, opacity}
//   {t: "spin", id, x, y, size, color, ms}
// A loop is {prop, from, to, period, easing}; a transition is {prop, duration, easing}.

import { PALETTE, PASTELS, edgeBuckets, incidentEdges, shortShoal, SHOAL_CARD, BOARD_CARD } from "./model.js";
import { motionPolicy, flowEdges } from "./motion.js";
import {
  sphereShade, glassColorAt, LIGHT, lightOf, luneCmds, latitudeCmds, meridianCmds, rimCmds as bowlRimCmds, bowlGlow, bowlShadow, bowlColorAt, BOWL,
  BOWL_BASE, BOWL_LIT, BOWL_SHADE, BOWL_LIP, bowlRings, bowlCaps, bowlPlumb, lipCmds, crescentCmds, rimShade,
  rimNorm, bowlSurfaceAt, bowlAzimuth, bowlRimAt, shadowLayers, shadowSteps, bowlCovers, blend8, rgb8, SHADOW_INK, SHADOW_UNDER,
  bowlSweep, glassBall,
} from "./sphere.js";
import {
  edgeCurve, curveCmds, arrowHead, polygonCmds, shapeCmds, circleCmds, ellipseCmds, conjugateEllipseCmds, ellipseArcCmds, annularSectorCmds,
  samplesBySpacing, pointAt, tangentAt, toScreen, boundsOf, fitTransform, withAlpha, mix, clamp, readableInk,
} from "./geom.js";

const P = PALETTE;
const solid = (color) => ({ kind: "solid", color });
// The depth canvas: a slate gradient, brighter than the flat view's near-black (2026-11-13: so the dark shadows of the
// globes, the bowl, you and the edges read against it), and what a depth-mode surface sits on, its middle.
export const DEPTH_BG = { from: "#3d4e67", to: "#1f3047" };
const DEPTH_UNDER = "#2e3f57";

/** A hover id for the pointer on a globe away from its roles, its middle and its name. */
export const GLOBE = "globe:";

/** A globe's name (depth mode): larger than a flat disc's, `gap` px clear above `top`, the highest thing drawn on the
 * globe (its outline, or a role's body and badge on its top), with the needs-you line between the name and the globe. */
export const GLOBE_TITLE = { size: 14, h: 19, needs: 11, needsH: 15, gap: 12 };
export function globeTitleAt(top, needsYou) {
  const T = GLOBE_TITLE, below = needsYou > 0 ? T.needsH : 0;
  const y = top - T.gap - below - T.h;
  return { y, needsY: y + T.h, bottom: top - T.gap };
}

/**
 * The shoals whose globe is in focus for a hover or pin id: the globe itself, its middle or its name, one of its roles
 * or their satellites, or an edge (both ends' shoals). Their globes draw a little more opaque.
 */
export function focusedShoals(vm, focus) {
  const out = new Set();
  if (typeof focus !== "string" || !focus) return out;
  for (const pre of [GLOBE, SHOAL_CARD, BOARD_CARD]) if (focus.startsWith(pre)) return out.add(focus.slice(pre.length));
  const add = (id) => { const s = vm.byId.get(id)?.shoal; if (s) out.add(s); };
  if (vm.byId.has(focus)) add(focus);
  else if (focus.includes("#") && vm.byId.has(focus.slice(0, focus.lastIndexOf("#")))) add(focus.slice(0, focus.lastIndexOf("#")));
  else {
    const e = vm.edges.find((x) => x.id === focus);
    if (e) { add(e.src); add(e.dst); }
  }
  return out;
}

/**
 * Depth mode: an edge over a globe or the bowl gets a dark casing under its core (a map road's), its core a little more
 * alpha and a lighter ink (its color toward white by EDGE_INK.lift), so it stands out against a pale surface and a dark
 * one alike (2026-11-13: the bowl's walls swallowed the plain edges). Faded edges (another node hovered) keep a faint one
 * and their color. {width, alpha: the casing's; coreAlpha, color: the core's}.
 */
export const EDGE_INK = { lift: 0.35, casing: 2.8, head: 2.2 };
export function edgeCasing(bk) {
  const faded = bk.tier === "fade";
  return { width: bk.core + EDGE_INK.casing, alpha: faded ? Math.min(0.55, bk.coreAlpha * 0.6) : Math.min(0.85, 0.4 + bk.coreAlpha * 0.5),
    coreAlpha: faded ? bk.coreAlpha : Math.min(1, bk.coreAlpha * 1.2 + 0.12), color: faded || !bk.color ? bk.color : mix(bk.color, "#ffffff", EDGE_INK.lift) };
}

/**
 * The edges' shadows, cast by the view's one light: on the floor in the global view, on the bowl's inner wall per shoal.
 * Each end's shadow is where the light's ray through that end meets the surface (depth.js shadowOf); the edge's shadow
 * is its curve carried there, each point moved as its ends' shadows are, between them. EDGE_SHADOW_STROKES strokes, from
 * `outW` wide down to the core's `coreW`, each adding an equal step of darkness, so it is darkest along the middle and
 * fades out to its edges with no step showing (two strokes, a soft one and a core, read as a dark line in a pale band);
 * `peak` is its darkness along the middle. The arrowheads' shadows filled. Fainter on the far floor than on the bowl's
 * wall, just behind the edges.
 */
export const EDGE_SHADOW = { floor: { outW: 7, coreW: 1.6, peak: 0.2, heads: 0.16 }, bowl: { outW: 6.5, coreW: 1.5, peak: 0.38, heads: 0.32 } };
export const EDGE_SHADOW_STROKES = 4;
/** An edge shadow's strokes, widest first: {width, alpha}, each alpha solved so the darkness steps up evenly. */
export function edgeShadowStrokes(tone, n = EDGE_SHADOW_STROKES) {
  const out = [];
  let below = 0;
  for (let i = 0; i < n; i++) {
    const total = (tone.peak * (i + 1)) / n, alpha = 1 - (1 - total) / (1 - below);
    out.push({ width: tone.outW + ((tone.coreW - tone.outW) * i) / (n - 1), alpha });
    below = total;
  }
  return out;
}
/** An edge's curve c carried by its ends' shadow offsets d0 (at its start) and d1 (at its end), screen px. */
export function shadowCurve(c, d0, d1) {
  const at = (p, t) => ({ x: p.x + d0.x + (d1.x - d0.x) * t, y: p.y + d0.y + (d1.y - d0.y) * t });
  return { p0: at(c.p0, 0), c1: at(c.c1, 1 / 3), c2: at(c.c2, 2 / 3), p3: at(c.p3, 1) };
}

/** A role's contact shadow on the bowl's wall: its geometric shadow `rx` by `ry` of the role's radius, `off` of it toward
 * where its shadow falls, its darkest `peak` (what the three 13 percent layers it was added up to) and penumbra `soft`. */
export const CONTACT = { rx: 1.15, ry: 0.8, off: 0.7, peak: 0.34, soft: 0.3 };

/** The glyph a marching dot shows at frame j: bright head, a short tail behind it. */
export const DOT_GLYPHS = ["●", "•", "·", " ", " "];
export function dotFrames(i, L) {
  return Array.from({ length: L }, (_, j) => DOT_GLYPHS[(((j - i) % L) + L) % L] ?? " ");
}

/** Node radius on screen: grows with zoom, within limits, so dense views stay legible. */
export const screenR = (r, k) => r * clamp(Math.sqrt(k), 0.7, 1.5);

/** Ellipsizes from the right to fit `maxW` px at a monospace `size` (about 0.6em per glyph). */
export function fitText(text, maxW, size) {
  const s = String(text ?? "");
  const n = Math.max(2, Math.floor(maxW / (size * 0.6)));
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}
const textW = (s, size) => String(s).length * size * 0.6;

/** An edge's arrowheads: at its destination (the default), at its source, at both ends (a merged pair carrying
 * traffic both ways), or none. */
function headsAt(e, c, size, sizeAt = null) {
  const back = { p0: c.p3, c1: c.c2, c2: c.c1, p3: c.p0 };
  const at = e.heads ?? "dst";
  const dst = sizeAt ? size * sizeAt(e.dst) : size, src = sizeAt ? size * sizeAt(e.src) : size;
  return at === "both" ? [arrowHead(c, dst), arrowHead(back, src)] : at === "src" ? [arrowHead(back, src)]
    : at === "none" ? [] : [arrowHead(c, dst)];
}
const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** The host lune of the bowl seen at offsets (nx, ny) in radii, or null outside the rim: the inner surface's point
 * there, by its azimuth. */
export function bowlHostAt(fr, bands, nx, ny) {
  const u = bowlSurfaceAt(fr, nx, ny);
  if (!u) return null;
  const phi = bowlAzimuth(fr, u);
  const TAU = 2 * Math.PI;
  return bands.find((b) => ((phi - b.a0) % TAU + TAU) % TAU <= b.a1 - b.a0) ?? null;
}

/**
 * The color under a screen point in depth mode, or null off every surface: the nearest globe's glass there (as
 * sphere.js blends it), or the bowl's inside in that host's tint. `surfaces` is what buildScene collected.
 */
export function surfaceUnder(surfaces, x, y) {
  for (let i = surfaces.length - 1; i >= 0; i--) {
    const s = surfaces[i], nx = (x - s.x) / s.r, ny = (y - s.y) / s.r;
    if (s.kind === "globe" && Math.hypot(nx, ny) <= 1) return glassColorAt(s.tint, nx, ny, { fog: s.fog, focused: s.focused, under: DEPTH_UNDER, light: s.light });
    if (s.kind === "bowl" && rimNorm(s.frame, { x: nx, y: ny }) <= 1) {
      const b = bowlHostAt(s.frame, s.bands, nx, ny);
      return bowlColorAt(s.frame, nx, ny, b ? b.color ?? PASTELS[(b.index + 2) % PASTELS.length] : null, DEPTH_UNDER);
    }
  }
  return null;
}

// In the bowl a node's perspective scale is about BOWL_UNIT where a role sits mid-wall (depth.js BOWL_GAIN), more at
// you (about 1.9) and less on the far wall (about 0.75).
const BOWL_UNIT = 1.25;
/** An arrowhead's size in the bowl, of its bucket's, at a node of perspective scale s: in step with how near it is (a
 * square root undersold it: the far wall's heads were still nearly full size). */
export const headScaleOf = (s) => clamp((s / BOWL_UNIT) ** 0.9, 0.55, 1.35);
/**
 * How an edge from you tapers with depth, from its source (scale s0) to its destination (s1): {w0, w1} its width at each
 * end (of its bucket's core) and {a0, a1} its alpha (of its core's): wide and bright at its near end (at you,
 * TAPER.near times its core), and at its far end (on the wall) narrower and dimmer by the two ends' perspective scales,
 * more steeply than perspective alone (TAPER.w and TAPER.a), so it reads as running away from you, into the bowl.
 */
export const TAPER = { near: 2.2, w: 1.8, a: 1.2, alphaNear: 1.15, minW: 0.45, minA: 0.45 };
export function taperOf(s0, s1) {
  const near = Math.max(s0, s1, 1e-6), k = (s) => s / near;
  const w = (s) => Math.max(TAPER.minW, TAPER.near * k(s) ** TAPER.w), a = (s) => Math.max(TAPER.minA, TAPER.alphaNear * k(s) ** TAPER.a);
  return { w0: w(s0), w1: w(s1), a0: a(s0), a1: a(s1) };
}
/** A filled ribbon along cubic c, w0 wide at its start and w1 at its end: each side the curve's control points moved
 * out along the normals at its ends (a good offset for a gently bent edge), out on one side and back on the other. */
export function ribbonCmds(c, w0, w1) {
  const t0 = tangentAt(c, 0), t1 = tangentAt(c, 1), n0 = { x: -t0.y, y: t0.x }, n1 = { x: -t1.y, y: t1.x };
  const side = (k) => {
    const off = (p, n, w) => ({ x: p.x + (k * n.x * w) / 2, y: p.y + (k * n.y * w) / 2 });
    return [off(c.p0, n0, w0), off(c.c1, n0, w0 + (w1 - w0) / 3), off(c.c2, n1, w0 + (2 * (w1 - w0)) / 3), off(c.p3, n1, w1)];
  };
  const [a0, a1, a2, a3] = side(1), [b0, b1, b2, b3] = side(-1);
  return [["M", a0.x, a0.y], ["C", a1.x, a1.y, a2.x, a2.y, a3.x, a3.y], ["L", b3.x, b3.y], ["C", b2.x, b2.y, b1.x, b1.y, b0.x, b0.y], ["Z"]];
}

/** Pulse glyphs for stepped (frames) pulses: a blink for needs-you, a slow half-blink for work. */
export const PULSE_GLYPH = "◯";
export const pulseFrames = (kind) => (kind === "needs" ? [PULSE_GLYPH, " "] : [PULSE_GLYPH, PULSE_GLYPH, " ", " "]);

/**
 * Builds the display list. `opts`: {width, height, tf, hover, selected, policy (motion.js) or
 * motion (a tier name), packets}; `hover` is a node or an edge id. Returns {items, hits, edgeHits,
 * discs, nodeScreen, edgeScreen, animated, extent}:
 * `animated` counts elements that make the window redraw on their own (loops, frames, spins,
 * transitions); `extent` {x0, y0, x1, y1} is the screen box of everything drawn for the graph
 * (regions and their labels, edges, nodes and halos, satellites, labels, chips), which `fitScene`
 * keeps inside the canvas.
 */
export function buildScene(vm, lay, opts) {
  const { width, height, tf } = opts;
  const policy = opts.policy ?? motionPolicy(opts.motion ?? "full");
  const animate = policy.any;
  const items = [];
  const k = tf.k;
  const S = (p) => toScreen(tf, p);
  const posOf = (id) => lay.pos.get(id);
  const hover = opts.hover && vm.byId.has(opts.hover) ? opts.hover : null;
  // a hovered edge keeps itself and its two ends bright, as a hovered node keeps its neighbors
  const edgeFocus = opts.hover ?? opts.selected;
  const hoverEdge = !hover && edgeFocus ? vm.edges.find((e) => e.id === edgeFocus) ?? null : null;
  const bright = hover ? new Set([hover, ...(vm.neighbors.get(hover) ?? [])]) : hoverEdge ? new Set([hoverEdge.src, hoverEdge.dst]) : null;
  const dimOf = (id) => (bright && !bright.has(id) ? 0.2 : 1);
  const extent = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
  const grow = (x0, y0, x1, y1) => {
    if (![x0, y0, x1, y1].every(Number.isFinite)) return;
    extent.x0 = Math.min(extent.x0, x0); extent.y0 = Math.min(extent.y0, y0);
    extent.x1 = Math.max(extent.x1, x1); extent.y1 = Math.max(extent.y1, y1);
  };
  const growRect = (r) => grow(r.x, r.y, r.x + r.w, r.y + r.h);
  const growCentered = (cx, y, text, size, h) => { const w = textW(text, size); grow(cx - w / 2, y, cx + w / 2, y + h); };

  // 1. canvas: near-black violet to navy, a cool wash, and a soft bloom behind the hub
  const rect = [["M", 0, 0], ["L", width, 0], ["L", width, height], ["L", 0, height], ["Z"]];
  items.push({ t: "path", id: "bg", fill: true, cmds: rect, bg: { kind: "linear", angle: 165, from: lay.cam ? DEPTH_BG.from : P.bgTop, to: lay.cam ? DEPTH_BG.to : P.bgBottom } });
  items.push({ t: "path", id: "bg-wash", fill: true, cmds: rect, bg: { kind: "linear", angle: 35, from: withAlpha(P.navy, lay.cam ? 0.18 : 0.7), to: withAlpha(P.navy, 0) } });
  const hub = vm.nodes.find((n) => n.kind === "user");
  const bloomAt = hub ? S(posOf(hub.id)) : { x: width / 2, y: height / 2 };
  const bloomR = Math.max(width, height) * 0.42;
  [1, 0.72, 0.48, 0.28].forEach((f, i) => {
    items.push({ t: "path", id: `bloom-${i}`, fill: true, cmds: circleCmds(bloomAt.x, bloomAt.y, bloomR * f), bg: solid(withAlpha("#2a2148", 0.05 + i * 0.012)) });
  });

  // the edges' shadows: kept in their place in the paint order (on the floor, or on the bowl's inside, after its shading),
  // and filled in with the edges themselves (3.)
  const edgeShadow = {};
  const edgeShadowsHere = () => {
    if (!lay.shadowOf || edgeShadow.core) return;
    const tone = lay.bowl ? EDGE_SHADOW.bowl : EDGE_SHADOW.floor, ink = (a) => solid(withAlpha("#01040a", a));
    // the strokes share one list of commands: the edges' shadow curves, filled in below
    const cmds = [], strokes = edgeShadowStrokes(tone);
    edgeShadow.cmds = cmds;
    edgeShadow.strokes = strokes.map((st, i) => ({ t: "path", id: i === strokes.length - 1 ? "edge-shadow" : `edge-shadow-soft-${i}`, fill: false, width: st.width, cmds, bg: ink(st.alpha) }));
    edgeShadow.core = edgeShadow.strokes.at(-1);
    edgeShadow.heads = { t: "path", id: "edge-shadow-heads", fill: true, cmds: [], bg: ink(tone.heads) };
    items.push(...edgeShadow.strokes, edgeShadow.heads);
  };

  // 1b. depth mode: the floor grid, and stems from each plane and lifted node down to it, with their shadows
  if (lay.grid) {
    const lines = [], rim = [];
    for (const g of lay.grid) {
      const a = S({ x: g.x0, y: g.y0 }), b = S({ x: g.x1, y: g.y1 });
      (g.edge ? rim : lines).push(["M", a.x, a.y], ["L", b.x, b.y]);
    }
    if (lines.length) items.push({ t: "path", id: "floor-grid", fill: false, width: 1, cmds: lines, bg: solid(withAlpha(P.steel2, 0.11)) });
    if (rim.length) items.push({ t: "path", id: "floor-rim", fill: false, width: 1.2, cmds: rim, bg: solid(withAlpha(P.steel2, 0.2)) });
  }
  // A shadow is where the one light (depth.js castShadow) puts it: a globe's, a lifted seat's and yours under your stems,
  // each role's on a globe (depth.js casts) and the bowl's on its floor. Each is a stack of fine alpha layers (sphere.js
  // shadowLayers), as many as its size on screen asks for, so it falls off smoothly with no ring showing, each layer's
  // alpha solved over what is under its middle: the canvas and the shadows drawn before it there (a role's falls in its
  // globe's). The bowl's inner layers that its opaque inside covers whole are left out.
  const drawn = []; // the floor's shadow layers so far: {c, a, b, A}
  const floorUnder = (q) => {
    let col = rgb8(SHADOW_UNDER);
    for (const d of drawn) {
      const det = d.a.x * d.b.y - d.a.y * d.b.x, x = q.x - d.c.x, y = q.y - d.c.y;
      const u = (x * d.b.y - y * d.b.x) / det, v = (d.a.x * y - d.a.y * x) / det;
      if (u * u + v * v <= 1) col = blend8(col, rgb8(SHADOW_INK), d.A);
    }
    return "#" + col.map((v) => v.toString(16).padStart(2, "0")).join("");
  };
  const shadowStack = (id, sh) => {
    const c = S(sh), ax = { x: sh.a.x * k, y: sh.a.y * k }, bx = { x: sh.b.x * k, y: sh.b.y * k };
    const px = Math.sqrt(Math.hypot(ax.x, ax.y) * Math.hypot(bx.x, bx.y));
    const hidden = lay.bowl && lay.clusters[0] ? bowlCovers(lay.bowl.frame, S(lay.clusters[0]), lay.clusters[0].r * k) : null;
    const layers = shadowLayers(sh.peak, sh.soft, shadowSteps(sh.peak, sh.soft, px), floorUnder(c)), mine = [];
    for (const [i, l] of layers.entries()) {
      const a = { x: ax.x * l.s, y: ax.y * l.s }, b = { x: bx.x * l.s, y: bx.y * l.s };
      if (hidden && hidden(c, a, b)) break; // nested: every layer inside this one is covered too
      items.push({ t: "path", id: `shadow-${id}-${i}`, fill: true, cmds: ellipseArcCmds(c.x, c.y, a, b), bg: solid(withAlpha(SHADOW_INK, l.alpha)) });
      mine.push({ c, a, b, A: Math.round(l.alpha * 255) });
    }
    drawn.push(...mine);
  };
  for (const st of lay.stems ?? []) if (st.shadow) shadowStack(st.id, st.shadow);
  for (const cs of lay.casts ?? []) shadowStack(cs.id, cs.shadow);
  // each stem's own shadow, a thin pole's: from its foot, where it meets the floor, to its top's shadow (the caster's), so
  // every shadow on the floor is tied to what casts it (global view; the bowl's stem stands in front of its shadow)
  const poles = lay.bowl ? [] : (lay.stems ?? []).filter((st) => st.shadow).flatMap((st) => { const a = S({ x: st.x1, y: st.y1 }), b = S(st.shadow); return [["M", a.x, a.y], ["L", b.x, b.y]]; });
  if (poles.length) items.push({ t: "path", id: "stem-shadows", fill: false, width: 1.2, dash: [3, 4], cmds: poles, bg: solid(withAlpha("#01040a", 0.22)) });
  for (const st of lay.stems ?? []) {
    const a = S({ x: st.x0, y: st.y0 }), b = S({ x: st.x1, y: st.y1 });
    items.push({ t: "path", id: `stem-${st.id}`, fill: false, width: 1, dash: [3, 4], cmds: [["M", a.x, a.y], ["L", b.x, b.y]], bg: solid(withAlpha(P.steel2, 0.32)) });
  }
  if (!lay.bowl) edgeShadowsHere();

  // node radii on screen (depth mode: with perspective), wanted by the regions' titles and by the edges' trims
  const nodeR = new Map(vm.nodes.map((n) => [n.id, screenR(n.visual.r, k) * (lay.scaleOf?.get(n.id) ?? 1)]));

  // 2. regions: cluster discs (global) or host bands (per shoal); their titles block node labels
  const discs = [];
  const labelObstacles = [];
  // depth mode: what each label sits on, so its ink can be picked against it (globes far to near; the bowl)
  const surfaces = [];
  const inFocus = focusedShoals(vm, edgeFocus);
  if (vm.global) {
    for (const c of lay.clusters) {
      const p = S(c);
      const r = c.r * k;
      const ry = r * (c.ry ?? 1); // a tilted plane (depth mode): the disc is an ellipse, its slab edge drawn under it
      const ring = (dy = 0) => ellipseCmds(p.x, p.y + dy, r, ry);
      discs.push({ id: c.id, x: p.x, y: p.y, r, hole: r * 0.5, sphere: !!c.sphere });
      const fog = c.fog ?? 1;
      if (c.sphere) {
        // glass: much clearer with nothing on it in focus, a little more opaque when the pointer is on it
        // lit from the way to the one light from this globe (depth.js lightFrom), so its lit side and its shadow agree
        const focused = inFocus.has(c.id), glassFog = 0.72 + 0.28 * fog, gl = lightOf(c.light ?? LIGHT);
        const shade = sphereShade(c.id, p.x, p.y, r, c.tint, glassFog, focused, gl);
        surfaces.push({ kind: "globe", x: p.x, y: p.y, r, tint: c.tint, fog: glassFog, focused, light: gl });
        items.push({ t: "path", id: `sphere-body-${c.id}`, fill: true, cmds: circleCmds(p.x, p.y, r), bg: solid(shade.body) });
        items.push(...shade.paths);
        // Soft glancing reflection, rather than a single sharp white dot.
        // (8 layers, not 12: the same highlight, and room in the render budget for the soft floor shadows)
        for (let i = 0; i < 8; i++) {
          const sr = r * (0.17 - 0.018 * i);
          items.push({ t: "path", id: `sphere-spec-${c.id}-${i}`, fill: true, cmds: ellipseCmds(p.x + r * gl.x, p.y + r * gl.y, sr, sr * 0.7), bg: solid(withAlpha("#ffffff", (focused ? 0.05 : 0.038) * fog)) });
        }
        // the far side's grid shows through the glass
        for (const front of [false, true]) {
          const cmds = (c.globeGrid ?? []).filter((g) => g.front === front).flatMap((g) => g.cmds.map(([op, ...xy]) => [op, ...xy.map((v, i) => (i % 2 ? v * k + tf.ty : v * k + tf.tx))]));
          items.push({ t: "path", id: `sphere-grid-${c.id}-${front}`, fill: false, width: front ? 0.9 : 0.6, dash: front ? null : [2, 5], cmds, bg: solid(withAlpha(front ? "#e4edf4" : c.tint, (front ? 0.19 : 0.12) * fog)) });
        }
        items.push({ t: "path", id: `sphere-limb-${c.id}`, fill: false, width: 1.5, cmds: circleCmds(p.x, p.y, r * 0.993), bg: { kind: "linear", angle: (Math.atan2(-gl.x, gl.y) * 180) / Math.PI, from: withAlpha("#ffffff", 0.4 * fog), to: withAlpha("#020814", focused ? 0.7 : 0.5) } });
        items.push({ t: "path", id: `sphere-rim-${c.id}`, fill: false, width: 0.8, cmds: circleCmds(p.x, p.y, r), bg: solid(withAlpha(c.tint, (focused ? 0.55 : 0.42) * fog)) });
      } else if (c.ry !== undefined) {
        // the slab's thickness: its near edge as a band between the top ellipse's lower half and the same one dropped
        const t = 11 * k * (c.s ?? 1);
        const edge = [["M", p.x - r, p.y], ["A", r, ry, 0, false, false, p.x + r, p.y], ["L", p.x + r, p.y + t],
          ["A", r, ry, 0, false, true, p.x - r, p.y + t], ["Z"]];
        items.push({ t: "path", id: `disc-slab-${c.id}`, fill: true, cmds: edge, bg: solid(withAlpha(c.tint, 0.2 * fog)) });
        items.push({ t: "path", id: `disc-slab-rim-${c.id}`, fill: false, width: 1, cmds: [["M", p.x - r, p.y + t], ["A", r, ry, 0, false, false, p.x + r, p.y + t]],
          bg: solid(withAlpha(c.tint, 0.35 * fog)) });
      }
      if (!c.sphere) {
        items.push({ t: "path", id: `disc-${c.id}`, fill: true, cmds: ring(), bg: solid(withAlpha(c.tint, hover ? 0.025 : c.ry !== undefined ? 0.11 * fog : 0.045)) });
        items.push({ t: "path", id: `disc-glow-${c.id}`, fill: false, width: 5, cmds: ring(), bg: solid(withAlpha(c.tint, 0.05)) });
      }
      if (!c.sphere) items.push({ t: "path", id: `disc-rim-${c.id}`, fill: false, width: c.ry !== undefined ? 1.4 : 1, cmds: ring(), bg: solid(withAlpha(c.tint, c.ry !== undefined ? 0.5 * fog : 0.22)) });
      const label = `${c.label.toUpperCase()}${vm.shoals?.find((s) => s.id === c.id)?.boards?.items?.length ? " ↗" : ""}`;
      // a globe's name is larger and sits clear above the globe and above any role (body and badge) on its top
      const top = c.sphere ? Math.min(p.y - r, ...vm.nodes.filter((n) => n.kind === "role" && n.shoal === c.id && posOf(n.id))
        .map((n) => S(posOf(n.id)).y - nodeR.get(n.id) - 9)) : null;
      const T = c.sphere ? { ...GLOBE_TITLE, ...globeTitleAt(top, c.needsYou) } : { size: 11, h: 16, needs: 9.5, y: p.y - ry - 22, needsY: p.y - ry - 8 };
      items.push({ t: "text", id: `disc-label-${c.id}`, x: p.x - 180, y: T.y, w: 360, h: T.h, text: label, size: T.size, color: withAlpha(c.tint, 0.95), align: "center", weight: "semibold" });
      grow(p.x - r - 3, p.y - ry - 3, p.x + r + 3, p.y + ry + 3 + (c.ry !== undefined ? 7 * k : 0));
      growCentered(p.x, T.y, label, T.size, T.h);
      labelObstacles.push({ x: p.x - textW(label, T.size) / 2, y: T.y, w: textW(label, T.size), h: c.needsYou > 0 ? T.needsY - T.y + 15 : T.h });
      discs.at(-1).title = labelObstacles.at(-1);
      if (c.needsYou > 0) {
        items.push({ t: "text", id: `disc-needs-${c.id}`, x: p.x - 180, y: T.needsY, w: 360, h: 13, text: `${c.needsYou} need${c.needsYou === 1 ? "s" : ""} you`, size: T.needs, color: P.magenta, align: "center" });
      }
    }
  } else if (lay.bowl && lay.clusters[0]) {
    // depth mode: the cutout sphere. You at its middle; each host a lune of its inner surface in the host's color
    // (seen from above and from the side in perspective: the rim a turned ellipse round the hosts' lunes, which tile
    // the whole inside, the bottom below and beside you)
    const c = lay.clusters[0], fr = lay.bowl.frame, rim = fr.rim;
    const o = S(c), R = c.r * k;
    const rc = { x: o.x + R * rim.cx, y: o.y + R * rim.cy }, rimCmds = bowlRimCmds(fr, o.x, o.y, R);
    surfaces.push({ kind: "bowl", x: o.x, y: o.y, r: R, frame: fr, bands: lay.bands });
    // the inside: an opaque base (the bowl hides the floor behind it), then light pooling where the bowl's light falls
    // brightest (sphere.js bowlSpot), in many faint layers over a smooth sweep, so no band shows
    items.push({ t: "path", id: "bowl-in", fill: true, cmds: rimCmds, bg: solid(BOWL_BASE) });
    bowlGlow(fr, o.x, o.y, R).forEach((e, i) => items.push({ t: "path", id: `bowl-glow-${i}`, fill: true, cmds: conjugateEllipseCmds(e.x, e.y, e.a, e.b), bg: solid(withAlpha(BOWL_LIT, BOWL.glow)) }));
    items.push({ t: "path", id: "bowl-sweep", fill: true, cmds: rimCmds, bg: { kind: "linear", angle: bowlSweep(fr).angle, from: withAlpha(BOWL_LIT, 0), to: withAlpha(BOWL_LIT, BOWL.sweep) } });
    const tintOf = (b) => b.color ?? PASTELS[(b.index + 2) % PASTELS.length];
    for (const b of lay.bands) {
      // a wash strong enough that two hosts read apart, light enough that an edge or an arrow over it still reads; the
      // outline carries the host's color
      const tint = tintOf(b), cmds = luneCmds(fr, o.x, o.y, R, b.a0, b.a1);
      items.push({ t: "path", id: `bowl-host-${b.host}`, fill: true, cmds, bg: solid(withAlpha(tint, BOWL.wash)) });
      items.push({ t: "path", id: `bowl-host-rim-${b.host}`, fill: false, width: 1.4, cmds, bg: solid(withAlpha(tint, BOWL.outline)) });
    }
    // depth: many faint caps of latitude round the bottom, nested, so the inside darkens smoothly toward it (no step
    // shows, sphere.js bowlCaps)
    bowlCaps(fr).forEach((t, i) => items.push({ t: "path", id: `bowl-deep-${i}`, fill: true, cmds: latitudeCmds(fr, o.x, o.y, R, t), bg: solid(withAlpha(BOWL_SHADE, BOWL.deep)) }));
    // the lip's shade on the inside: the wall under the lip on the light's side is out of the light
    const shade = rimShade(fr, o.x, o.y, R);
    if (shade) items.push({ t: "path", id: "bowl-shade", fill: true, cmds: shade.cmds, bg: { kind: "linear", angle: shade.angle, from: withAlpha(BOWL_SHADE, BOWL.shade), to: withAlpha(BOWL_SHADE, BOWL.shadeEdge) } });
    // faint guides: four rings of latitude round the bottom (what shows a hollow) and eight meridians down to it
    const yaw = lay.cam?.spin ?? lay.cam?.yaw ?? 0;
    items.push({ t: "path", id: "bowl-rings", fill: false, width: 0.8, bg: solid(withAlpha("#e4edf4", 0.1)), cmds: bowlRings(fr).flatMap((t) => latitudeCmds(fr, o.x, o.y, R, t)) });
    items.push({ t: "path", id: "bowl-grid", fill: false, width: 0.7, dash: [2, 5], bg: solid(withAlpha("#e4edf4", 0.12)),
      cmds: Array.from({ length: 8 }, (_, i) => meridianCmds(fr, o.x, o.y, R, yaw + (i * Math.PI) / 4)).flat() });
    // your shadow on the inside, where the bowl's light's ray through you meets the wall, and the edges' shadows (filled
    // in with the edges, below): on the surface, so over its shading and its guides
    const youAt = lay.bowl.you ?? { x: 0, y: 0, z: 0 };
    if (hub && nodeR.has(hub.id)) {
      bowlShadow(fr, o.x, o.y, R, nodeR.get(hub.id) / R, youAt).forEach((e, i) => items.push({
        t: "path", id: `bowl-shadow-${i}`, fill: true, cmds: ellipseArcCmds(e.x, e.y, e.a, e.b), bg: solid(withAlpha("#01040a", e.alpha)),
      }));
    }
    edgeShadowsHere();
    // the plumb line: from you down the axis to the bottom, where it stands in a small ring, so you read as floating in
    // front of the inside, not drawn on it (your body, drawn last, covers its top)
    if (hub && posOf(hub.id)) {
      const pl = bowlPlumb(fr, youAt), f = pl.foot;
      items.push({ t: "path", id: "bowl-plumb", fill: false, width: 1.1, dash: [3, 4], bg: solid(withAlpha("#e4edf4", 0.42)),
        cmds: [["M", o.x + R * pl.from.x, o.y + R * pl.from.y], ["L", o.x + R * pl.to.x, o.y + R * pl.to.y]] });
      items.push({ t: "path", id: "bowl-foot", fill: false, width: 1, bg: solid(withAlpha("#e4edf4", 0.5)),
        cmds: conjugateEllipseCmds(o.x + R * f.cx, o.y + R * f.cy, { x: R * f.a.x, y: R * f.a.y }, { x: R * f.b.x, y: R * f.b.y }) });
    }
    // the opening: the near wall's outside as a thin crescent under the lip, lit from the upper left, then the lip (the
    // wall's cut face, a lit band) with its inner edge (the rim) and its outer edge
    const shell = crescentCmds(fr, o.x, o.y, R);
    if (shell.length) {
      items.push({ t: "path", id: "bowl-shell", fill: true, cmds: shell, bg: { kind: "linear", angle: 110, from: withAlpha(mix(c.tint, "#ffffff", 0.25), 0.6), to: withAlpha("#050b17", 0.75) } });
      items.push({ t: "path", id: "bowl-limb", fill: false, width: 1, cmds: shell, bg: solid(withAlpha(c.tint, 0.4)) });
    }
    const lip = fr.lip, lipOut = conjugateEllipseCmds(o.x + R * lip.cx, o.y + R * lip.cy, { x: R * lip.a.x, y: R * lip.a.y }, { x: R * lip.b.x, y: R * lip.b.y });
    items.push({ t: "path", id: "bowl-lip-face", fill: true, cmds: lipCmds(fr, o.x, o.y, R), bg: { kind: "linear", angle: 135, from: withAlpha("#eef4fa", 0.3), to: withAlpha(mix(c.tint, "#c9d8e6", 0.5), 0.14) } });
    items.push({ t: "path", id: "bowl-lip", fill: false, width: 1.4, cmds: rimCmds, bg: { kind: "linear", angle: 135, from: withAlpha("#ffffff", 0.7), to: withAlpha(c.tint, 0.45) } });
    items.push({ t: "path", id: "bowl-lip-out", fill: false, width: 1, cmds: lipOut, bg: solid(withAlpha(mix(c.tint, "#050b17", 0.3), 0.55)) });
    grow(o.x + R * (lip.cx - lip.w), o.y + R * (lip.cy - lip.h), o.x + R * (lip.cx + lip.w), o.y + R * (lip.cy + lip.h));
    for (const cmd of shell) for (let i = 1; i + 1 < cmd.length; i += 2) grow(cmd[i], cmd[i + 1], cmd[i], cmd[i + 1]);
    for (const b of lay.bands) {
      // the host's name just outside the rim, out from the rim's middle through the lune's middle
      const tint = tintOf(b), am = (b.a0 + b.a1) / 2;
      const text = `${b.home ? "⌂ " : ""}${b.host}${b.jobsOnly ? " (jobs)" : ""}`;
      const e = bowlRimAt(fr, am), q = { x: o.x + R * e.x, y: o.y + R * e.y };
      const dx = q.x - rc.x, dy = q.y - rc.y, dl = Math.hypot(dx, dy) || 1, ux = dx / dl, uy = dy / dl;
      const out = 16 + R * BOWL_LIP + (textW(text, 10) / 2) * Math.abs(ux); // clear of the lip by the text's half width
      const lp = { x: q.x + out * ux, y: q.y + out * uy };
      items.push({ t: "text", id: `band-label-${b.host}`, x: lp.x - 70, y: lp.y - 7, w: 140, h: 14, text, size: 10, color: withAlpha(tint, 0.95), align: "center", weight: "semibold" });
      growCentered(lp.x, lp.y - 7, text, 10, 14);
      labelObstacles.push({ x: lp.x - textW(text, 10) / 2, y: lp.y - 7, w: textW(text, 10), h: 14 });
    }
    discs.push({ id: c.id, x: o.x, y: o.y, r: R, hole: R * 0.3 });
  } else {
    const c = lay.clusters[0];
    const o0 = S({ x: 0, y: 0 });
    for (const b of lay.bands) {
      // depth mode: each host's band is on its own plane, with its own center and squash
      const o = b.cx !== undefined ? S({ x: b.cx, y: b.cy }) : o0;
      const ry = b.ry ?? 1;
      const bs = b.s ?? 1; // perspective: a far host's ring is smaller
      const fogB = b.fog ?? 1;
      const R0 = b.r0 * bs, R1 = b.r1 * bs;
      const tint = b.color ?? PASTELS[(b.index + 2) % PASTELS.length];
      if (b.ry !== undefined) {
        for (let i = 3; i >= 1; i--) {  // the band's thickness: copies stepping down, the lowest darkest
          items.push({ t: "path", id: `band-slab-${b.host}-${i}`, fill: true, cmds: annularSectorCmds(o.x, o.y + i * 4 * k * bs, R0 * k, R1 * k, b.a0, b.a1, ry),
            bg: solid(withAlpha(tint, (0.05 + 0.03 * (3 - i)) * fogB)) });
        }
      }
      items.push({ t: "path", id: `band-${b.host}`, fill: true, cmds: annularSectorCmds(o.x, o.y, R0 * k, R1 * k, b.a0, b.a1, ry), bg: solid(withAlpha(tint, b.ry !== undefined ? 0.13 * fogB : 0.055)) });
      // a band that closes the whole ring has no ends to draw: its sector outline would leave a radial seam
      const closed = b.a1 - b.a0 >= Math.PI * 2 - 0.3;
      const rim = closed ? [...ellipseCmds(o.x, o.y, R0 * k, R0 * k * ry), ...ellipseCmds(o.x, o.y, R1 * k, R1 * k * ry)]
        : annularSectorCmds(o.x, o.y, R0 * k, R1 * k, b.a0, b.a1, ry);
      items.push({ t: "path", id: `band-rim-${b.host}`, fill: false, width: b.ry !== undefined ? 1.3 : 1, cmds: rim, bg: solid(withAlpha(tint, b.ry !== undefined ? 0.42 * fogB : 0.16)) });
      const am = (b.a0 + b.a1) / 2;
      const lp = { x: o.x + (R1 * k + 14) * Math.cos(am), y: o.y + (R1 * k + 14) * ry * Math.sin(am) };
      const bandText = `${b.home ? "\u2302 " : ""}${b.host}${b.jobsOnly ? " (jobs)" : ""}`;
      items.push({ t: "text", id: `band-label-${b.host}`, x: lp.x - 70, y: lp.y - 7, w: 140, h: 14, text: bandText, size: 10, color: withAlpha(tint, 0.9), align: "center", weight: "semibold" });
      growCentered(lp.x, lp.y - 7, bandText, 10, 14);
      for (let i = 0; i <= 16; i++) {
        const a = b.a0 + ((b.a1 - b.a0) * i) / 16;
        const q = { x: o.x + R1 * k * Math.cos(a), y: o.y + R1 * k * ry * Math.sin(a) };
        grow(q.x, q.y, q.x, q.y);
      }
    }
    // the donut hole: inside the innermost host band
    if (c) discs.push({ id: c.id, x: o0.x, y: o0.y, r: c.r * k, hole: lay.bands.length ? Math.min(...lay.bands.map((b) => b.r0)) * k : c.r * k * 0.5 });
  }

  // 2b. a role whose CI job runs on another host: a tether to that host's band (per shoal), or a
  // short chip at the node naming the host (global view, where hosts have no region)
  for (const t of lay.tethers ?? []) {
    if (!t.from) continue;
    const a = S(t.from);
    const color = vm.hostColor.get(t.host) ?? P.steel2;
    const dim = dimOf(t.id);
    const text = `${t.event} \u2192 ${t.host}`;
    if (t.to) {
      const b = S(t.to);
      items.push({ t: "path", id: `tether-${t.id}`, fill: false, width: 1, dash: [2, 3], cmds: [["M", a.x, a.y], ["L", b.x, b.y]], bg: solid(withAlpha(color, 0.6 * dim)) });
      items.push({ t: "path", id: `tether-end-${t.id}`, fill: true, cmds: circleCmds(b.x, b.y, 2.5), bg: solid(withAlpha(color, 0.9 * dim)) });
      const m = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const r = { x: m.x - 70, y: m.y - 14, w: 140, h: 13 };
      labelObstacles.push(r);
      grow(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.max(a.x, b.x), Math.max(a.y, b.y));
      growCentered(m.x, m.y - 14, text, 9, 13);
      items.push({ t: "text", id: `tether-label-${t.id}`, ...r, text, size: 9, color: withAlpha(color, 0.85 * dim), align: "center" });
    } else {
      const dir = Number.isFinite(t.dir) ? t.dir : 0;
      const len = 34 * clamp(Math.sqrt(k), 0.8, 1.3);
      const b = { x: a.x + len * Math.cos(dir), y: a.y + len * Math.sin(dir) + 8 };
      items.push({ t: "path", id: `tether-${t.id}`, fill: false, width: 1, dash: [2, 2], cmds: [["M", a.x, a.y], ["L", b.x, b.y]], bg: solid(withAlpha(color, 0.7 * dim)) });
      const w = textW(text, 9) + 18;
      const right = Math.cos(dir) >= 0;
      const r = { x: right ? b.x : b.x - w, y: b.y - 8, w, h: 16 };
      labelObstacles.push(r);
      growRect(r);
      items.push({ t: "box", id: `tether-chip-${t.id}`, ...r, radius: 8, bg: withAlpha(P.panelSolid, 0.85 * dim), border: { w: 1, color: withAlpha(color, 0.6 * dim) }, opacity: 1 });
      items.push({ t: "box", id: `tether-dot-${t.id}`, x: r.x + 6, y: r.y + 5, w: 6, h: 6, radius: 3, bg: withAlpha(color, dim), opacity: 1 });
      items.push({ t: "text", id: `tether-label-${t.id}`, x: r.x + 14, y: r.y + 1, w: w - 16, h: 14, text, size: 9, color: withAlpha(color, 0.95 * dim), align: "left" });
    }
  }

  // 3. edges, batched per color x tier bucket: a glow pass under a core pass, then arrowheads
  const curves = new Map();
  for (const e of vm.edges) {
    const a = posOf(e.src), b = posOf(e.dst);
    if (!a || !b) continue;
    const c = edgeCurve(S(a), S(b), { bend: e.bend, trimA: nodeR.get(e.src) + 2, trimB: nodeR.get(e.dst) + 3 });
    curves.set(e.id, c);
    for (const t of [0.25, 0.5, 0.75]) { const q = pointAt(c, t); grow(q.x, q.y, q.x, q.y); }
  }
  const edgeHits = [...curves].map(([id, c]) => ({ id, c }));
  const edgeScreen = new Map(edgeHits.map(({ id, c }) => [id, { ...pointAt(c, 0.5), r: 2 }]));
  const keep = hover ? incidentEdges(vm, hover) : hoverEdge ? new Set([hoverEdge.id]) : null;
  // in the bowl an arrowhead is sized in perspective with the node it is at: small on the far wall, large at you, so an
  // edge from you reads as pointing away, into the bowl
  const headScale = lay.bowl ? (id) => headScaleOf(lay.scaleOf?.get(id) ?? BOWL_UNIT) : null;
  // and an edge from you tapers with depth (taperOf): a ribbon wide and bright at you, thin and dim at the far wall
  const tapers = lay.bowl && hub ? (e) => (e.src === hub.id || e.dst === hub.id) && e.src !== e.dst : () => false;
  for (const bk of edgeBuckets(vm.edges.filter((e) => curves.has(e.id)), keep)) {
    const tapered = bk.dash ? [] : bk.edges.filter(tapers), plain = tapered.length ? bk.edges.filter((e) => !tapered.includes(e)) : bk.edges;
    const cmds = plain.flatMap((e) => curveCmds(curves.get(e.id)));
    const casing = lay.cam ? edgeCasing(bk) : null; // depth mode: a dark casing so the line reads over a surface
    const coreAlpha = casing ? casing.coreAlpha : bk.coreAlpha, ink = casing?.color ?? bk.color;
    // and its shadow, where the light puts it (not for faded edges, nor an alias's line)
    if (edgeShadow.core && bk.tier !== "fade" && bk.cls !== "alias") {
      for (const e of bk.edges) {
        const a = lay.shadowOf.get(e.src), b = lay.shadowOf.get(e.dst);
        if (!a || !b) continue;
        const sc = shadowCurve(curves.get(e.id), { x: a.dx * k, y: a.dy * k }, { x: b.dx * k, y: b.dy * k }), cc = curveCmds(sc);
        edgeShadow.cmds.push(...cc);
        edgeShadow.heads.cmds.push(...headsAt(e, sc, bk.arrow, headScale).flatMap((h) => polygonCmds(h)));
      }
    }
    if (cmds.length) {
      if (casing) items.push({ t: "path", id: `ek-${bk.key}`, fill: false, width: casing.width, dash: bk.dash, cmds, bg: solid(withAlpha(P.void, casing.alpha)) });
      if (bk.glow > 0) items.push({ t: "path", id: `eg-${bk.key}`, fill: false, width: bk.glow, cmds, bg: solid(withAlpha(ink, bk.glowAlpha)) });
      items.push({ t: "path", id: `ec-${bk.key}`, fill: false, width: bk.core, dash: bk.dash, cmds, bg: solid(withAlpha(ink, coreAlpha)) });
    }
    for (const e of tapered) {
      const c = curves.get(e.id), t = taperOf(lay.scaleOf?.get(e.src) ?? BOWL_UNIT, lay.scaleOf?.get(e.dst) ?? BOWL_UNIT);
      const angle = (Math.atan2(c.p3.x - c.p0.x, -(c.p3.y - c.p0.y)) * 180) / Math.PI;
      if (casing) items.push({ t: "path", id: `etk-${e.id}`, fill: true, cmds: ribbonCmds(c, bk.core * t.w0 + 2.4, bk.core * t.w1 + 1.6), bg: solid(withAlpha(P.void, casing.alpha)) });
      if (bk.glow > 0) items.push({ t: "path", id: `etg-${e.id}`, fill: true, cmds: ribbonCmds(c, bk.glow * t.w0, bk.glow * t.w1),
        bg: { kind: "linear", angle, from: withAlpha(ink, bk.glowAlpha * t.a0), to: withAlpha(ink, bk.glowAlpha * t.a1) } });
      items.push({ t: "path", id: `et-${e.id}`, fill: true, cmds: ribbonCmds(c, bk.core * t.w0, bk.core * t.w1),
        bg: { kind: "linear", angle, from: withAlpha(ink, Math.min(1, coreAlpha * t.a0)), to: withAlpha(ink, Math.min(1, coreAlpha * t.a1)) } });
    }
    if (bk.cls !== "alias") {
      const heads = bk.edges.flatMap((e) => headsAt(e, curves.get(e.id), bk.arrow, headScale).flatMap((h) => polygonCmds(h)));
      if (casing) items.push({ t: "path", id: `eak-${bk.key}`, fill: false, width: EDGE_INK.head, cmds: heads, bg: solid(withAlpha(P.void, casing.alpha)) });
      items.push({ t: "path", id: `ea-${bk.key}`, fill: true, cmds: heads, bg: solid(withAlpha(ink, Math.min(1, coreAlpha + 0.1))) });
    }
  }

  // 4. item satellites: spokes, then dots, batched by color
  const spokes = new Map();
  const satFill = new Map();
  const satRing = new Map(); // a next item that waits on something: a ring, so what can start stands out
  const satHits = [];
  const satScreen = new Map();
  const ownerOf = (s) => vm.byId.get(s.owner);
  for (const s of lay.sats) {
    const o = ownerOf(s);
    if (!o) continue;
    const dim = dimOf(o.id);
    const a = S({ x: s.ax, y: s.ay }), b = S(s);
    // a next item still blocked (its blocker as recorded, or only past its daily re-check) is a ring in the board's
    // blocked color: in the role's own tint it read as free work (2026-11-11). One whose blocker cleared is red: work due
    const cleared = s.stale && s.staleKind !== "expired";
    const ring = s.kind === "next" && s.blocker && !cleared;
    const color = s.kind === "active" ? withAlpha(o.visual.color, 0.95 * dim) : ring ? withAlpha(P.magenta, 0.9 * dim)
      : cleared ? withAlpha(P.red, 0.85 * dim) : withAlpha(o.tint, (s.kind === "next" ? 0.9 : 0.5) * dim);
    const sr = s.r * clamp(Math.sqrt(k), 0.7, 1.4);
    const into = ring ? satRing : satFill;
    // a ring's spoke stops at its rim
    const len = Math.hypot(b.x - a.x, b.y - a.y), cut = ring && len > sr ? (len - sr) / len : 1;
    const sc = withAlpha(o.tint, 0.5 * dim);
    if (!spokes.has(sc)) spokes.set(sc, []);
    spokes.get(sc).push(["M", a.x, a.y], ["L", a.x + (b.x - a.x) * cut, a.y + (b.y - a.y) * cut]);
    if (!into.has(color)) into.set(color, []);
    into.get(color).push(...circleCmds(b.x, b.y, sr));
    satHits.push({ id: s.id, x: b.x, y: b.y, r: Math.max(sr, 4) });
    satScreen.set(s.id, { x: b.x, y: b.y, r: sr });
    grow(b.x - sr, b.y - sr, b.x + sr, b.y + sr);
  }
  let si = 0;
  for (const [color, cmds] of spokes) items.push({ t: "path", id: `spoke-${si++}`, fill: false, width: 1.2, cmds, bg: solid(color) });
  si = 0;
  for (const [color, cmds] of satFill) items.push({ t: "path", id: `sat-${si++}`, fill: true, cmds, bg: solid(color) });
  si = 0;
  for (const [color, cmds] of satRing) items.push({ t: "path", id: `sat-ring-${si++}`, fill: false, width: 1.3, cmds, bg: solid(color) });

  // 5. flow: marching dots along hot and warm edges, motion native through .frames()
  // (full: every flowing edge at its own speed; balanced: the busiest few on one shared interval,
  // so every frames element wakes the window on the same tick; low and off: none)
  let animated = 0;
  if (policy.maxDots > 0) {
    let budget = policy.maxDots;
    const allowed = Number.isFinite(policy.topEdges) ? flowEdges(vm.edges, policy) : null;
    for (const e of vm.edges) {
      if (!e.flow || budget <= 0 || !curves.has(e.id)) continue;
      if (allowed && !allowed.has(e.id)) continue;
      if (keep && !keep.has(e.id)) continue;
      const hot = e.tier === "hot";
      const pts = samplesBySpacing(curves.get(e.id), hot ? 13 : 17, { margin: 7, max: Math.min(budget, 18) });
      const L = Math.max(3, Math.min(pts.length, hot ? 5 : 4));
      const ms = policy.frameMs || e.flowMs;
      pts.forEach((p, i) => {
        items.push({ t: "text", id: `dot-${e.id}-${i}`, x: p.x - 6, y: p.y - 7, w: 12, h: 14, text: dotFrames(i, L)[0], size: hot ? 10 : 8, color: withAlpha(e.color, hot ? 1 : 0.75), align: "center", frames: { list: dotFrames(i, L), ms } });
      });
      animated += pts.length;
      budget -= pts.length;
    }
  }

  // 6. nodes: halos and rings (native loops), bodies, hatch, dashed outlines, spinners
  const polyFill = new Map();
  const polyStroke = new Map();
  const hatch = new Map();
  const dashed = new Map();
  const add = (m, key, cmds) => { if (!m.has(key)) m.set(key, []); m.get(key).push(...cmds); };
  const hits = [];
  const nodeScreen = new Map();
  // which nodes may pulse under the policy, loudest first, at most maxPulses
  const pulsing = new Set();
  if (policy.pulseScope !== "none" && policy.maxPulses > 0) {
    const want = vm.nodes.filter((n) => n.visual.halo && (policy.pulseScope === "all" || n.visual.needsYou || (policy.pulseScope === "needs+working" && n.visual.key === "working")));
    want.sort((a, b) => (a.visual.urgency ?? 9) - (b.visual.urgency ?? 9) || (a.id < b.id ? -1 : 1));
    for (const n of want.slice(0, policy.maxPulses)) pulsing.add(n.id);
  }
  for (const n of lay.order ?? vm.nodes) { // depth mode: far to near
    const p0 = posOf(n.id);
    if (!p0) continue;
    const v = n.visual;
    const p = S(p0);
    const r = nodeR.get(n.id);
    // depth mode: what is far fades (fog); a hover fades what is not near it (dimOf). Only the hover's fading stops a
    // pulse: fog only makes it fainter (2026-11-13: gated on both, almost no needs-you node pulsed in depth mode)
    const fog = lay.fogOf?.get(n.id) ?? 1, dim = dimOf(n.id) * fog;
    const op = v.opacity * dim;
    hits.push({ id: n.id, x: p.x, y: p.y, r: Math.max(r, 7) });
    nodeScreen.set(n.id, { x: p.x, y: p.y, r });
    const hs = v.halo?.scale ?? 2.4;
    const pulse = pulsing.has(n.id) && dimOf(n.id) === 1;
    const loopPulse = pulse && policy.pulseMode === "loop";
    if (lay.bowl && n.kind === "user") {
      // in the bowl you are a small glass ball in front of the inside (sphere.js glassBall), lit from the bowl's light, not a
      // ring; your halo still breathes behind it where the flat view's would
      if (v.halo && loopPulse) {
        const hr = r * hs;
        items.push({ t: "box", id: `halo-${n.id}`, x: p.x - hr, y: p.y - hr, w: hr * 2, h: hr * 2, radius: hr, bg: withAlpha(v.color, 0.16), opacity: v.halo.to * dim,
          loops: [{ prop: "opacity", from: v.halo.from * fog, to: v.halo.to * fog, period: v.halo.period, easing: "ease-in-out" }] });
        animated++;
      }
      items.push(...glassBall(n.id, p.x, p.y, r, v.color, op, lay.bowl.youLight ?? LIGHT));
      grow(p.x - r - 2, p.y - r - 2, p.x + r + 2, p.y + r + 2);
      continue;
    }
    const outer = v.halo ? r * hs : v.ring ? r + 7 : r + 2;
    grow(p.x - outer, p.y - outer, p.x + outer, p.y + outer);
    if (v.halo) {
      const hr = r * hs;
      items.push({
        t: "box", id: `halo-${n.id}`, x: p.x - hr, y: p.y - hr, w: hr * 2, h: hr * 2, radius: hr,
        bg: withAlpha(v.color, 0.16), opacity: loopPulse ? v.halo.to * dim : (v.needsYou ? 0.55 : 0.32) * dim,
        loops: loopPulse ? [{ prop: "opacity", from: v.halo.from * fog, to: v.halo.to * fog, period: v.halo.period, easing: "ease-in-out" }] : [],
      });
      if (loopPulse) animated++;
      const ir = r * (1 + (hs - 1) * 0.45);
      items.push({ t: "box", id: `halo-in-${n.id}`, x: p.x - ir, y: p.y - ir, w: ir * 2, h: ir * 2, radius: ir, bg: withAlpha(v.color, 0.14 * dim), opacity: 1 });
    }
    if (v.ring) {
      const rr = r + 6;
      items.push({
        t: "box", id: `ring-${n.id}`, x: p.x - rr, y: p.y - rr, w: rr * 2, h: rr * 2, radius: rr,
        border: { w: 1.5, color: withAlpha(v.color, 0.95) }, opacity: loopPulse ? dim : 0.85 * dim,
        loops: loopPulse ? [{ prop: "opacity", from: 0.15 * fog, to: fog, period: v.ring.period, easing: "ease-in-out" }] : [],
      });
    }
    if (pulse && policy.pulseMode === "frames") {
      // a stepped pulse: an outer ring glyph that blinks on the policy's shared frame interval
      const gs = 2 * (r + 11);
      items.push({ t: "text", id: `pulse-${n.id}`, x: p.x - gs / 2, y: p.y - gs / 2, w: gs, h: gs, text: PULSE_GLYPH, size: gs * 0.92, color: withAlpha(v.color, (v.needsYou ? 0.9 : 0.5) * fog), align: "center", frames: { list: pulseFrames(v.needsYou ? "needs" : "work"), ms: policy.frameMs } });
      animated++;
    }
    const bodyColor = v.fill > 0 ? withAlpha(v.color, v.fill * op) : withAlpha(P.void, 0.6 * op);
    const surface = lay.surfaceOf?.get(n.id);
    if (lay.bowl && surface?.facing > 0.18) {
      // in the bowl, a role's contact shadow on the wall it sits on, offset away from the light (the way its shadow falls,
      // depth.js shadowOf), a smooth stack as every shadow is (sphere.js shadowLayers). A globe is glass and catches none:
      // there a role's shadow is on the floor (depth.js casts)
      const sh = lay.shadowOf?.get(n.id), sl = sh ? Math.hypot(sh.dx, sh.dy) : 0;
      const [ux, uy] = sl > 1e-6 ? [sh.dx / sl, sh.dy / sl] : [0.57, 0.82];
      const T = CONTACT, cx = p.x + r * T.off * ux, cy = p.y + r * T.off * uy;
      const wall = surfaceUnder(surfaces, cx, cy) ?? SHADOW_UNDER;
      for (const [i, l] of shadowLayers(T.peak * dim, T.soft, shadowSteps(T.peak * dim, T.soft, r * Math.sqrt(T.rx * T.ry)), wall).entries()) {
        items.push({ t: "path", id: `contact-${n.id}-${i + 1}`, fill: true, cmds: ellipseCmds(cx, cy, r * T.rx * l.s, r * T.ry * l.s), bg: solid(withAlpha(SHADOW_INK, l.alpha)) });
      }
    }
    if (v.shape === "circle") {
      const solidBorder = v.border === "solid" || (v.kind === "user");
      if (surface && v.fill > 0) items.push({ t: "path", id: `role-shade-${n.id}`, fill: true,
        cmds: circleCmds(p.x, p.y, r), bg: { kind: "linear", angle: 135,
          from: withAlpha(mix(v.color, "#ffffff", 0.42), op), to: withAlpha(mix(v.color, "#030916", 0.68), op) } });
      items.push({
        t: "box", id: `body-${n.id}`, x: p.x - r, y: p.y - r, w: r * 2, h: r * 2, radius: r,
        bg: surface && v.fill > 0 ? withAlpha(bodyColor, 0.15 * op) : bodyColor, border: solidBorder ? { w: v.borderWidth || 1.5, color: withAlpha(v.color, op) } : null, opacity: 1,
      });
      if (v.fill >= 0.5 && n.kind === "role" && !surface) {
        const cr = r * 0.42;
        items.push({ t: "box", id: `core-${n.id}`, x: p.x - cr, y: p.y - cr - r * 0.12, w: cr * 2, h: cr * 2, radius: cr, bg: withAlpha(mix(v.color, "#ffffff", 0.55), 0.55 * op), opacity: 1 });
      }
    } else {
      const cmds = shapeCmds(v.shape, p.x, p.y, r);
      add(polyFill, bodyColor, cmds);
      if (v.border === "solid") add(polyStroke, withAlpha(v.color, op), cmds);
    }
    if (v.hatch) add(hatch, `${withAlpha(v.color, 0.75 * op)}|${v.hatch}`, shapeCmds(v.shape, p.x, p.y, r));
    if (v.border === "dashed" || v.border === "dotted") add(dashed, `${v.border}|${withAlpha(v.color, op)}`, shapeCmds(v.shape, p.x, p.y, r + 0.5));
    if (v.spinner && policy.spinners) {
      items.push({ t: "spin", id: `spin-${n.id}`, x: p.x - r - 5, y: p.y - r - 5, size: 2 * r + 10, color: withAlpha(v.color, 0.9 * dim), ms: 1400 });
      animated++;
    }
  }
  let pi = 0;
  for (const [color, cmds] of polyFill) items.push({ t: "path", id: `poly-${pi++}`, fill: true, cmds, bg: solid(color) });
  pi = 0;
  for (const [color, cmds] of polyStroke) items.push({ t: "path", id: `polyline-${pi++}`, fill: false, width: 1.6, cmds, bg: solid(color) });
  pi = 0;
  for (const [key, cmds] of hatch) {
    const [color, interval] = key.split("|");
    items.push({ t: "path", id: `hatch-${pi++}`, fill: true, cmds, bg: { kind: "slash", color, width: 1.2, interval: Number(interval) || 4 } });
  }
  pi = 0;
  for (const [key, cmds] of dashed) {
    const [style, color] = key.split("|");
    items.push({ t: "path", id: `dash-${pi++}`, fill: false, width: style === "dotted" ? 1.8 : 1.6, dash: style === "dotted" ? [1.6, 3.2] : [4, 3], cmds, bg: solid(color) });
  }

  // 7. broadcast ripples (native loops on left, top, width, height and opacity, one clock)
  if (policy.ripples) {
    for (const b of vm.broadcasts) {
      if (b.tier !== "hot" && b.tier !== "warm") continue;
      const p0 = posOf(b.src);
      if (!p0 || dimOf(b.src) < 1) continue;
      const p = S(p0);
      const r0 = nodeR.get(b.src) + 4;
      const r1 = r0 + (b.tier === "hot" ? 46 : 32) * clamp(k, 0.7, 1.4);
      const period = b.tier === "hot" ? 2400 : 3600;
      items.push({
        t: "box", id: `ripple-${b.src}`, x: p.x - r0, y: p.y - r0, w: 2 * r0, h: 2 * r0, radius: r1 + 2,
        border: { w: 1.2, color: withAlpha(P.cyan, 0.85) }, opacity: 0.5,
        loops: [
          { prop: "left", from: p.x - r0, to: p.x - r1, period, easing: "ease-out" },
          { prop: "top", from: p.y - r0, to: p.y - r1, period, easing: "ease-out" },
          { prop: "width", from: 2 * r0, to: 2 * r1, period, easing: "ease-out" },
          { prop: "height", from: 2 * r0, to: 2 * r1, period, easing: "ease-out" },
          { prop: "opacity", from: 0.7, to: 0, period, easing: "ease-out" },
        ],
      });
      animated++;
    }
  }

  // 8. labels and badges (screen size, not zoomed). Labels are placed greedily, loudest first:
  // below the node, else above, right or left, whichever does not collide with a placed label or
  // another node; roles always get one, quieter nodes drop theirs when there is no room.
  const placed = [...labelObstacles];
  const bodies = [...nodeScreen].map(([id, s]) => ({ id, x: s.x - s.r, y: s.y - s.r, w: 2 * s.r, h: 2 * s.r }));
  const rank = (n) => (n.visual.needsYou ? 0 : n.kind === "role" ? 1 : n.kind === "user" ? 2 : 3);
  const order = vm.nodes.filter((n) => nodeScreen.has(n.id)).sort((a, b) => rank(a) - rank(b) || (a.id < b.id ? -1 : 1));
  for (const n of order) {
    const s = nodeScreen.get(n.id);
    const v = n.visual;
    const dim = dimOf(n.id);
    const focus = n.id === hover || n.id === opts.selected;
    const loud = v.needsYou || focus;
    const isRole = n.kind === "role";
    // an aliased external whose home role is in view: the alias tether says it, the label repeats it
    const aliased = n.kind === "external" && n.alias_of && vm.byId.has(n.alias_of);
    if (aliased && !focus) continue;
    const size = isRole ? 10.5 : n.kind === "user" ? 11 : 9;
    const away = vm.global && n.awayHost ? ` @${n.awayHost}` : "";
    const text = fitText(n.kind === "user" ? "you" : n.label + away, isRole ? (away ? 210 : 150) : 120, size);
    const tagW = n.hostColor ? 10 : 0;
    const w = textW(text, size) + tagW + 4, h = 14;
    const gap = (v.halo && isRole ? 4 : 3) + (n.items?.length ? 6 : 0);
    const spots = [
      { x: s.x - w / 2, y: s.y + s.r + gap },
      { x: s.x - w / 2, y: s.y - s.r - gap - h - (v.badge ? 6 : 0) },
      { x: s.x + s.r + 8, y: s.y - h / 2 },
      { x: s.x - s.r - 8 - w, y: s.y - h / 2 },
    ];
    const free = (r) => r.x >= 2 && r.x + r.w <= width - 2 && !placed.some((q) => overlaps(q, r)) && !bodies.some((b) => b.id !== n.id && overlaps(b, r));
    let spot = spots.map((p) => ({ ...p, w, h })).find(free);
    if (!spot) {
      if (!isRole && n.kind !== "user" && !focus) continue;
      spot = { ...spots[0], w, h };
    }
    placed.push(spot);
    growRect(spot);
    // on a globe or in the bowl, the ink (light or dark) that reads against what is under the label
    const under = surfaceUnder(surfaces, spot.x + w / 2, spot.y + h / 2);
    const color = under
      ? withAlpha(readableInk(under, v.needsYou ? v.color : null), (isRole || n.kind === "user" ? 0.98 : 0.85) * dim)
      : withAlpha(loud ? (v.needsYou ? mix(v.color, "#ffffff", 0.45) : P.ink) : isRole ? P.ink : P.dim, (isRole ? 0.92 : 0.75) * dim);
    items.push({
      t: "text", id: `label-${n.id}`, x: spot.x + tagW, y: spot.y, w: w - tagW + 2, h, text,
      size, align: "left", weight: loud || under ? "semibold" : null, color,
    });
    if (n.hostColor) {
      items.push({ t: "box", id: `hosttag-${n.id}`, x: spot.x + 1, y: spot.y + 4, w: 6, h: 6, radius: 3, bg: withAlpha(n.hostColor, 0.95 * dim), opacity: 1 });
    }
    if (v.grants > 0 && n.kind === "role") {
      const gs = 14, gx = s.x - s.r * 0.72 - gs + 1, gy = s.y - s.r * 0.72 - gs * 0.55;
      items.push({ t: "box", id: `grant-bg-${n.id}`, x: gx, y: gy, w: gs, h: gs, radius: gs / 2, bg: withAlpha(P.olive, dim), border: { w: 1.5, color: withAlpha(P.bgBottom, dim) }, opacity: 1 });
      items.push({ t: "text", id: `grant-${n.id}`, x: gx, y: gy, w: gs, h: gs, text: v.grants > 1 ? String(v.grants) : "\u2691", size: 8.5, color: withAlpha("#0b0b10", dim), align: "center", weight: "bold" });
    }
    if (v.badge) {
      const bs = n.kind === "user" ? 16 : 14;
      const bx = s.x + s.r * 0.72, by = s.y - s.r * 0.72 - bs * 0.55;
      items.push({ t: "box", id: `badge-bg-${n.id}`, x: bx - 1, y: by, w: bs, h: bs, radius: bs / 2, bg: withAlpha(n.kind === "user" ? P.magenta : v.color, dim), border: { w: 1.5, color: withAlpha(P.bgBottom, dim) }, opacity: 1 });
      items.push({ t: "text", id: `badge-${n.id}`, x: bx - 1, y: by, w: bs, h: bs, text: v.badge, size: n.kind === "user" ? 9.5 : 9, color: withAlpha("#0b0b10", dim), align: "center", weight: "bold" });
    }
  }

  // 9. discrete events: packets from source to target, flashes on done, one-shot ripples
  for (const pk of policy.packets ? opts.packets ?? [] : []) {
    animated++;
    const going = pk.phase === "go";
    if (pk.kind === "packet") {
      const a = posOf(pk.src), b = posOf(pk.dst);
      if (!a || !b) continue;
      const p = S(going ? b : a);
      const sz = 9;
      items.push({
        t: "box", id: `pk-${pk.seq}`, x: p.x - sz / 2, y: p.y - sz / 2, w: sz, h: sz, radius: sz / 2,
        bg: withAlpha(mix(pk.color, "#ffffff", 0.35), 1), border: { w: 3, color: withAlpha(pk.color, 0.35) }, opacity: going ? 0.95 : 1,
        transitions: [{ prop: "left", duration: pk.ms, easing: "ease-in-out" }, { prop: "top", duration: pk.ms, easing: "ease-in-out" }, { prop: "opacity", duration: pk.ms, easing: "ease-in" }],
      });
    } else {
      const a = posOf(pk.src);
      if (!a) continue;
      const p = S(a);
      const r0 = (nodeR.get(pk.src) ?? 10) + 2;
      const r1 = r0 + (pk.kind === "broadcast" ? 70 : 26);
      const r = going ? r1 : r0;
      items.push({
        t: "box", id: `fx-${pk.seq}`, x: p.x - r, y: p.y - r, w: 2 * r, h: 2 * r, radius: r1 + 2,
        border: { w: pk.kind === "broadcast" ? 1.5 : 2.5, color: pk.color }, bg: pk.kind === "done" ? withAlpha(pk.color, 0.18) : null, opacity: going ? 0 : 1,
        transitions: ["left", "top", "width", "height", "opacity"].map((prop) => ({ prop, duration: pk.ms, easing: "ease-out" })),
      });
    }
  }

  if (!Number.isFinite(extent.x0)) Object.assign(extent, { x0: 0, y0: 0, x1: width, y1: height });
  for (const it of [...(edgeShadow.strokes ?? []), edgeShadow.heads]) if (it && !it.cmds.length) items.splice(items.indexOf(it), 1);
  return { items, hits, satHits, edgeHits, discs, nodeScreen, satScreen, edgeScreen, animated, extent };
}

/** The world box of what scales with zoom: node centers, satellites and the cluster regions. */
function worldBounds(lay) {
  const pts = [...lay.pos.values()].map((p) => ({ x: p.x, y: p.y, r: 0 }));
  for (const s of lay.sats ?? []) pts.push({ x: s.x, y: s.y, r: 0 });
  for (const c of lay.clusters ?? []) pts.push({ x: c.x, y: c.y, r: c.r });
  // depth mode: each host's band is on its own raised plane, and the stems reach the floor; the grid may run off
  for (const b of lay.bands ?? []) {
    if (b.cx === undefined) continue;
    const R = b.r1 * (b.s ?? 1);
    pts.push({ x: b.cx - R, y: b.cy, r: 0 }, { x: b.cx + R, y: b.cy, r: 0 }, { x: b.cx, y: b.cy - R * (b.ry ?? 1), r: 0 }, { x: b.cx, y: b.cy + R * (b.ry ?? 1), r: 0 });
  }
  // and the middle of each shadow, so a shadow cast aside stays in view (its soft edge may run off, as the grid does)
  // (the bowl's lies behind it, on its own floor)
  for (const st of lay.stems ?? []) pts.push({ x: st.x1, y: st.y1, r: 0 }, ...(st.shadow && !lay.bowl ? [{ x: st.shadow.x, y: st.shadow.y, r: 0 }] : []));
  return boundsOf(pts);
}

/**
 * The view transform that fits the whole graph inside a canvas `size` {width, height}: every
 * node, label, chip and cluster ring, `pad` px clear of each side and `top` px below the header.
 * Labels and halos keep their screen size whatever the zoom, so a fit to world bounds alone
 * clips them; this measures the scene, solves for the zoom that leaves room for that overhang,
 * and centers the result, repeating while label placement settles. Options: {pad, top, kMax,
 * iterations}.
 */
export function fitScene(vm, lay, size, opts = {}) {
  const pad = opts.pad ?? 24, top = opts.top ?? 48, kMax = opts.kMax ?? 1.6;
  const width = Math.max(1, size.width), height = Math.max(1, size.height);
  const availW = Math.max(1, width - 2 * pad), availH = Math.max(1, height - top - pad);
  const wb = worldBounds(lay);
  const ww = Math.max(wb.w, 1), wh = Math.max(wb.h, 1);
  let tf = fitTransform(wb, { width, height }, pad, kMax);
  const policy = motionPolicy("off");
  for (let i = 0; i < (opts.iterations ?? 5); i++) {
    const e = buildScene(vm, lay, { width, height, tf, policy }).extent;
    const sx0 = wb.x * tf.k + tf.tx, sx1 = (wb.x + wb.w) * tf.k + tf.tx;
    const sy0 = wb.y * tf.k + tf.ty, sy1 = (wb.y + wb.h) * tf.k + tf.ty;
    // what the drawn scene adds beyond the world box on each side, in screen px (negative where
    // the world box reaches further than anything drawn)
    const ovL = sx0 - e.x0, ovR = e.x1 - sx1, ovT = sy0 - e.y0, ovB = e.y1 - sy1;
    const k = clamp(Math.min(kMax, (availW - ovL - ovR) / ww, (availH - ovT - ovB) / wh), 0.1, kMax);
    const next = {
      k,
      tx: pad + (availW - (ww * k + ovL + ovR)) / 2 + ovL - wb.x * k,
      ty: top + (availH - (wh * k + ovT + ovB)) / 2 + ovT - wb.y * k,
    };
    const done = Math.abs(next.k - tf.k) < 1e-4 * tf.k && Math.abs(next.tx - tf.tx) < 0.25 && Math.abs(next.ty - tf.ty) < 0.25;
    tf = next;
    if (done) break;
  }
  return tf;
}

/** Header text for a view: breadcrumb and counts. */
export function headerText(vm) {
  const roles = vm.nodes.filter((n) => n.kind === "role").length;
  const where = vm.global ? "all shoals" : `all shoals / ${shortShoal(vm.view)}`;
  return { where, counts: `${roles} roles · ${vm.edges.length} edges` };
}

/** The legend: state colors in rail order. */
export const LEGEND = [
  ["auth", P.auth], ["asks", P.magenta], ["working", P.teal], ["system", P.blue],
  ["upstream", P.violet], ["peer", P.amber], ["idle", P.slate], ["unknown", P.red],
];
