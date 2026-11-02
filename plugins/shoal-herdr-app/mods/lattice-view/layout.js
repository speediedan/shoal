// layout.js: deterministic layout for the shoal lattice. Pure: no Herdr or GPUI imports.
//
// Two steps. First a structured target for every node (global: shoal clusters on a ring around
// the shared `user` hub; per shoal: roles on a ring grouped into host sectors, the system writer
// and wake relay inside, externals outside). Then a small force refinement (anchor springs plus
// collision and short-range repulsion, a compact Fruchterman-Reingold variant with linear
// cooling) that clears overlaps without losing the structure. Starting jitter is seeded by a hash
// of each node id and a warm start reuses the previous positions, so the picture is stable from
// one data tick to the next. Item satellites are placed after the refinement, as short spokes
// fanned outward from each role: the starburst.

import { hash32, rng, clamp } from "./geom.js";

const TAU = Math.PI * 2;
const byLabel = (a, b) => (a.label < b.label ? -1 : a.label > b.label ? 1 : a.id < b.id ? -1 : 1);

function jitter(id, amp) {
  const r = rng(hash32(id));
  return { x: (r() - 0.5) * 2 * amp, y: (r() - 0.5) * 2 * amp };
}

const circMean = (angles) => Math.atan2(angles.reduce((s, a) => s + Math.sin(a), 0), angles.reduce((s, a) => s + Math.cos(a), 0));

/** The radius a node keeps clear around itself, satellites included. */
export function footprint(n) {
  const r = n.visual?.r ?? 8;
  return n.items?.length ? r + 30 : r + 6;
}

/** Spread angles apart so no two are closer than `min` (keeps externals from stacking). */
function spreadAngles(list, min) {
  list.sort((a, b) => a.a - b.a || (a.id < b.id ? -1 : 1));
  for (let pass = 0; pass < 6; pass++) {
    for (let i = 1; i < list.length; i++) {
      const gap = list[i].a - list[i - 1].a;
      if (gap < min) {
        const push = (min - gap) / 2;
        list[i - 1].a -= push;
        list[i].a += push;
      }
    }
  }
  return list;
}

/**
 * Targets and the decorations that come with them (cluster discs in the global view, host bands
 * per shoal). Coordinates are world units, about pixels at zoom 1, centered on the hub.
 */
