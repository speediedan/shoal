// geom.js: pure geometry for the shoal lattice. No Herdr or GPUI imports, so it runs under Node.
//
// Points are {x, y}. A curve is a cubic bezier {p0, c1, c2, p3}. A view transform is
// {k, tx, ty}: screen = world * k + t. Path commands are arrays ["M", x, y], ["L", x, y],
// ["C", c1x, c1y, c2x, c2y, x, y], ["A", rx, ry, rot, large, sweep, x, y], ["Z"]; main.js turns
// them into PathBuilder calls and the preview turns them into SVG `d` strings.

export const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
export const lerp = (a, b, t) => a + (b - a) * t;
const finite = (v, d = 0) => (Number.isFinite(v) ? v : d);

/** FNV-1a 32-bit: a stable hash of a string (node ids seed the layout). */
export function hash32(str) {
  let h = 0x811c9dc5;
  const s = String(str);
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** mulberry32: a small deterministic generator in [0, 1). */
export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const sub = (a, b) => ({ x: a.x - b.x, y: a.y - b.y });
const add = (a, b) => ({ x: a.x + b.x, y: a.y + b.y });
const mul = (a, k) => ({ x: a.x * k, y: a.y * k });
export const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
function unit(v) {
  const l = Math.hypot(v.x, v.y);
  return l > 1e-9 ? { x: v.x / l, y: v.y / l } : { x: 1, y: 0 };
}

/**
 * A gently bent cubic from a to b. `bend` is the sideways offset as a share of the length, to the
 * left of the direction of travel, so a->b and b->a bow apart. `trimA`/`trimB` pull the ends back
 * by the node radii so arrows land on the rim, not the center.
 */
export function edgeCurve(a, b, { bend = 0.14, trimA = 0, trimB = 0 } = {}) {
  const d = sub(b, a);
  const len = Math.hypot(d.x, d.y);
  if (len < 1e-6) {
    // a loop on one node: a small teardrop above it
    const r = Math.max(trimA, 6) * 2.4;
    return { p0: { x: a.x - 3, y: a.y - trimA }, c1: { x: a.x - r, y: a.y - r * 1.6 }, c2: { x: a.x + r, y: a.y - r * 1.6 }, p3: { x: a.x + 3, y: a.y - trimA } };
  }
  const n = { x: -d.y / len, y: d.x / len };
  const off = bend * len;
  const c1 = add(add(a, mul(d, 0.25)), mul(n, off));
  const c2 = add(add(a, mul(d, 0.75)), mul(n, off));
  const tA = Math.min(trimA, len * 0.45);
  const tB = Math.min(trimB, len * 0.45);
  const p0 = add(a, mul(unit(sub(c1, a)), tA));
  const p3 = add(b, mul(unit(sub(c2, b)), tB));
  return { p0, c1, c2, p3 };
}

export function pointAt(c, t) {
  const u = 1 - t;
  const a = u * u * u, b = 3 * u * u * t, d = 3 * u * t * t, e = t * t * t;
  return {
    x: a * c.p0.x + b * c.c1.x + d * c.c2.x + e * c.p3.x,
    y: a * c.p0.y + b * c.c1.y + d * c.c2.y + e * c.p3.y,
  };
}

export function tangentAt(c, t) {
  const u = 1 - t;
  const x = 3 * u * u * (c.c1.x - c.p0.x) + 6 * u * t * (c.c2.x - c.c1.x) + 3 * t * t * (c.p3.x - c.c2.x);
  const y = 3 * u * u * (c.c1.y - c.p0.y) + 6 * u * t * (c.c2.y - c.c1.y) + 3 * t * t * (c.p3.y - c.c2.y);
  return unit({ x, y });
}

export function curveLength(c, steps = 24) {
  let len = 0;
  let prev = c.p0;
  for (let i = 1; i <= steps; i++) {
    const p = pointAt(c, i / steps);
    len += dist(prev, p);
    prev = p;
  }
  return len;
}

/**
 * Points evenly spaced by arc length along the curve, leaving `margin` px clear at both ends:
 * where the marching dots sit. At most `max` points.
 */
export function samplesBySpacing(c, spacing, { margin = 6, max = 16 } = {}) {
  const steps = 48;
  const table = [{ t: 0, s: 0 }];
  let prev = c.p0;
  let s = 0;
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    const p = pointAt(c, t);
    s += dist(prev, p);
    table.push({ t, s });
    prev = p;
  }
  const usable = s - 2 * margin;
  if (!(usable > spacing * 0.5)) return [];
  const n = clamp(Math.floor(usable / spacing) + 1, 1, max);
  const step = n > 1 ? usable / (n - 1) : 0;
  const out = [];
  let j = 1;
  for (let i = 0; i < n; i++) {
    const target = margin + (n > 1 ? i * step : usable / 2);
    while (j < table.length - 1 && table[j].s < target) j++;
    const a = table[j - 1], b = table[j];
    const f = b.s > a.s ? (target - a.s) / (b.s - a.s) : 0;
    out.push(pointAt(c, lerp(a.t, b.t, clamp(f, 0, 1))));
  }
  return out;
}

