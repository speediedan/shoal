// ocean.js: the splash's sea around the SHOAL logo, as a display list (no GPUI here, so it runs under Node).
//
// Kelp strands rise from a seabed line just above the tagline, in the room either side of the logo, and
// weave behind the creatures. The two sets of strands are the two shoals. Each strand carries floats
// (pneumatocysts), and the floats are the nodes of a small network: tendrils link each float to the next
// strand's float at the same height, and link the creatures to their nearest float, so a shoal reads as
// connected nodes and edges without a single straight line.
//
// Between the shoals, two solo vines are the standalone sessions: each rises from the seabed and threads through
// a letter, behind one stroke and in front of the next, to a single float above the logo, and each float is tied
// to both shoals. One more tendril is the only direct link between the shoals. Below the logo, two foam waves;
// above its right end, a few rising bubbles.
//
// Everything is static and deterministic (from the layout alone). Only the solo vines cross the logo, and
// nothing comes near the host cards, so the logo and every card stay readable.

import { logoLetters, capPoint } from "./logo.js";

export const SEA = {
  kelpDeep: "#0d4f4c",
  kelp: "#1f8f7a",
  kelpLight: "#5fd3b0",
  float: "#47d6b4",
  floatRing: "#9ff5de",
  tendril: "#38c8ff",
  foam: "#9fe9ff",
  wave: "#38c8ff",
  bubble: "#bdf2ff",
  soloDeep: "#0a4a48",
  solo: "#2aa585",
  soloLight: "#9debcf",
  soloLink: "#8af0c8",
};

const r2 = (v) => Math.round(v * 10) / 10;

// a small deterministic hash (mulberry32) so a layout always draws the same sea
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

/** The point at `t` (0..1) along a strand's cubic segments (sampled per segment). */
function along(segs, t) {
  const n = segs.length;
  const k = Math.min(n - 1, Math.floor(t * n));
  const u = t * n - k;
  const [x0, y0, c1x, c1y, c2x, c2y, x1, y1] = segs[k];
  const m = 1 - u;
  return {
    x: m * m * m * x0 + 3 * m * m * u * c1x + 3 * m * u * u * c2x + u * u * u * x1,
    y: m * m * m * y0 + 3 * m * m * u * c1y + 3 * m * u * u * c2y + u * u * u * y1,
  };
}

/** One strand: a stack of cubic segments from (x, base) up to height h, swaying by `amp` and kept inside
 * [lo, hi] horizontally. Returns the segments as [x0, y0, c1x, c1y, c2x, c2y, x1, y1]. */
function strand(x, base, h, amp, lo, hi, rand) {
  const n = 3;
  const clamp = (v) => Math.max(lo, Math.min(hi, v));
  const segs = [];
  let px = x, py = base;
  const dir = rand() < 0.5 ? -1 : 1;
  for (let i = 0; i < n; i++) {
    const s = (i % 2 === 0 ? 1 : -1) * dir;
    const ny = base - (h * (i + 1)) / n;
    const nx = clamp(x + s * amp * (0.35 + 0.25 * rand()));
    const dy = (py - ny) / 3;
    segs.push([px, py, clamp(px + s * amp), py - dy, clamp(nx + s * amp * 0.6), ny + dy, nx, ny].map(r2));
    px = nx;
    py = ny;
  }
  return segs;
}

const segCmds = (segs) => [["M", segs[0][0], segs[0][1]], ...segs.map((s) => ["C", s[2], s[3], s[4], s[5], s[6], s[7]])];

/** Cubic segments of a spline through `pts` (Catmull-Rom, open), as [x0, y0, c1x, c1y, c2x, c2y, x1, y1]. */
function splineSegs(pts) {
  const at = (i) => pts[Math.max(0, Math.min(pts.length - 1, i))];
  const segs = [];
  for (let i = 0; i + 1 < pts.length; i++) {
    const [p0, p1, p2, p3] = [at(i - 1), at(i), at(i + 1), at(i + 2)];
    segs.push([p1.x, p1.y, p1.x + (p2.x - p0.x) / 6, p1.y + (p2.y - p0.y) / 6, p2.x - (p3.x - p1.x) / 6, p2.y - (p3.y - p1.y) / 6, p2.x, p2.y].map(r2));
  }
  return segs;
}