export function layoutTargets(vm) {
  const targets = new Map();
  const center = new Map(); // node id -> the point its satellites fan away from
  const clusters = [];
  const bands = [];
  const anchors = new Map(); // host name -> where its tethers end
  const nodes = vm.nodes;
  const user = nodes.find((n) => n.kind === "user");
  if (user) targets.set(user.id, { x: 0, y: 0, fixed: true });

  const rolesOf = (sid) => nodes.filter((n) => n.shoal === sid && n.kind === "role");

  if (vm.global) {
    const shoals = vm.shoalsInView;
    const S = Math.max(1, shoals.length);
    const rcs = shoals.map((s) => Math.max(84, 40 + rolesOf(s.id).length * 23));
    const rcMax = Math.max(76, ...rcs);
    const ringR = S === 1 ? rcMax + 190 : Math.max(rcMax + 200, (2 * rcMax + 150) / (2 * Math.sin(Math.PI / S)));
    shoals.forEach((s, i) => {
      const ang = Math.PI + (TAU * i) / S;
      const c = { x: ringR * Math.cos(ang), y: ringR * Math.sin(ang) };
      const rc = rcs[i];
      const toHub = Math.atan2(-c.y, -c.x);
      const roles = rolesOf(s.id).sort((a, b) => (a.host ?? "") < (b.host ?? "") ? -1 : (a.host ?? "") > (b.host ?? "") ? 1 : byLabel(a, b));
      roles.forEach((n, j) => {
        const a = toHub + Math.PI / Math.max(roles.length, 1) + (TAU * j) / Math.max(roles.length, 1);
        targets.set(n.id, { x: c.x + rc * Math.cos(a), y: c.y + rc * Math.sin(a) });
        center.set(n.id, c);
      });
      for (const n of nodes.filter((m) => m.shoal === s.id && m.kind !== "role" && m.kind !== "writer" && m.kind !== "external")) {
        const off = n.kind === "system" ? 0 : n.kind === "relay" ? Math.PI : Math.PI / 2;
        const rr = n.kind === "system" ? rc * 0.34 : rc * 0.3;
        targets.set(n.id, { x: c.x + rr * Math.cos(toHub + off), y: c.y + rr * Math.sin(toHub + off) });
        center.set(n.id, c);
      }
      clusters.push({ id: s.id, label: s.short, gen: s.gen, tint: s.tint, x: c.x, y: c.y, r: rc + 52, urgentColor: s.urgentColor, needsYou: s.needsYou });
    });
    const clusterOf = new Map(clusters.map((c) => [c.id, c]));
    // externals: by their alias home, else beside the cluster they talk to
    const ext = nodes.filter((n) => n.kind === "external" || n.kind === "writer" || (n.kind !== "user" && !n.shoal));
    const placed = [];
    const loose = [];
    for (const n of ext) {
      const home = n.alias_of ? targets.get(n.alias_of) : null;
      const homeNode = n.alias_of ? vm.byId.get(n.alias_of) : null;
      if (home && homeNode) {
        const c = clusterOf.get(homeNode.shoal) ?? { x: 0, y: 0 };
        const a = Math.atan2(home.y - c.y, home.x - c.x) + 0.42;
        const cr = Math.hypot(home.x - c.x, home.y - c.y) + 84;
        placed.push({ id: n.id, c, a, r: cr });
        continue;
      }
      const nb = [...(vm.neighbors.get(n.id) ?? [])].map((id) => vm.byId.get(id)).filter((m) => m && m.shoal && clusterOf.has(m.shoal));
      if (!nb.length) {
        // no traffic in view: a quiet arc above the hub, not a far ring that shrinks the fit
        loose.push(n.id);
        continue;
      }
      const c = clusterOf.get(nb[0].shoal);
      const angs = nb.map((m) => { const t = targets.get(m.id); return Math.atan2(t.y - c.y, t.x - c.x); });
      placed.push({ id: n.id, c, a: circMean(angs), r: c.r + 66 });
    }
    loose.forEach((id, i) => {
      const a = -Math.PI / 2 + (i - (loose.length - 1) / 2) * 0.34;
      placed.push({ id, c: { x: 0, y: 0 }, a, r: ringR * (i % 2 ? 0.78 : 0.54) });
    });
    for (const p of placed) {
      targets.set(p.id, { x: p.c.x + p.r * Math.cos(p.a), y: p.c.y + p.r * Math.sin(p.a) });
      center.set(p.id, p.c);
    }
  } else {
    const sid = vm.view;
    const roles = rolesOf(sid);
    const R = Math.max(160, roles.length * 32);
    const summary = vm.shoalsInView[0];
    const home = summary?.home ?? null;
    // a host that only runs this shoal's CI jobs (state.job_host) still gets a band to tether to
    const jobHosts = roles.map((n) => n.jobHost).filter(Boolean);
    const hosts = [...new Set([...roles.map((n) => n.host ?? "?"), ...jobHosts])].sort((a, b) => (a === home ? -1 : b === home ? 1 : a < b ? -1 : 1));
    const H = hosts.length;
    const weight = (h) => Math.max(roles.filter((n) => (n.host ?? "?") === h).length, 0.7);
    const totalW = hosts.reduce((t, h) => t + weight(h), 0) || 1;
    const gap = H > 1 ? 0.26 : 0;
    const usable = TAU - gap * H;
    let a0 = -Math.PI / 2 + gap / 2;
    const hostIndex = new Map((vm.hosts ?? []).map((h, i) => [h.name, i]));
    for (const h of hosts) {
      const mine = roles.filter((n) => (n.host ?? "?") === h).sort(byLabel);
      const span = (usable * weight(h)) / totalW;
      mine.forEach((n, j) => {
        const a = a0 + (span * (j + 0.5)) / mine.length;
        targets.set(n.id, { x: R * Math.cos(a), y: R * Math.sin(a) });
        center.set(n.id, { x: 0, y: 0 });
      });
      bands.push({ host: h, home: h === home, color: vm.hostColor?.get(h) ?? null, jobsOnly: mine.length === 0, r0: R - 50, r1: R + 62, a0, a1: a0 + span, index: hostIndex.get(h) ?? bands.length });
      const am = a0 + span / 2;
      anchors.set(h, { x: (R + 6) * Math.cos(am), y: (R + 6) * Math.sin(am), band: true });
      a0 += span + gap;
    }
    for (const n of nodes.filter((m) => m.shoal === sid && m.kind !== "role" && m.kind !== "writer" && m.kind !== "external")) {
      const a = n.kind === "system" ? (Math.PI * 3) / 4 : n.kind === "relay" ? Math.PI / 4 : -Math.PI / 2;
      targets.set(n.id, { x: R * 0.4 * Math.cos(a), y: R * 0.4 * Math.sin(a) });
      center.set(n.id, { x: 0, y: 0 });
    }
    const ext = nodes.filter((n) => n.kind === "external" || n.kind === "writer");
    const list = ext.map((n) => {
      const angs = [...(vm.neighbors.get(n.id) ?? [])].map((id) => targets.get(id)).filter(Boolean).map((t) => Math.atan2(t.y, t.x));
      return { id: n.id, a: angs.length ? circMean(angs) : (hash32(n.id) / 4294967296) * TAU };
    });
    for (const p of spreadAngles(list, 0.38)) {
      targets.set(p.id, { x: R * 1.62 * Math.cos(p.a), y: R * 1.62 * Math.sin(p.a) });
      center.set(p.id, { x: 0, y: 0 });
    }
    clusters.push({ id: sid, label: summary?.short ?? sid, gen: summary?.gen ?? "", tint: summary?.tint ?? "#6a8592", x: 0, y: 0, r: R + 70, urgentColor: summary?.urgentColor, needsYou: summary?.needsYou ?? 0 });
  }
  // anything left (a node of an unexpected kind) gets a seeded spot on an outer ring
  for (const n of nodes) {
    if (targets.has(n.id)) continue;
    const a = (hash32(n.id) / 4294967296) * TAU;
    targets.set(n.id, { x: 420 * Math.cos(a), y: 420 * Math.sin(a) });
    center.set(n.id, { x: 0, y: 0 });
  }
  // Global view: hosts show as per-node dots and the rail's host block, not as places on the canvas
  // (a host pill under a cluster reads as part of it), so CI tethers there have no anchor.
  return { targets, center, clusters, bands, anchors };
}