/** An arrowhead triangle with its tip on the curve's end, along the end tangent. */
export function arrowHead(c, size = 6, spread = 0.55) {
  const tip = c.p3;
  const dir = tangentAt(c, 1);
  const back = { x: tip.x - dir.x * size, y: tip.y - dir.y * size };
  const n = { x: -dir.y, y: dir.x };
  const w = size * spread;
  return [
    [tip.x, tip.y],
    [back.x + n.x * w, back.y + n.y * w],
    [back.x - n.x * w, back.y - n.y * w],
  ];
}

// ---- shapes

/** Polygon points for a node shape centered at (cx, cy), circumradius r. */
export function shapePoints(kind, cx, cy, r) {
  const ring = (n, start) => Array.from({ length: n }, (_, i) => {
    const a = start + (i * 2 * Math.PI) / n;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  });
  switch (kind) {
    case "hexagon": return ring(6, 0);
    case "diamond": return ring(4, -Math.PI / 2);
    case "triangle": return ring(3, -Math.PI / 2);
    case "square": {
      const h = r * 0.78;
      return [[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h]];
    }
    default: return ring(24, 0);
  }
}

export function polygonCmds(points) {
  const out = points.map(([x, y], i) => [i === 0 ? "M" : "L", x, y]);
  out.push(["Z"]);
  return out;
}

/** A full circle as two arcs. */
export function circleCmds(cx, cy, r) {
  return [
    ["M", cx + r, cy],
    ["A", r, r, 0, false, true, cx - r, cy],
    ["A", r, r, 0, false, true, cx + r, cy],
    ["Z"],
  ];
}

/** Node outline commands for a shape kind ("circle" uses arcs, the rest polygons). */
export function shapeCmds(kind, cx, cy, r) {
  return kind === "circle" || !kind ? circleCmds(cx, cy, r) : polygonCmds(shapePoints(kind, cx, cy, r));
}