/** A leaf blade at `t` along a strand, leaning out to side `dir` and up, its tip kept inside [lo, hi]. */
function leafCmds(segs, t, dir, len, wid, lo, hi) {
  const p = along(segs, t);
  const q = along(segs, Math.min(0.99, t + 0.04));
  const tx = q.x - p.x, ty = q.y - p.y;
  const tl = Math.hypot(tx, ty) || 1;
  let tipX = p.x + dir * len * 0.85 + (tx / tl) * len * 0.5;
  tipX = Math.max(lo, Math.min(hi, tipX));
  const tipY = p.y - len * 0.75 + (ty / tl) * len * 0.3;
  const mx = (p.x + tipX) / 2, my = (p.y + tipY) / 2;
  const nx = -(tipY - p.y), ny = tipX - p.x;
  const nl = Math.hypot(nx, ny) || 1;
  const ox = (nx / nl) * wid, oy = (ny / nl) * wid;
  return [["M", r2(p.x), r2(p.y)],
    ["C", r2(mx + ox), r2(my + oy), r2(mx + ox * 0.4), r2(my + oy * 0.4), r2(tipX), r2(tipY)],
    ["C", r2(mx - ox * 0.4), r2(my - oy * 0.4), r2(mx - ox), r2(my - oy), r2(p.x), r2(p.y)], ["Z"]];
}

// The solo vines, in the cap units of the letter each threads (logo.js capPoint: x from the letter's left, y 0 on
// the cap line, 1 on the baseline), so they follow the letters at any scale. `way`: [x, y, over]; `over` draws the
// stretch from that point to the next in front of the letters, so a vine goes under one stroke and over the next.
// Each crossing's ends lie in open water, so no seam shows.
export const SOLO_VINES = [
  // up the gap between S and H, under the H's left stem, over its bar, out through its open top
  { letter: 1, root: [-0.13, 1.28], tip: [0.62, -0.2],
    way: [[0.02, 1.06], [0.3, 0.87], [0.5, 0.68, true], [0.45, 0.38], [0.5, 0.12]] },
  // up the gap between A and L, under the A's right leg, over its bar, under its left stroke, out over the O
  { letter: 3, root: [1.12, 1.28], tip: [0.02, -0.2],
    way: [[0.99, 1.04], [0.72, 0.9], [0.47, 0.85, true], [0.45, 0.53], [0.27, 0.33], [0.1, 0.1]] },
];

/** A curved tendril between two points, bowed by `bow` (a fraction of its length) to one side. */
function tendril(a, b, bow) {
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  const dx = b.x - a.x, dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len, ny = dx / len;
  const cx = mx + nx * len * bow, cy = my + ny * len * bow;
  return [["M", r2(a.x), r2(a.y)], ["C", r2((a.x + 2 * cx) / 3), r2((a.y + 2 * cy) / 3), r2((b.x + 2 * cx) / 3), r2((b.y + 2 * cy) / 3), r2(b.x), r2(b.y)]];
}

/** An upward arch from a to b: both controls `lift` above the higher end. */
function arch(a, b, lift) {
  const y = Math.min(a.y, b.y) - lift;
  return [["M", r2(a.x), r2(a.y)], ["C", r2(a.x + (b.x - a.x) / 3), r2(y), r2(a.x + (2 * (b.x - a.x)) / 3), r2(y), r2(b.x), r2(b.y)]];
}

/** Points along a path's cubic segments (24 per segment), for clearance checks. */
export function sampleCurve(cmds) {
  const pts = [];
  let px = 0, py = 0;
  for (const c of cmds) {
    if (c[0] === "M" || c[0] === "L") { px = c[1]; py = c[2]; pts.push({ x: px, y: py }); continue; }
    if (c[0] !== "C") continue;
    for (let i = 1; i <= 24; i++) {
      const t = i / 24, m = 1 - t;
      pts.push({ x: m * m * m * px + 3 * m * m * t * c[1] + 3 * m * t * t * c[3] + t * t * t * c[5],
                 y: m * m * m * py + 3 * m * m * t * c[2] + 3 * m * t * t * c[4] + t * t * t * c[6] });
    }
    px = c[5]; py = c[6];
  }
  return pts;
}