/**
 * The incremental layout kept in mod state. `update(vm)` sets new targets and keeps positions;
 * `step(n)` runs n refinement iterations; `settled` says when to stop notifying.
 */
export class Layout {
  constructor() {
    this.pos = new Map();
    this.iter = 0;
    this.iters = 0;
    this.t0 = 0;
    this.sig = "";
    this.meta = null;
    this.vm = null;
  }

  update(vm) {
    const meta = layoutTargets(vm);
    const sig = vm.view + "|" + vm.nodes.map((n) => n.id).join(",");
    const cold = this.pos.size === 0;
    const changed = sig !== this.sig;
    const keep = new Map();
    for (const n of vm.nodes) {
      const prev = this.pos.get(n.id);
      const t = meta.targets.get(n.id);
      if (prev) keep.set(n.id, { x: prev.x, y: prev.y });
      else {
        const j = t.fixed ? { x: 0, y: 0 } : jitter(n.id, 14);
        keep.set(n.id, { x: t.x + j.x, y: t.y + j.y });
      }
    }
    this.pos = keep;
    this.meta = meta;
    this.vm = vm;
    this.sig = sig;
    this.iter = 0;
    this.iters = cold ? 260 : changed ? 150 : 30;
    this.t0 = cold ? 42 : changed ? 26 : 2.5;
    return this;
  }

  get settled() {
    return this.iter >= this.iters;
  }

