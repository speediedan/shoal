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

import { PALETTE, PASTELS, edgeBuckets, incidentEdges, shortShoal } from "./model.js";
import {
  edgeCurve, curveCmds, arrowHead, polygonCmds, shapeCmds, circleCmds, annularSectorCmds,
  samplesBySpacing, toScreen, withAlpha, mix, clamp,
} from "./geom.js";

const P = PALETTE;
const solid = (color) => ({ kind: "solid", color });

/** The glyph a marching dot shows at frame j: bright head, a short tail behind it. */
export const DOT_GLYPHS = ["●", "•", "·", " ", " "];
export function dotFrames(i, L) {
  return Array.from({ length: L }, (_, j) => DOT_GLYPHS[(((j - i) % L) + L) % L] ?? " ");
}

/** Node radius on screen: grows with zoom, within limits, so dense views stay legible. */
export const screenR = (r, k) => r * clamp(Math.sqrt(k), 0.7, 1.5);

/**
 * Builds the display list. `opts`: {width, height, tf, hover, selected, motion ("full" | "low" |
 * "off"), packets, maxDots}. Returns {items, hits, discs, nodeScreen}.
 */
export function buildScene(vm, lay, opts) {
  const { width, height, tf } = opts;
  const motion = opts.motion ?? "full";
  const animate = motion !== "off";
  const items = [];
  const k = tf.k;
  const S = (p) => toScreen(tf, p);
  const posOf = (id) => lay.pos.get(id);
  const hover = opts.hover && vm.byId.has(opts.hover) ? opts.hover : null;
  const bright = hover ? new Set([hover, ...(vm.neighbors.get(hover) ?? [])]) : null;
  const dimOf = (id) => (bright && !bright.has(id) ? 0.2 : 1);

  // 1. canvas: near-black violet to navy, a cool wash, and a soft bloom behind the hub
  const rect = [["M", 0, 0], ["L", width, 0], ["L", width, height], ["L", 0, height], ["Z"]];
  items.push({ t: "path", id: "bg", fill: true, cmds: rect, bg: { kind: "linear", angle: 165, from: P.bgTop, to: P.bgBottom } });
  items.push({ t: "path", id: "bg-wash", fill: true, cmds: rect, bg: { kind: "linear", angle: 35, from: withAlpha(P.navy, 0.7), to: withAlpha(P.navy, 0) } });
  const hub = vm.nodes.find((n) => n.kind === "user");
  const bloomAt = hub ? S(posOf(hub.id)) : { x: width / 2, y: height / 2 };
  const bloomR = Math.max(width, height) * 0.42;
  [1, 0.72, 0.48, 0.28].forEach((f, i) => {
    items.push({ t: "path", id: `bloom-${i}`, fill: true, cmds: circleCmds(bloomAt.x, bloomAt.y, bloomR * f), bg: solid(withAlpha("#2a2148", 0.05 + i * 0.012)) });
  });

  // 2. regions: cluster discs (global) or host bands (per shoal)
  const discs = [];
  if (vm.global) {
    for (const c of lay.clusters) {
      const p = S(c);
      const r = c.r * k;
      discs.push({ id: c.id, x: p.x, y: p.y, r });
      items.push({ t: "path", id: `disc-${c.id}`, fill: true, cmds: circleCmds(p.x, p.y, r), bg: solid(withAlpha(c.tint, hover ? 0.025 : 0.045)) });
      items.push({ t: "path", id: `disc-glow-${c.id}`, fill: false, width: 5, cmds: circleCmds(p.x, p.y, r), bg: solid(withAlpha(c.tint, 0.05)) });
      items.push({ t: "path", id: `disc-rim-${c.id}`, fill: false, width: 1, cmds: circleCmds(p.x, p.y, r), bg: solid(withAlpha(c.tint, 0.22)) });
      const label = `${c.label.toUpperCase()}  ${c.gen ?? ""}`.trim();
      items.push({ t: "text", id: `disc-label-${c.id}`, x: p.x - 120, y: p.y - r - 22, w: 240, h: 16, text: label, size: 11, color: withAlpha(c.tint, 0.95), align: "center", weight: "semibold" });
      if (c.needsYou > 0) {
        items.push({ t: "text", id: `disc-needs-${c.id}`, x: p.x - 120, y: p.y - r - 8, w: 240, h: 13, text: `${c.needsYou} need${c.needsYou === 1 ? "s" : ""} you`, size: 9.5, color: P.magenta, align: "center" });
      }
    }
  } else {
    const c = lay.clusters[0];
    const o = S({ x: 0, y: 0 });
    for (const b of lay.bands) {
      const tint = b.color ?? PASTELS[(b.index + 2) % PASTELS.length];
      items.push({ t: "path", id: `band-${b.host}`, fill: true, cmds: annularSectorCmds(o.x, o.y, b.r0 * k, b.r1 * k, b.a0, b.a1), bg: solid(withAlpha(tint, 0.055)) });
      items.push({ t: "path", id: `band-rim-${b.host}`, fill: false, width: 1, cmds: annularSectorCmds(o.x, o.y, b.r0 * k, b.r1 * k, b.a0, b.a1), bg: solid(withAlpha(tint, 0.16)) });
      const am = (b.a0 + b.a1) / 2;
      const lp = { x: o.x + (b.r1 * k + 14) * Math.cos(am), y: o.y + (b.r1 * k + 14) * Math.sin(am) };
      items.push({ t: "text", id: `band-label-${b.host}`, x: lp.x - 70, y: lp.y - 7, w: 140, h: 14, text: `${b.home ? "\u2302 " : ""}${b.host}${b.jobsOnly ? " (jobs)" : ""}`, size: 10, color: withAlpha(tint, 0.9), align: "center", weight: "semibold" });
    }
    if (c) discs.push({ id: c.id, x: o.x, y: o.y, r: c.r * k });
  }

  // 2b. physical servers: host markers (global) and tethers from a role to the host running its CI job
  if (vm.global) {
    for (const [h, a] of lay.anchors ?? []) {
      if (!a.marker) continue;
      const p = S(a);
      const color = vm.hostColor.get(h) ?? P.steel2;
      const roles = vm.nodes.filter((n) => n.kind === "role" && n.host === h).length;
      const w = 30 + h.length * 6.6 + 34;
      items.push({ t: "box", id: `host-${h}`, x: p.x - w / 2, y: p.y - 11, w, h: 22, radius: 11, bg: withAlpha(color, 0.1), border: { w: 1, color: withAlpha(color, 0.55) }, opacity: 1 });
      items.push({ t: "box", id: `host-dot-${h}`, x: p.x - w / 2 + 9, y: p.y - 3.5, w: 7, h: 7, radius: 3.5, bg: color, opacity: 1 });
      items.push({ t: "text", id: `host-label-${h}`, x: p.x - w / 2 + 20, y: p.y - 7, w: w - 24, h: 14, text: `${h}  ${roles}`, size: 10, color: withAlpha(color, 0.95), align: "left", weight: "semibold" });
    }
  }
  for (const t of lay.tethers ?? []) {
    if (!t.from) continue;
    const a = S(t.from), b0 = S(t.to);
    const b = lay.anchors?.get(t.host)?.marker ? { x: b0.x, y: b0.y - 12 } : b0;
    const color = vm.hostColor.get(t.host) ?? P.steel2;
    const dim = dimOf(t.id);
    items.push({ t: "path", id: `tether-${t.id}`, fill: false, width: 1, dash: [2, 3], cmds: [["M", a.x, a.y], ["L", b.x, b.y]], bg: solid(withAlpha(color, 0.6 * dim)) });
    items.push({ t: "path", id: `tether-end-${t.id}`, fill: true, cmds: circleCmds(b.x, b.y, 2.5), bg: solid(withAlpha(color, 0.9 * dim)) });
    const m = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    items.push({ t: "text", id: `tether-label-${t.id}`, x: m.x - 70, y: m.y - 14, w: 140, h: 13, text: `${t.event} \u2192 ${t.host}`, size: 9, color: withAlpha(color, 0.85 * dim), align: "center" });
  }

  // 3. edges, batched per color x tier bucket: a glow pass under a core pass, then arrowheads
  const nodeR = new Map(vm.nodes.map((n) => [n.id, screenR(n.visual.r, k)]));
  const curves = new Map();
  for (const e of vm.edges) {
    const a = posOf(e.src), b = posOf(e.dst);
    if (!a || !b) continue;
    curves.set(e.id, edgeCurve(S(a), S(b), { bend: e.bend, trimA: nodeR.get(e.src) + 2, trimB: nodeR.get(e.dst) + 3 }));
  }
  const keep = hover ? incidentEdges(vm, hover) : null;
  for (const bk of edgeBuckets(vm.edges.filter((e) => curves.has(e.id)), keep)) {
    const cmds = bk.edges.flatMap((e) => curveCmds(curves.get(e.id)));
    if (bk.glow > 0) items.push({ t: "path", id: `eg-${bk.key}`, fill: false, width: bk.glow, cmds, bg: solid(withAlpha(bk.color, bk.glowAlpha)) });
    items.push({ t: "path", id: `ec-${bk.key}`, fill: false, width: bk.core, dash: bk.dash, cmds, bg: solid(withAlpha(bk.color, bk.coreAlpha)) });
    if (bk.cls !== "alias") {
      const heads = bk.edges.flatMap((e) => polygonCmds(arrowHead(curves.get(e.id), bk.arrow)));
      items.push({ t: "path", id: `ea-${bk.key}`, fill: true, cmds: heads, bg: solid(withAlpha(bk.color, Math.min(1, bk.coreAlpha + 0.1))) });
    }
  }

  // 4. item satellites: spokes, then dots, batched by color
  const spokes = new Map();
  const satFill = new Map();
  const ownerOf = (s) => vm.byId.get(s.owner);
  for (const s of lay.sats) {
    const o = ownerOf(s);
    if (!o) continue;
    const dim = dimOf(o.id);
    const a = S({ x: s.ax, y: s.ay }), b = S(s);
    const sc = withAlpha(o.tint, 0.5 * dim);
    if (!spokes.has(sc)) spokes.set(sc, []);
    spokes.get(sc).push(["M", a.x, a.y], ["L", b.x, b.y]);
    const color = s.kind === "active" ? withAlpha(o.visual.color, 0.95 * dim) : s.stale ? withAlpha(P.red, 0.85 * dim) : withAlpha(o.tint, (s.kind === "next" ? 0.9 : 0.5) * dim);
    if (!satFill.has(color)) satFill.set(color, []);
    satFill.get(color).push(...circleCmds(b.x, b.y, s.r * clamp(Math.sqrt(k), 0.7, 1.4)));
  }
  let si = 0;
  for (const [color, cmds] of spokes) items.push({ t: "path", id: `spoke-${si++}`, fill: false, width: 1.2, cmds, bg: solid(color) });
  si = 0;
  for (const [color, cmds] of satFill) items.push({ t: "path", id: `sat-${si++}`, fill: true, cmds, bg: solid(color) });

  // 5. flow: marching dots along hot and warm edges, motion native through .frames()
  if (animate) {
    let budget = opts.maxDots ?? (motion === "low" ? 80 : 240);
    for (const e of vm.edges) {
      if (!e.flow || budget <= 0 || !curves.has(e.id)) continue;
      if (keep && !keep.has(e.id)) continue;
      const hot = e.tier === "hot";
      const pts = samplesBySpacing(curves.get(e.id), hot ? 13 : 17, { margin: 7, max: Math.min(budget, 18) });
      const L = Math.max(3, Math.min(pts.length, hot ? 5 : 4));
      pts.forEach((p, i) => {
        items.push({ t: "text", id: `dot-${e.id}-${i}`, x: p.x - 6, y: p.y - 7, w: 12, h: 14, text: dotFrames(i, L)[0], size: hot ? 10 : 8, color: withAlpha(e.color, hot ? 1 : 0.75), align: "center", frames: { list: dotFrames(i, L), ms: e.flowMs } });
      });
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
  for (const n of vm.nodes) {
    const p0 = posOf(n.id);
    if (!p0) continue;
    const v = n.visual;
    const p = S(p0);
    const r = nodeR.get(n.id);
    const dim = dimOf(n.id);
    const op = v.opacity * dim;
    hits.push({ id: n.id, x: p.x, y: p.y, r: Math.max(r, 7) });
    nodeScreen.set(n.id, { x: p.x, y: p.y, r });
    const hs = v.halo?.scale ?? 2.4;
    if (v.halo) {
      const hr = r * hs;
      items.push({
        t: "box", id: `halo-${n.id}`, x: p.x - hr, y: p.y - hr, w: hr * 2, h: hr * 2, radius: hr,
        bg: withAlpha(v.color, 0.16), opacity: animate ? v.halo.to * dim : 0.3 * dim,
        loops: animate && dim === 1 ? [{ prop: "opacity", from: v.halo.from, to: v.halo.to, period: v.halo.period, easing: "ease-in-out" }] : [],
      });
      const ir = r * (1 + (hs - 1) * 0.45);
      items.push({ t: "box", id: `halo-in-${n.id}`, x: p.x - ir, y: p.y - ir, w: ir * 2, h: ir * 2, radius: ir, bg: withAlpha(v.color, 0.14 * dim), opacity: 1 });
    }
    if (v.ring) {
      const rr = r + 6;
      items.push({
        t: "box", id: `ring-${n.id}`, x: p.x - rr, y: p.y - rr, w: rr * 2, h: rr * 2, radius: rr,
        border: { w: 1.5, color: withAlpha(v.color, 0.95) }, opacity: dim,
        loops: animate && dim === 1 ? [{ prop: "opacity", from: 0.15, to: 1, period: v.ring.period, easing: "ease-in-out" }] : [],
      });
    }
    const bodyColor = v.fill > 0 ? withAlpha(v.color, v.fill * op) : withAlpha(P.void, 0.6 * op);
    if (v.shape === "circle") {
      const solidBorder = v.border === "solid" || (v.kind === "user");
      items.push({
        t: "box", id: `body-${n.id}`, x: p.x - r, y: p.y - r, w: r * 2, h: r * 2, radius: r,
        bg: bodyColor, border: solidBorder ? { w: v.borderWidth || 1.5, color: withAlpha(v.color, op) } : null, opacity: 1,
      });
      if (v.fill >= 0.5 && n.kind === "role") {
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
    if (v.spinner && animate) items.push({ t: "spin", id: `spin-${n.id}`, x: p.x - r - 5, y: p.y - r - 5, size: 2 * r + 10, color: withAlpha(v.color, 0.9 * dim), ms: 1400 });
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
  if (animate) {
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
    }
  }

  // 8. labels and badges (screen size, not zoomed)
  for (const n of vm.nodes) {
    const s = nodeScreen.get(n.id);
    if (!s) continue;
    const v = n.visual;
    const dim = dimOf(n.id);
    const loud = v.needsYou || n.id === hover || n.id === opts.selected;
    const isRole = n.kind === "role";
    // the writer and relay keep their shapes in the global view; their labels only add noise there
    if (vm.global && (n.kind === "system" || n.kind === "relay")) continue;
    const text = n.kind === "user" ? "you" : n.kind === "external" ? `${n.label}${n.alias_of ? " ⇄" : ""}` : n.label;
    const lw = 130;
    const ly = s.y + s.r + (v.halo && isRole ? 4 : 3) + (n.items?.length ? 6 : 0);
    items.push({
      t: "text", id: `label-${n.id}`, x: s.x - lw / 2, y: ly, w: lw, h: 14, text,
      size: isRole ? 10.5 : n.kind === "user" ? 11 : 9, align: "center", weight: loud ? "semibold" : null,
      color: withAlpha(loud ? (v.needsYou ? mix(v.color, "#ffffff", 0.45) : P.ink) : isRole ? P.ink : P.dim, (isRole ? 0.92 : 0.75) * dim),
    });
    if (n.hostColor) {
      const tw = Math.min(lw, text.length * (isRole ? 6.3 : 5.4));
      items.push({ t: "box", id: `hosttag-${n.id}`, x: s.x - tw / 2 - 9, y: ly + 4, w: 6, h: 6, radius: 3, bg: withAlpha(n.hostColor, 0.95 * dim), opacity: 1 });
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
  for (const pk of opts.packets ?? []) {
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

  return { items, hits, discs, nodeScreen };
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