const circleCmds = (x, y, r) => [["M", r2(x - r), r2(y)], ["A", r2(r), r2(r), 0, false, true, r2(x + r), r2(y)],
  ["A", r2(r), r2(r), 0, false, true, r2(x - r), r2(y)], ["Z"]];

/** A wave across [x0, x1] at y: alternating crests of height `amp` and width `w`. */
function waveCmds(x0, x1, y, amp, w) {
  const cmds = [["M", r2(x0), r2(y)]];
  let x = x0, up = true;
  while (x < x1 - 0.5) {
    const nx = Math.min(x1, x + w);
    const a = up ? -amp : amp;
    cmds.push(["C", r2(x + (nx - x) / 3), r2(y + a), r2(x + (2 * (nx - x)) / 3), r2(y + a), r2(nx), r2(y)]);
    x = nx;
    up = !up;
  }
  return cmds;
}

/**
 * The sea for one art layout (`homeLayout` in art mode). Returns null in text mode.
 * {strands, vines, nodes, edges, links, bridge, waves, bubbles, seabed, items, front}: `items` is the draw order
 * behind the logo (back to front), `front` goes over it, each {kind, cmds, fill?, width?, color, alpha, sway?};
 * the rest is for tests and hit checks. `links` tie each solo vine's float to both shoals; `bridge` is the one
 * direct link between the shoals.
 */