  step(n = 1) {
    const vm = this.vm;
    if (!vm) return true;
    const ids = vm.nodes.map((m) => m.id);
    const nodes = vm.nodes;
    const T = this.meta.targets;
    const N = ids.length;
    const foot = nodes.map(footprint);
    for (let s = 0; s < n && this.iter < this.iters; s++, this.iter++) {
      const temp = this.t0 * (1 - this.iter / this.iters) + 0.02;
      const dx = new Float64Array(N);
      const dy = new Float64Array(N);
      const P = ids.map((id) => this.pos.get(id));
      for (let i = 0; i < N; i++) {
        const t = T.get(ids[i]);
        dx[i] += (t.x - P[i].x) * 0.1;
        dy[i] += (t.y - P[i].y) * 0.1;
      }
      for (let i = 0; i < N; i++) {
        for (let j = i + 1; j < N; j++) {
          let ex = P[i].x - P[j].x;
          let ey = P[i].y - P[j].y;
          let d = Math.hypot(ex, ey);
          if (d < 1e-6) {
            // coincident: separate along a direction seeded by the pair
            const a = (hash32(ids[i] + "|" + ids[j]) / 4294967296) * TAU;
            ex = Math.cos(a); ey = Math.sin(a); d = 1e-6;
          }
          const min = foot[i] + foot[j] + 8;
          if (d < min) {
            const f = ((min - d) / d) * 0.5;
            dx[i] += ex * f; dy[i] += ey * f;
            dx[j] -= ex * f; dy[j] -= ey * f;
          }
        }
      }
      for (let i = 0; i < N; i++) {
        if (T.get(ids[i]).fixed) {
          this.pos.set(ids[i], { x: T.get(ids[i]).x, y: T.get(ids[i]).y });
          continue;
        }
        const len = Math.hypot(dx[i], dy[i]);
        const k = len > temp ? temp / len : 1;
        this.pos.set(ids[i], { x: P[i].x + dx[i] * k, y: P[i].y + dy[i] * k });
      }
    }
    return this.settled;
  }

  run() {
    while (!this.settled) this.step(32);
    return this;
  }

  /** Positions, satellites and decorations, ready for the scene. */
  result() {
    const vm = this.vm;
    const pos = new Map();
    for (const [id, p] of this.pos) pos.set(id, { x: p.x, y: p.y });
    const sats = [];
    for (const n of vm?.nodes ?? []) {
      if (!n.items?.length) continue;
      const p = pos.get(n.id);
      const c = this.meta.center.get(n.id) ?? { x: 0, y: 0 };
      let dir = Math.atan2(p.y - c.y, p.x - c.x);
      if (!Number.isFinite(dir) || (Math.abs(p.x - c.x) < 1e-6 && Math.abs(p.y - c.y) < 1e-6)) dir = -Math.PI / 2;
      const k = n.items.length;
      const spread = Math.min(2.6, 0.44 * (k - 1));
      const r = n.visual?.r ?? 11;
      n.items.forEach((it, j) => {
        const a = k > 1 ? dir - spread / 2 + (spread * j) / (k - 1) : dir;
        const len = r + 12 + (it.kind === "active" ? 2 : it.kind === "next" ? 9 + 5 * (j % 2) : 16 + 4 * (j % 2));
        sats.push({
          id: `${n.id}#${j}`,
          owner: n.id,
          kind: it.kind,
          item: it.item,
          url: it.url ?? null,
          stale: it.stale === true,
          x: p.x + len * Math.cos(a),
          y: p.y + len * Math.sin(a),
          ax: p.x + (r + 2) * Math.cos(a),
          ay: p.y + (r + 2) * Math.sin(a),
          r: it.kind === "active" ? 4.2 : it.kind === "next" ? 3.3 : 2.6,
        });
      });
    }
    const tethers = [];
    for (const n of vm?.nodes ?? []) {
      const a = n.jobHost ? this.meta.anchors.get(n.jobHost) : null;
      // to: null means "no region for that host in this view": the scene draws a chip at the node
      if (n.jobHost) tethers.push({ id: n.id, host: n.jobHost, event: n.state?.event ?? "ci", from: pos.get(n.id), to: a ? { x: a.x, y: a.y } : null, dir: Math.atan2(pos.get(n.id).y - (this.meta.center.get(n.id)?.y ?? 0), pos.get(n.id).x - (this.meta.center.get(n.id)?.x ?? 0)) });
    }
    const anchors = this.meta?.anchors ?? new Map();
    return { pos, sats, tethers, anchors, clusters: this.meta?.clusters ?? [], bands: this.meta?.bands ?? [], targets: this.meta?.targets ?? new Map() };
  }
}

/** One-shot layout to rest, warm-started from `prev` (a Map id -> {x, y}) when given. */
export function layoutGraph(vm, prev = null) {
  const l = new Layout();
  if (prev) {
    for (const [id, p] of prev) l.pos.set(id, { x: p.x, y: p.y });
    l.sig = vm.view + "|" + vm.nodes.map((n) => n.id).join(",");
  }
  l.update(vm);
  l.run();
  return l.result();
}

export { clamp };