/** An annular sector (a host band): angles in radians, clockwise from a0 to a1. */
export function annularSectorCmds(cx, cy, r0, r1, a0, a1) {
  const p = (r, a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const large = a1 - a0 > Math.PI;
  const [ox0, oy0] = p(r1, a0), [ox1, oy1] = p(r1, a1), [ix1, iy1] = p(r0, a1), [ix0, iy0] = p(r0, a0);
  return [
    ["M", ox0, oy0],
    ["A", r1, r1, 0, large, true, ox1, oy1],
    ["L", ix1, iy1],
    ["A", r0, r0, 0, large, false, ix0, iy0],
    ["Z"],
  ];
}

export function curveCmds(c, move = true) {
  const out = move ? [["M", c.p0.x, c.p0.y]] : [];
  out.push(["C", c.c1.x, c.c1.y, c.c2.x, c.c2.y, c.p3.x, c.p3.y]);
  return out;
}

/** SVG `d` for a list of commands (used by the preview and by tests). */
export function pathD(cmds) {
  const f = (v) => (typeof v === "boolean" ? (v ? 1 : 0) : Math.round(finite(v) * 100) / 100);
  return cmds.map(([op, ...args]) => op + args.map(f).join(" ")).join("");
}

// ---- hit testing

/**
 * The node under a pointer: the closest whose disc (radius + slop) contains it. `nodes` are
 * {id, x, y, r} in the same space as `p`. Returns the id or null.
 */
export function hitNode(nodes, p, slop = 4) {
  let best = null;
  let bestD = Infinity;
  for (const n of nodes) {
    const d = Math.hypot(n.x - p.x, n.y - p.y);
    if (d <= (n.r ?? 8) + slop && d < bestD) {
      best = n.id;
      bestD = d;
    }
  }
  return best;
}

/**
 * The edge under a pointer: the closest whose curve passes within `slop`. `edges` are {id, c} with c
 * an `edgeCurve` in the same space as `p`. Returns the id or null.
 */
export function hitEdge(edges, p, slop = 5, steps = 16) {
  let best = null;
  let bestD = slop;
  for (const { id, c } of edges) {
    // the curve lies inside its control points' box
    const xs = [c.p0.x, c.c1.x, c.c2.x, c.p3.x], ys = [c.p0.y, c.c1.y, c.c2.y, c.p3.y];
    if (p.x < Math.min(...xs) - slop || p.x > Math.max(...xs) + slop || p.y < Math.min(...ys) - slop || p.y > Math.max(...ys) + slop) continue;
    let a = c.p0;
    for (let i = 1; i <= steps; i++) {
      const b = pointAt(c, i / steps);
      const dx = b.x - a.x, dy = b.y - a.y;
      const t = clamp(((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy || 1), 0, 1);
      const d = Math.hypot(a.x + t * dx - p.x, a.y + t * dy - p.y);
      if (d <= bestD) {
        best = id;
        bestD = d;
      }
      a = b;
    }
  }
  return best;
}

/** The cluster disc ({id, x, y, r, title?}) under a pointer, its title above it included, or null. */
export function hitDisc(discs, p) {
  for (const c of discs) if (Math.hypot(c.x - p.x, c.y - p.y) <= c.r || inRect(c.title, p)) return c.id;
  return null;
}

/** The disc whose middle ({hole}: a radius) is under a pointer, or null. */
export function hitHole(discs, p) {
  for (const c of discs) if (c.hole && Math.hypot(c.x - p.x, c.y - p.y) <= c.hole) return c.id;
  return null;
}

/** True when p is inside the rectangle {x, y, w, h}. */
export const inRect = (r, p) => !!r && p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h;

// ---- pan and zoom

export const identity = () => ({ k: 1, tx: 0, ty: 0 });
export const toScreen = (tf, p) => ({ x: p.x * tf.k + tf.tx, y: p.y * tf.k + tf.ty });
export const toWorld = (tf, p) => ({ x: (p.x - tf.tx) / tf.k, y: (p.y - tf.ty) / tf.k });
export const panBy = (tf, dx, dy) => ({ k: tf.k, tx: tf.tx + dx, ty: tf.ty + dy });

/** Zooms by `factor` keeping the world point under `anchor` (screen) fixed. */
export function zoomAt(tf, anchor, factor, kMin = 0.25, kMax = 4) {
  const k = clamp(tf.k * factor, kMin, kMax);
  const w = toWorld(tf, anchor);
  return { k, tx: anchor.x - w.x * k, ty: anchor.y - w.y * k };
}

/**
 * The zoom factor for one scroll event's `delta.y` (pixels). GPUI reports a wheel turned up (away
 * from you, X button 4) as a positive delta.y, and a trackpad's pixel deltas use the same sign, so
 * up zooms in and down zooms out, the map convention: exp(dy * 0.004), at most 2x either way per
 * event so a burst of line deltas cannot jump.
 */
export const wheelFactor = (dy) => Math.exp(clamp(finite(dy) * 0.004, -Math.LN2, Math.LN2));

/**
 * Where a hover card of `w` x `h` goes beside a node `at` {x, y, r} inside a canvas `size`
 * {width, height}: right of the node, else left of it; top near the node, flipped to end near it
 * when it would run off the bottom; then clamped inside the canvas with `margin`. A card taller
 * than the canvas is capped at `maxH` (the caller drops lines to fit). Returns {x, y, w, h, maxH}.
 */
export function placeCard(at, size, w, h, margin = 8) {
  const W = finite(size.width, 800), H = finite(size.height, 600);
  const maxH = Math.max(40, H - 2 * margin);
  const ch = Math.min(h, maxH);
  const gap = (at.r ?? 8) + 16;
  let x = at.x + gap;
  if (x + w > W - margin) x = at.x - gap - w;
  x = clamp(x, margin, Math.max(margin, W - w - margin));
  let y = at.y - 20;
  if (y + ch > H - margin) y = at.y + 20 - ch;
  y = clamp(y, margin, Math.max(margin, H - ch - margin));
  return { x, y, w, h: ch, maxH };
}

export function boundsOf(points, pad = 0) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const p of points) {
    const r = (p.r ?? 0) + pad;
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    x0 = Math.min(x0, p.x - r); y0 = Math.min(y0, p.y - r);
    x1 = Math.max(x1, p.x + r); y1 = Math.max(y1, p.y + r);
  }
  if (!Number.isFinite(x0)) return { x: -100, y: -100, w: 200, h: 200 };
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** The transform that fits world bounds into a viewport {width, height} with padding. */
export function fitTransform(b, size, pad = 40, kMax = 1.6) {
  const w = Math.max(1, finite(size.width, 800) - 2 * pad);
  const h = Math.max(1, finite(size.height, 600) - 2 * pad);
  const k = clamp(Math.min(w / Math.max(b.w, 1), h / Math.max(b.h, 1)), 0.2, kMax);
  return {
    k,
    tx: pad + (w - b.w * k) / 2 - b.x * k,
    ty: pad + (h - b.h * k) / 2 - b.y * k,
  };
}

// ---- color

/** "#rrggbb" plus an alpha in 0..1 as "#rrggbbaa". Accepts "#rgb" and "#rrggbbaa" too. */
export function withAlpha(hex, a) {
  let h = String(hex).replace("#", "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  h = h.slice(0, 6);
  const v = Math.round(clamp(finite(a, 1), 0, 1) * 255).toString(16).padStart(2, "0");
  return `#${h}${v}`;
}

/** Mixes two "#rrggbb" colors. */
export function mix(a, b, t) {
  const p = (h) => {
    const s = String(h).replace("#", "");
    return [0, 2, 4].map((i) => parseInt(s.slice(i, i + 2), 16));
  };
  const ca = p(a), cb = p(b);
  return "#" + ca.map((v, i) => Math.round(lerp(v, cb[i], t)).toString(16).padStart(2, "0")).join("");
}