export function oceanScene(L) {
  if (!L || L.mode !== "art") return null;
  const u = L.scale ?? 1;
  const W = L.width;
  const lg = L.logo;
  const cardsTop = L.cards.length ? Math.min(...L.cards.map((c) => c.y)) : L.foot.y;
  const seabed = Math.min(L.tagline ? L.tagline.y - 6 : lg.y + lg.h + 12, cardsTop - 40 * u);
  const top = Math.max(L.refresh ? L.refresh.y + L.refresh.h + 4 : 8, Math.min(...L.creatures.map((c) => c.y), lg.y) - 4);
  const rand = rng(Math.round(W) * 7919 + Math.round(seabed));
  const keep = 14 * u; // room kept clear around the logo box
  const sides = [
    { lo: 8 * u, hi: lg.x - keep },
    { lo: lg.x + lg.w + keep, hi: W - 8 * u },
  ].filter((s) => s.hi - s.lo > 30 * u);

  const strands = [];
  for (const side of sides) {
    const width = side.hi - side.lo;
    const count = Math.max(2, Math.min(6, Math.round(width / (44 * u))));
    for (let i = 0; i < count; i++) {
      const x = side.lo + ((i + 0.5) * width) / count + (rand() - 0.5) * 10 * u;
      const tall = (seabed - top) * (0.55 + 0.4 * rand());
      const amp = 9 * u + 7 * u * rand();
      // a strand's sway stays inside its side, so none reaches into the logo box
      strands.push({ x, base: seabed, h: tall, width: (4.6 + 2.6 * rand()) * u,
                     segs: strand(x, seabed, tall, amp, side.lo, side.hi, rand), side: side === sides[0] ? 0 : 1 });
    }
  }

  // floats: two or three per strand, at fixed fractions, slightly jittered
  const nodes = [];
  strands.forEach((s, si) => {
    const ts = s.h > 120 * u ? [0.38, 0.64, 0.88] : [0.45, 0.8];
    ts.forEach((t, k) => {
      const p = along(s.segs, Math.min(0.96, t + (rand() - 0.5) * 0.06));
      nodes.push({ id: `n${si}.${k}`, strand: si, level: k, x: r2(p.x), y: r2(p.y), r: r2((3.2 + 1.6 * rand()) * u) });
    });
  });

  // edges: same-level floats of neighbouring strands on one side, then each creature to its nearest float
  const edges = [];
  for (let si = 0; si + 1 < strands.length; si++) {
    if (strands[si].side !== strands[si + 1].side) continue;
    const a = nodes.filter((n) => n.strand === si);
    const b = nodes.filter((n) => n.strand === si + 1);
    for (const na of a) {
      const nb = b.find((n) => n.level === na.level);
      if (nb && Math.abs(na.y - nb.y) < 90 * u) edges.push({ a: na.id, b: nb.id, from: na, to: nb, bow: 0.18 });
    }
  }
  for (const c of L.creatures) {
    const ccx = c.x + c.size / 2, ccy = c.y + c.size / 2;
    const near = nodes.map((n) => ({ n, d: Math.hypot(n.x - ccx, n.y - ccy) })).sort((p, q) => p.d - q.d)[0];
    if (!near || near.d > 4 * c.size) continue;
    // end the tendril at the creature's rim, not its middle (the creature is drawn over it)
    const k = (c.size * 0.42) / (near.d || 1);
    const rim = { x: r2(ccx + (near.n.x - ccx) * k), y: r2(ccy + (near.n.y - ccy) * k) };
    edges.push({ a: near.n.id, b: `creature:${c.name}`, from: near.n, to: rim, bow: 0.22, creature: c.name });
  }

  // the links between the shoals: each solo vine's float to both sides, and one direct tendril. They arc above
  // the logo, ending on the floats of the strands nearest it, and each is checked point by point to stay off it.
  const f = lg.font;
  const nearest = (side) => strands.map((s, i) => ({ s, i })).filter((x) => x.s.side === side)
    .sort((p, q) => (side === 0 ? q.s.x - p.s.x : p.s.x - q.s.x)).slice(0, 2)
    .flatMap((x) => nodes.filter((n) => n.strand === x.i).sort((p, q) => p.y - q.y));
  const clearOf = (cmds) => sampleCurve(cmds).every((p) => !(p.x > lg.x - 4 && p.x < lg.x + lg.w + 4 && p.y > lg.y - 4 && p.y < lg.y + lg.h));
  const archTo = (a, b, lift) => arch(a, b, Math.min(lift, Math.min(a.y, b.y) - top));
  const used = new Set();
  // a float of this side not yet taken whose arc clears the logo; a taken one if it must
  const anchor = (side, to, lift) => {
    const fits = nearest(side).filter((n) => clearOf(archTo(n, to, lift)));
    const n = fits.find((c) => !used.has(c.id)) ?? fits[0];
    if (n) used.add(n.id);
    return n ?? null;
  };
  const bridge = [];
  const links = [];
  const vines = [];
  if (nearest(0).length && nearest(1).length) {
    // the direct link takes the highest float each side, so it clears everything else
    const lift = 1.15 * f;
    const a = anchor(0, { x: lg.x + lg.w / 2, y: top }, lift);
    const b = a && anchor(1, a, lift);
    if (a && b) bridge.push({ a: a.id, b: b.id, from: a, to: b, bridge: true, cmds: archTo(a, b, lift) });
    const letters = logoLetters(lg);
    const tipY = lg.y - 0.17 * f;
    for (const [k, plan] of SOLO_VINES.entries()) {
      const l = letters[plan.letter];
      const at = ([x, y]) => capPoint(l, x, y);
      const tip = { id: `solo.${k}`, strand: -1, level: -1, x: r2(at(plan.tip).x), y: r2(tipY), r: r2(4.2 * u) };
      const pts = [{ x: at(plan.root).x, y: seabed }, ...plan.way.map((w) => ({ ...at(w), over: !!w[2] })), tip];
      const segs = splineSegs(pts);
      const front = segs.filter((_s, i) => pts[i].over);
      vines.push({ id: tip.id, tip, letter: l.ch, segs, front, width: r2(4.4 * u), sway: { amp: r2(1.6 * u), period: 5300 + 900 * k } });
      nodes.push(tip);
    }
    for (const v of vines) {
      for (const side of [0, 1]) {
        const near = (side === 0) === (v.tip.x < lg.x + lg.w / 2);
        const rise = (near ? 0.2 : 0.62) * f;
        const from = anchor(side, v.tip, rise);
        if (from) links.push({ a: from.id, b: v.tip.id, from, to: v.tip, solo: true, side, cmds: archTo(from, v.tip, rise) });
      }
    }
    edges.push(...links, ...bridge);
  }

  const waves = [
    { y: r2(lg.y + lg.h * 0.97), amp: 4 * u, w: 22 * u, width: 2.4 * u, alpha: 0.55 },
    { y: r2(lg.y + lg.h * 0.97 + 9 * u), amp: 3 * u, w: 30 * u, width: 1.6 * u, alpha: 0.3 },
  ].map((w) => ({ ...w, x0: r2(lg.x + lg.w * 0.04), x1: r2(lg.x + lg.w * 0.96) }));
  const bubbles = [0, 1, 2, 3].map((i) => ({
    x: r2(lg.x + lg.w * (0.9 + 0.035 * i)), y: r2(lg.y - (6 + 13 * i) * u), r: r2((2 + i * 0.9) * u),
  })).filter((b) => b.y - b.r > top);

  // leaf blades along each strand, alternating sides, tipped up and kept inside the strand's side
  const leaves = [];
  strands.forEach((s, si) => {
    const side = sides[s.side];
    [0.16, 0.42, 0.68].forEach((t, k) => {
      leaves.push(leafCmds(s.segs, t, (k + si) % 2 === 0 ? 1 : -1, (15 + 8 * rand()) * u, (4.5 + 2 * rand()) * u, side.lo, side.hi));
    });
  });
  // a solo vine's blades are small, and all behind the letters
  const soloLeaves = vines.flatMap((v, vi) => [0.06, 0.82, 0.94].map((t, k) => leafCmds(v.segs, t, (k + vi) % 2 === 0 ? 1 : -1, (10 + 3 * rand()) * u, (3 + rand()) * u, 0, W)));

  const items = [];
  for (const cmds of leaves) items.push({ kind: "leaf", cmds, fill: true, color: SEA.kelp, alpha: 0.6 });
  // strands: a wide dark body, then a light midrib
  strands.forEach((s, i) => {
    const cmds = segCmds(s.segs);
    const sway = { amp: r2(2.5 * u), period: 4200 + 700 * (i % 4) };
    items.push({ kind: "kelp", cmds, width: s.width, color: SEA.kelpDeep, alpha: 0.9, sway });
    items.push({ kind: "kelp", cmds, width: r2(s.width * 0.45), color: SEA.kelp, alpha: 0.75, sway });
    items.push({ kind: "kelp", cmds, width: r2(Math.max(0.8, s.width * 0.14)), color: SEA.kelpLight, alpha: 0.45, sway });
  });
  for (const cmds of soloLeaves) items.push({ kind: "vine-leaf", cmds, fill: true, color: SEA.solo, alpha: 0.7 });
  // a solo vine: all of it goes behind the logo, then its over-crossings go again in front (`front`, drawn after
  // the letters); both copies sway alike, so the weave holds
  const front = [];
  for (const v of vines) {
    const strokes = [[v.width, SEA.soloDeep, 1], [r2(v.width * 0.5), SEA.solo, 1], [r2(Math.max(0.7, v.width * 0.16)), SEA.soloLight, 0.8]];
    for (const [width, color, alpha] of strokes) {
      items.push({ kind: "vine", cmds: segCmds(v.segs), width, color, alpha, sway: v.sway });
      front.push({ kind: "vine", cmds: v.front.flatMap((g) => segCmds([g])), width, color, alpha, sway: v.sway });
    }
  }
  // tendrils: a soft halo under a fine line; one whose curve would cross the logo box is dropped
  const clear = { x0: lg.x - keep / 2, y0: lg.y, x1: lg.x + lg.w + keep / 2, y1: lg.y + lg.h };
  for (let i = edges.length - 1; i >= 0; i--) {
    if (edges[i].cmds) continue;
    const b = itemBounds({ cmds: tendril(edges[i].from, edges[i].to, edges[i].bow) });
    if (b.x0 < clear.x1 && clear.x0 < b.x1 && b.y0 < clear.y1 && clear.y0 < b.y1) edges.splice(i, 1);
  }
  for (const e of edges) {
    const cmds = e.cmds ?? tendril(e.from, e.to, e.bow);
    // a link to a solo vine is mint, the one direct link between the shoals a little stronger than the rest
    const color = e.solo ? SEA.soloLink : SEA.tendril;
    items.push({ kind: "edge", cmds, width: r2(3.5 * u), color, alpha: 0.07 });
    items.push({ kind: "edge", cmds, width: r2(Math.max(0.8, (e.bridge ? 1.5 : 1.1) * u)), color, alpha: e.bridge ? 0.62 : 0.42 });
  }
  // floats: a glow ring, the float, a highlight
  for (const n of nodes) {
    items.push({ kind: "node", cmds: circleCmds(n.x, n.y, n.r * 2.1), fill: true, color: SEA.float, alpha: 0.1 });
    items.push({ kind: "node", cmds: circleCmds(n.x, n.y, n.r), fill: true, color: SEA.float, alpha: 0.85 });
    items.push({ kind: "node", cmds: circleCmds(n.x - n.r * 0.3, n.y - n.r * 0.3, n.r * 0.35), fill: true, color: SEA.floatRing, alpha: 0.8 });
  }
  for (const w of waves) {
    items.push({ kind: "wave", cmds: waveCmds(w.x0, w.x1, w.y, w.amp, w.w), width: w.width, color: SEA.wave, alpha: w.alpha });
  }
  items.push({ kind: "wave", cmds: waveCmds(waves[0].x0, waves[0].x1, waves[0].y - 1.2 * u, waves[0].amp, waves[0].w),
               width: r2(0.9 * u), color: SEA.foam, alpha: 0.5 });
  for (const b of bubbles) items.push({ kind: "bubble", cmds: circleCmds(b.x, b.y, b.r), width: r2(Math.max(0.8, u)), color: SEA.bubble, alpha: 0.55 });

  return { strands, vines, nodes, edges, links, bridge, waves, bubbles, seabed: r2(seabed), top: r2(top), items: mergeItems(items), front: mergeItems(front) };
}

/** `fn`, remembering its last result: a pure scene is only worked out again when its arguments (a layout, say) change,
 * so a poll or a card's status does not redo it, and a resize does. */
export function lastResult(fn) {
  let key = null, value = null;
  return (...args) => {
    const k = JSON.stringify(args);
    if (k !== key) { key = k; value = fn(...args); }
    return value;
  };
}

/** Items of one style (and no sway of their own) become one path of several subpaths: every paint_path is a
 * full-canvas layer, so fewer layers make a cheaper frame, on a CPU renderer above all. Order is kept per style. */
export function mergeItems(items) {
  const out = [];
  const byStyle = new Map();
  for (const it of items) {
    if (it.sway) { out.push(it); continue; }
    const key = [it.kind, it.fill ? "f" : "s", it.width ?? "", it.color, it.alpha].join("|");
    const have = byStyle.get(key);
    if (have) have.cmds = have.cmds.concat(it.cmds);
    else { const m = { ...it, cmds: it.cmds.slice() }; byStyle.set(key, m); out.push(m); }
  }
  return out;
}

/** Bounds of a display-list item's points (controls included, so the box contains the curve). */
export function itemBounds(it) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const add = (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); };
  for (const c of it.cmds) {
    if (c[0] === "M" || c[0] === "L") add(c[1], c[2]);
    else if (c[0] === "C") { add(c[1], c[2]); add(c[3], c[4]); add(c[5], c[6]); }
    else if (c[0] === "A") { add(c[6] - c[1], c[7] - c[2]); add(c[6] + c[1], c[7] + c[2]); }
  }
  return { x0, y0, x1, y1 };
}
