// model.js: the shoal graph doc v1 to a view model. Pure: no Herdr or GPUI imports.
//
// The doc is additive: unknown fields are ignored and a missing optional field is treated as
// absent, never as an error. Everything the renderer needs to know about how a state looks
// (color, fill, halo, hatch, border, badge, urgency) is decided here, so main.js and the SVG
// preview draw the same encoding.

import { hash32, withAlpha } from "./geom.js";

// Canvas palette: the neuron-explorer void and steel, the style reference's near-black violet,
// saturated hues for state and pastel tints for identity (so state always reads first).
export const PALETTE = {
  void: "#04050a",
  bgTop: "#14111a",
  bgBottom: "#03050b",
  navy: "#0a1222",
  panel: "#090b0dbd",
  panelSolid: "#0b0e12",
  steel0: "#1c2a30",
  steel1: "#3c4a52",
  steel2: "#6a8592",
  ink: "#d4d4d4",
  dim: "#8c8c8c",
  faint: "#555555",
  teal: "#2ee6c0",
  magenta: "#ff4f9a",
  auth: "#ff5a2c",
  blue: "#4ea3ff",
  violet: "#a58bff",
  amber: "#ffb547",
  slate: "#56636e",
  red: "#ff4d5e",
  cyan: "#38c8ff",
  mint: "#50d090",
  olive: "#c8c070",
  lavender: "#a898d8",
};

/** Identity tints (shoal clusters, role satellites): the style reference's pastels. */
export const PASTELS = ["#b0b070", "#50d090", "#70b090", "#d098b0", "#a898d8", "#88c8d8", "#d8b088"];

const P = PALETTE;

// role-time vocabulary. Encodings key on class + event; a group only decides when the event is
// empty. Policy versions add events, so an event we do not know is a neutral "other" wait and a
// class we do not know is "unknown": never an error.
const EVENT_KEY = {
  "user-auth": "auth",
  "user-ask": "ask",
  "user-prompt": "prompt",
  "user-design": "design",
  assignment: "assign",
  sources: "sources",
  "local-ci": "local-ci",
  "remote-ci": "remote-ci",
  lease: "lease",
  upstream: "upstream",
  "session-blocked": "peer",
  self: "self",
  handover: "handover",
  refill: "refill",
  hold: "hold",
};
const GROUP_KEY = { human: "ask", system: "system", upstream: "upstream", peer: "peer", self: "self" };

const SOFT_MAGENTA = "#d985b6";
/**
 * How each state looks. `urgency` orders the needs-you rail (lower is louder); null means the
 * state never asks for the operator. `hatch` is the pattern_slash interval (px), `badge` a text
 * glyph (no vendor logos).
 */
export const STATE_STYLE = {
  auth: { label: "needs auth", color: P.auth, fill: 0.95, halo: { period: 640, from: 0.25, to: 0.85, scale: 3.0 }, ring: { period: 640 }, badge: "⚷", urgency: 0, needsYou: true },
  ask: { label: "asks you", color: P.magenta, fill: 0.92, halo: { period: 900, from: 0.18, to: 0.7, scale: 2.7 }, ring: { period: 900 }, badge: "?", urgency: 1, needsYou: true },
  prompt: { label: "waits at a prompt", color: P.magenta, fill: 0.92, halo: { period: 900, from: 0.18, to: 0.7, scale: 2.7 }, ring: { period: 900 }, badge: "⏎", urgency: 1, needsYou: true },
  design: { label: "design call", color: P.magenta, fill: 0.9, halo: { period: 1100, from: 0.15, to: 0.6, scale: 2.6 }, ring: { period: 1100 }, badge: "✎", urgency: 2, needsYou: true },
  assign: { label: "queue empty: assign", color: SOFT_MAGENTA, fill: 0.55, halo: { period: 1700, from: 0.08, to: 0.36, scale: 2.3 }, badge: "∅", urgency: 3, needsYou: true },
  sources: { label: "queue empty: sources", color: SOFT_MAGENTA, fill: 0.55, halo: { period: 1700, from: 0.08, to: 0.36, scale: 2.3 }, badge: "↧", urgency: 3, needsYou: true },
  working: { label: "working", color: P.teal, fill: 0.9, halo: { period: 2800, from: 0.1, to: 0.42, scale: 2.4 }, urgency: null },
  "local-ci": { label: "local CI", color: "#4e8cff", fill: 0.3, hatch: 4, spinner: true, border: "solid", badge: "▣", urgency: null },
  "remote-ci": { label: "remote CI", color: "#5cc8ff", fill: 0.22, hatch: 7, spinner: true, border: "solid", badge: "☁", urgency: null },
  lease: { label: "waits for a lease", color: "#7c8cff", fill: 0.3, hatch: 2.6, border: "solid", badge: "⧖", urgency: null },
  system: { label: "waits on system", color: P.blue, fill: 0.28, hatch: 4, border: "solid", urgency: null },
  upstream: { label: "waits upstream", color: P.violet, fill: 0.26, hatch: 5, border: "solid", badge: "↑", urgency: null },
  peer: { label: "blocked on a peer", color: P.amber, fill: 0.22, border: "dashed", badge: "⊘", urgency: null },
  self: { label: "ready between turns", color: P.teal, fill: 0, border: "solid", urgency: null },
  handover: { label: "handing over", color: P.lavender, fill: 0.35, border: "dashed", badge: "⇆", urgency: null },
  refill: { label: "refilling", color: P.mint, fill: 0.3, border: "dashed", badge: "↺", urgency: null },
  hold: { label: "on hold", color: "#9aa7b0", fill: 0.3, border: "solid", badge: "∥", urgency: null },
  other: { label: "other wait", color: "#8fa3ad", fill: 0.25, border: "dashed", badge: "…", urgency: null },
  idle: { label: "idle", color: P.slate, fill: 0.75, urgency: null },
  excluded: { label: "excluded", color: P.steel2, fill: 0, border: "solid", opacity: 0.42, urgency: null },
  unknown: { label: "unknown", color: P.red, fill: 0.12, border: "dotted", badge: "✕", urgency: 4, stall: true },
};

/** Loudness for picking a shoal's most urgent state (pulse dot, cluster tint). */
export const STATE_RANK = ["auth", "ask", "prompt", "design", "assign", "sources", "unknown", "peer", "local-ci", "remote-ci", "lease", "system", "upstream", "handover", "refill", "hold", "other", "working", "self", "idle", "excluded"];

/** Per-host palette for host tags, bands and tethers (distinct from state hues). */
export const HOST_COLORS = ["#9ad1ff", "#f2a7c6", "#b7e08a", "#e6c08f", "#c3a6ff", "#8fe0d2", "#f0e68c"];

export const EDGE_COLOR = {
  msg: "#7fb6cc",
  broadcast: P.cyan,
  ask: P.magenta,
  health: P.amber,
  offer: P.mint,
  grant: P.olive,
  handoff: P.lavender,
  "blocked-on": P.red,
  alias: "#8c8c8c",
};

/** Bend of each edge class, so parallel classes between one pair do not overlap. */
export const EDGE_BEND = { msg: 0.12, ask: 0.16, health: 0.14, offer: 0.2, grant: 0.24, handoff: 0.2, "blocked-on": 0.32, alias: 0.0 };

/** Stroke style per tier. glow: the wide low-alpha under-stroke; core: the thin bright one. */
export const TIER_STYLE = {
  hot: { core: 1.8, coreAlpha: 0.95, glow: 6.5, glowAlpha: 0.22, arrow: 6.5 },
  warm: { core: 1.3, coreAlpha: 0.7, glow: 4.5, glowAlpha: 0.12, arrow: 5.5 },
  cold: { core: 0.9, coreAlpha: 0.32, glow: 0, glowAlpha: 0, arrow: 4.5 },
  struct: { core: 1.3, coreAlpha: 0.85, glow: 4, glowAlpha: 0.1, arrow: 5.5 },
  stale: { core: 1, coreAlpha: 0.35, glow: 0, glowAlpha: 0, arrow: 4.5 },
  fade: { core: 0.8, coreAlpha: 0.1, glow: 0, glowAlpha: 0, arrow: 4 },
};
export const EDGE_DASH = { "blocked-on": [5, 4], alias: [2, 4] };
/** Cross-host traffic: a long subtle dash on the core stroke. */
export const CROSS_HOST_DASH = [9, 3];
const FLOW_CLASSES = new Set(["msg", "ask", "health", "offer", "grant", "handoff"]);

const num = (v, d = 0) => (typeof v === "number" && Number.isFinite(v) ? v : d);
const arr = (v) => (Array.isArray(v) ? v : []);
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});
const str = (v) => (typeof v === "string" ? v : v == null ? "" : String(v));

/** "alpha-20261101-000000" -> "alpha". */
export const shortShoal = (id) => str(id).replace(/-\d{8}-\d{6}$/, "");

/** Seconds as "40s", "25m", "1h", "3d". */
export function fmtAge(s) {
  if (typeof s !== "number" || !Number.isFinite(s)) return "?";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 360) / 10}h`.replace(".0h", "h");
  return `${Math.round(s / 8640) / 10}d`.replace(".0d", "d");
}

/** The state key of a node, from its role-time class and event. Never throws. */
export function stateKey(node) {
  const s = obj(node?.state);
  const cls = str(s.class);
  const ev = str(s.event);
  const grp = str(s.group);
  if (cls === "work") return "working";
  if (cls === "wait") {
    if (Object.prototype.hasOwnProperty.call(EVENT_KEY, ev)) return EVENT_KEY[ev];
    if (!ev && Object.prototype.hasOwnProperty.call(GROUP_KEY, grp)) return GROUP_KEY[grp];
    return "other";
  }
  if (cls === "idle") return "idle";
  if (cls === "excluded") return "excluded";
  return "unknown";
}

const SHAPE_BY_ECOSYSTEM = { claude: "circle", codex: "hexagon", opencode: "diamond" };

/** A role's identity tint: a pastel picked by the hash of its id. */
export const roleTint = (id) => PASTELS[hash32(id) % PASTELS.length];

/** Grants waiting on the operator for a role (`work.grants_pending`, optional). */
export const grantsOf = (node) => arr(obj(node?.work).grants_pending).filter((g) => g && typeof g === "object");

/** Everything the renderer needs to draw one node. */
export function nodeVisual(node) {
  const kind = str(node?.kind) || "role";
  const key = stateKey(node);
  const metrics = obj(node?.metrics);
  if (kind === "user") {
    const work = obj(node.work);
    const grants = Math.max(num(work.grants_pending), arr(work.grants_pending).length);
    const asks = num(work.asks_open) + grants;
    return { kind, key: "hub", shape: "circle", r: 22, color: asks > 0 ? P.magenta : P.ink, fill: 0.06, border: "solid", borderWidth: 2, halo: { period: 3600, from: 0.08, to: 0.28, scale: 2.0 }, badge: asks > 0 ? String(asks) : null, grants, urgency: null, needsYou: false, opacity: 1 };
  }
  if (kind === "system") {
    const pages = num(metrics.pages_24h);
    return { kind, key: "writer", shape: "square", r: 6.5, color: pages > 0 ? P.amber : P.steel2, fill: 0.85, urgency: null, needsYou: false, opacity: 0.9, badge: null, grants: 0 };
  }
  if (kind === "relay") {
    return { kind, key: "relay", shape: "triangle", r: 7, color: key === "working" ? P.teal : P.steel2, fill: 0.8, urgency: null, needsYou: false, opacity: 0.85, badge: null, grants: 0 };
  }
  if (kind === "writer") {
    // an unknown or removed ledger writer: a small dashed node, quieter than an external
    return { kind, key: "writer-unknown", shape: "circle", r: 6, color: "#7a8a94", fill: 0.04, border: "dashed", urgency: null, needsYou: false, opacity: 0.7, badge: null, grants: 0 };
  }
  if (kind === "external") {
    return { kind, key: "external", shape: "circle", r: 8, color: node.alias_of ? P.lavender : "#8fa3ad", fill: 0.05, border: "dashed", urgency: null, needsYou: false, opacity: 0.8, badge: null, grants: 0 };
  }
  const st = STATE_STYLE[key] ?? STATE_STYLE.unknown;
  const done = num(metrics.done_24h);
  const needsYou = Boolean(st.needsYou || obj(node?.state).needs_you === true);
  return {
    kind: "role",
    key,
    shape: SHAPE_BY_ECOSYSTEM[node?.ecosystem] ?? "circle",
    r: 11 + Math.min(4, done * 0.8),
    color: st.color,
    fill: st.fill,
    border: st.border ?? null,
    borderWidth: st.border ? 1.6 : 0,
    hatch: st.hatch ?? 0,
    spinner: Boolean(st.spinner),
    halo: st.halo ?? null,
    ring: st.ring ?? null,
    badge: st.badge ?? null,
    urgency: st.urgency ?? (needsYou ? 3 : null),
    needsYou,
    stall: Boolean(st.stall),
    opacity: st.opacity ?? 1,
    label: key === "other" ? `other wait (${str(obj(node?.state).event) || "?"})` : st.label,
    grants: grantsOf(node).length,
  };
}

/** Every encoding, for the legend (toggle `l`). Hosts come from the view model. */
export function legendEntries(vm = null) {
  const states = Object.entries(STATE_STYLE).map(([key, s]) => ({
    key, label: key === "other" ? "other (unknown wait event)" : s.label, color: s.color, badge: s.badge ?? null,
    style: s.hatch ? "hatch" : s.border === "dashed" ? "dashed" : s.border === "dotted" ? "dotted" : s.fill === 0 ? "outline" : "fill",
    pulse: Boolean(s.ring), needsYou: Boolean(s.needsYou),
  }));
  const shapes = [["circle", "claude"], ["hexagon", "codex"], ["diamond", "opencode"], ["square", "shoal writer"], ["triangle", "wake relay"], ["ring", "you (operator hub)"], ["dashed", "external writer"]];
  const edges = Object.entries(EDGE_COLOR).map(([cls, color]) => ({ cls, color, dash: EDGE_DASH[cls] ?? null }));
  edges.push({ cls: "cross-host", color: "#7fb6cc", dash: CROSS_HOST_DASH });
  const hosts = vm ? [...vm.hostColor.entries()].map(([name, color]) => ({ name, color })) : [];
  return { states, shapes, edges, hosts, extra: [["grant pending", P.olive, "⚑"], ["CI job on another host", "#9ad1ff", "→"]] };
}


/** Edge recency tier from its counts and ages against the doc's windows. */
export function edgeTier(edge, windows = {}) {
  const cls = edge?.class;
  if (edge?.structural === true || cls === "blocked-on" || cls === "alias") return edge?.stale === true ? "stale" : "struct";
  const rateS = num(windows.rate_s, 300);
  const recentS = num(windows.recent_s, 3600);
  const age = typeof edge?.last_age_s === "number" ? edge.last_age_s : null;
  if (num(edge?.n_rate) > 0 || (age !== null && age <= rateS)) return "hot";
  if (num(edge?.n_1h) > 0 || (age !== null && age <= recentS)) return "warm";
  return "cold";
}

/** Frame interval (ms) for an edge's marching dots: faster with more traffic. */
export const flowMs = (ratePerH) => Math.round(Math.min(420, Math.max(90, 760 / (1 + Math.log2(1 + Math.max(0, num(ratePerH)))))));

/**
 * Edges grouped by color x opacity tier: one glow path and one core path each, since a path has
 * one Background. `fadeFor`: a set of edge ids to keep bright (the hovered node's); the rest fade.
 */
/** Messages per hour as a width step: 0 under 1/h, 1 under 4/h, 2 above. */
export const rateBand = (rate) => (num(rate) >= 4 ? 2 : num(rate) >= 1 ? 1 : 0);

export function edgeBuckets(edges, keepBright = null) {
  const buckets = new Map();
  for (const e of edges) {
    const tier = keepBright && !keepBright.has(e.id) ? "fade" : e.tier;
    const cross = Boolean(e.cross) && !EDGE_DASH[e.cls];
    // static rate encoding: busier edges draw wider and brighter, so rate reads without motion
    const band = tier === "fade" ? 0 : rateBand(e.rate);
    const key = `${e.cls}|${tier}${cross ? "|x" : ""}${band ? `|r${band}` : ""}`;
    let b = buckets.get(key);
    if (!b) {
      const st = { ...(TIER_STYLE[tier] ?? TIER_STYLE.cold) };
      st.core += band * 0.55;
      st.coreAlpha = Math.min(1, st.coreAlpha + band * 0.08);
      if (st.glow) st.glow += band * 1.2;
      b = { key, cls: e.cls, tier, cross, band, color: e.color, dash: EDGE_DASH[e.cls] ?? (cross ? CROSS_HOST_DASH : null), ...st, edges: [] };
      buckets.set(key, b);
    }
    b.edges.push(e);
  }
  // draw order: faint first, hot last
  const order = { fade: 0, cold: 1, stale: 1, struct: 2, warm: 3, hot: 4 };
  return [...buckets.values()].sort((a, b) => order[a.tier] - order[b.tier] || (a.key < b.key ? -1 : 1));
}

/** Host name -> tag color: the doc's `hosts` order first, then any other host named by a node. */
export function hostColors(doc) {
  const names = [];
  for (const h of arr(doc?.hosts)) { const n = str(h?.name); if (n && !names.includes(n)) names.push(n); }
  const extra = new Set();
  for (const n of arr(doc?.nodes)) {
    for (const h of [n?.host, obj(n?.state).job_host]) if (typeof h === "string" && h && !names.includes(h)) extra.add(h);
  }
  names.push(...[...extra].sort());
  return new Map(names.map((n, i) => [n, HOST_COLORS[i % HOST_COLORS.length]]));
}

/** A role's CI job host when it differs from where the role runs (`state.job_host`, optional). */
export function jobHostOf(node) {
  const st = obj(node?.state);
  const jh = typeof st.job_host === "string" && st.job_host ? st.job_host : obj(st.job).host;
  return typeof jh === "string" && jh && jh !== node?.host ? jh : null;
}

/** One row per host for the rail: roles working and waiting there, and msgs/h through them. */
export function hostSummaries(vm) {
  const rows = new Map();
  for (const [name, color] of vm.hostColor) rows.set(name, { name, color, home: false, roles: 0, working: 0, waiting: 0, needsYou: 0, msgsPerH: 0, jobsIn: 0 });
  for (const h of vm.hosts) if (rows.has(h?.name)) rows.get(h.name).home = h.home === true;
  const fed = new Map(vm.hosts.filter((h) => h && typeof h.roles === "number").map((h) => [h.name, h]));
  for (const n of vm.nodes) {
    if (n.kind !== "role") continue;
    const r = rows.get(n.host);
    if (r) {
      r.roles += 1;
      if (n.visual.key === "working") r.working += 1;
      else if (str(n.state.class) === "wait") r.waiting += 1;
      if (n.visual.needsYou) r.needsYou += 1;
      r.msgsPerH += num(n.metrics.msgs_out_1h) + num(n.metrics.msgs_in_1h);
    }
    const jh = jobHostOf(n);
    if (jh && rows.has(jh)) rows.get(jh).jobsIn += 1;
  }
  // the feed's own per-host counts win where it gives them (they cover every shoal, not this view)
  for (const r of rows.values()) {
    const h = fed.get(r.name);
    if (!h) continue;
    r.roles = num(h.roles, r.roles);
    r.working = num(h.working, r.working);
    r.waiting = num(h.waiting, r.waiting);
    r.needsYou = num(h.needs_you, r.needsYou);
    r.msgsPerH = num(h.msgs_1h, r.msgsPerH);
  }
  return [...rows.values()].filter((r) => r.roles > 0 || r.jobsIn > 0);
}

/** Satellite items of a role: active, next and recently done, for the starburst layer. */
export function roleItems(node, caps = { next: 4, done: 5 }) {
  const work = obj(node?.work);
  const out = [];
  const active = work.active && typeof work.active === "object" ? work.active : null;
  if (active?.item) out.push({ kind: "active", item: active.item, url: active.url ?? null });
  for (const n of arr(work.next).slice(0, caps.next)) if (n?.item) out.push({ kind: "next", item: n.item, url: n.url ?? null, blocker: n.blocker ?? null, stale: n.stale === true });
  for (const d of arr(work.done_recent).slice(-caps.done)) if (d?.item) out.push({ kind: "done", item: d.item, url: d.url ?? null, ts: d.ts ?? null });
  return out;
}

/** Needs-you ordering: auth, then asks and prompts, then design, then assignment, then stalls. */
export function needsYouList(nodes) {
  const rows = [];
  for (const n of nodes) {
    if ((n.kind ?? "role") !== "role") continue;
    const v = n.visual ?? nodeVisual(n);
    if (!v.needsYou && !v.stall) continue;
    const s = obj(n.state);
    const asks = arr(n.work?.asks);
    rows.push({
      id: n.id,
      label: n.label ?? n.slug ?? n.id,
      shoal: n.shoal ?? null,
      key: v.key,
      color: v.color,
      badge: v.badge,
      rank: v.urgency ?? 9,
      since_s: num(s.since_s, 0),
      text: asks[0] ?? (s.event ? `${s.class}:${s.event}` : s.class ?? ""),
    });
  }
  rows.sort((a, b) => a.rank - b.rank || b.since_s - a.since_s || (a.id < b.id ? -1 : 1));
  return rows;
}

/** One row per live shoal for the rail and the pulse: metrics plus the loudest state. */
export function shoalSummaries(doc) {
  const shoals = arr(doc?.shoals).filter((s) => s && s.id && s.retired !== true);
  shoals.sort((a, b) => (a.id < b.id ? -1 : 1));
  const nodes = arr(doc?.nodes);
  return shoals.map((s, i) => {
    const roles = nodes.filter((n) => n?.shoal === s.id && (n.kind ?? "role") === "role");
    const keys = roles.map((n) => stateKey(n));
    let urgent = "idle";
    for (const k of STATE_RANK) if (keys.includes(k)) { urgent = k; break; }
    const m = obj(s.metrics);
    const needs = needsYouList(roles);
    return {
      id: s.id,
      short: shortShoal(s.id),
      gen: s.gen ?? "",
      home: s.home ?? null,
      tint: PASTELS[i % PASTELS.length],
      urgent,
      urgentColor: (STATE_STYLE[urgent] ?? STATE_STYLE.idle).color,
      needsYou: needs.filter((r) => r.rank < 4).length,
      grants: Math.max(num(s.grants_pending_n), roles.reduce((t, n) => t + grantsOf(n).length, 0)),
      stalls: needs.filter((r) => r.rank >= 4).length,
      roles: num(m.roles, roles.length),
      working: num(m.working, keys.filter((k) => k === "working").length),
      waiting: num(m.waiting),
      msgsPerH: num(m.msgs_1h),
      msgs24: num(m.msgs_24h),
      done24: num(m.done_24h),
      done7: num(m.done_7d),
      asks: num(m.asks_open),
      pages24: num(m.pages_24h),
      workShare: typeof m.work_ratio_24h === "number" ? m.work_ratio_24h : null,
      offline: obj(s.mode).offline === true,
      freeze: obj(s.mode).freeze === true,
    };
  });
}

/**
 * The view model for one view: "global" (every shoal, alias edges linking clusters) or a shoal id
 * (that constellation, its externals, the user hub; alias edges left out).
 */
export function buildModel(doc, view = "global", opts = {}) {
  const items = opts.items !== false;
  const windows = obj(doc?.windows);
  const shoals = shoalSummaries(doc);
  const tintOf = new Map(shoals.map((s) => [s.id, s.tint]));
  const hostColor = hostColors(doc);
  const live = new Set(shoals.map((s) => s.id));
  const all = arr(doc?.nodes).filter((n) => n && typeof n.id === "string");
  const global = view === "global" || !live.has(view);
  const viewId = global ? "global" : view;

  const rawEdges = arr(doc?.edges).filter((e) => e && typeof e.src === "string" && typeof e.dst === "string");
  let keep;
  if (global) {
    keep = all.filter((n) => n.kind === "user" || n.kind === "external" || n.shoal == null || live.has(n.shoal));
  } else {
    const mine = new Set(all.filter((n) => n.shoal === viewId).map((n) => n.id));
    const ext = new Set();
    for (const e of rawEdges) {
      if (mine.has(e.src) && !mine.has(e.dst)) ext.add(e.dst);
      if (mine.has(e.dst) && !mine.has(e.src)) ext.add(e.src);
    }
    keep = all.filter((n) => mine.has(n.id) || n.kind === "user" || (ext.has(n.id) && (n.kind === "external" || n.kind === "writer")));
  }
  keep.sort((a, b) => (a.id < b.id ? -1 : 1));
  const byId = new Map();
  const nodes = keep.map((n) => {
    const visual = nodeVisual(n);
    const vn = {
      id: n.id,
      kind: n.kind ?? "role",
      shoal: n.shoal ?? null,
      label: n.label ?? n.slug ?? n.id,
      slug: n.slug ?? null,
      ecosystem: n.ecosystem ?? null,
      model: n.model ?? null,
      host: typeof n.host === "string" ? n.host : null,
      hostColor: hostColor.get(n.host) ?? null,
      jobHost: jobHostOf(n),
      grants: grantsOf(n),
      alias_of: n.alias_of ?? null,
      state: obj(n.state),
      work: obj(n.work),
      metrics: obj(n.metrics),
      viewArgv: Array.isArray(n.view) ? n.view : null,
      tint: n.kind === "role" ? roleTint(n.id) : tintOf.get(n.shoal) ?? PALETTE.steel2,
      visual,
      items: items && (n.kind ?? "role") === "role" ? roleItems(n) : [],
    };
    byId.set(vn.id, vn);
    return vn;
  });

  const edges = [];
  const broadcasts = [];
  for (const e of rawEdges) {
    const cls = e.class ?? "msg";
    const tier = edgeTier(e, windows);
    if (cls === "broadcast") {
      if (byId.has(e.src)) broadcasts.push({ id: e.id ?? `${e.src}|*`, src: e.src, tier, rate: num(e.rate_per_h) });
      continue;
    }
    if (cls === "alias" && !global) continue;
    if (!byId.has(e.src) || !byId.has(e.dst)) continue;
    edges.push({
      id: e.id ?? `${e.src}|${e.dst}|${cls}`,
      src: e.src,
      dst: e.dst,
      cls,
      tier,
      color: EDGE_COLOR[cls] ?? EDGE_COLOR.msg,
      bend: EDGE_BEND[cls] ?? 0.14,
      rate: num(e.rate_per_h),
      n_rate: num(e.n_rate),
      n_1h: num(e.n_1h),
      n_24h: num(e.n_24h),
      last_age_s: typeof e.last_age_s === "number" ? e.last_age_s : null,
      shoal: e.shoal ?? null,
      flow: FLOW_CLASSES.has(cls) && (tier === "hot" || tier === "warm"),
      flowMs: flowMs(e.rate_per_h),
      cross: typeof e.cross_host === "boolean" ? e.cross_host : Boolean(byId.get(e.src).host && byId.get(e.dst).host && byId.get(e.src).host !== byId.get(e.dst).host),
      stale: e.stale === true,
      open: typeof e.open === "number" ? e.open : null,
    });
  }
  edges.sort((a, b) => (a.id < b.id ? -1 : 1));

  const neighbors = new Map(nodes.map((n) => [n.id, new Set()]));
  for (const e of edges) {
    neighbors.get(e.src)?.add(e.dst);
    neighbors.get(e.dst)?.add(e.src);
  }

  // the operator hub counts every role's pending grants, not only its own row's
  const hub = nodes.find((n) => n.kind === "user");
  if (hub) {
    const roleGrants = all.reduce((t, n) => t + grantsOf(n).length, 0);
    const grants = Math.max(hub.visual.grants ?? 0, roleGrants, shoals.reduce((t, x) => t + x.grants, 0));
    const asks = num(hub.work.asks_open) + grants;
    hub.visual = { ...hub.visual, grants, color: asks > 0 ? P.magenta : P.ink, badge: asks > 0 ? String(asks) : null };
  }

  const shoalsInView = global ? shoals : shoals.filter((s) => s.id === viewId);
  return {
    hostColor,
    view: viewId,
    global,
    ts: doc?.ts ?? null,
    host: doc?.host ?? null,
    hosts: arr(doc?.hosts),
    windows,
    nodes,
    byId,
    edges,
    broadcasts,
    neighbors,
    shoals,
    shoalsInView,
    needsYou: needsYouList(nodes.filter((n) => global || n.shoal === viewId)),
  };
}

/** Edge ids touching a node (for fading everything else on hover). */
export function incidentEdges(vm, id) {
  const out = new Set();
  for (const e of vm.edges) if (e.src === id || e.dst === id) out.add(e.id);
  return out;
}

/**
 * New events since the last seen seq. The first call (lastSeq null) only sets the baseline, so a
 * fresh mod does not replay the whole tail. Capped to the newest `cap`.
 */
/** diffEvents for a whole doc: a `cursor_reset` doc carries a fresh tail, so it is a new baseline. */
export function eventsSince(lastSeq, doc, cap = 12) {
  return diffEvents(doc?.cursor_reset === true ? null : lastSeq, doc?.events, cap);
}

export function diffEvents(lastSeq, events, cap = 12) {
  const list = arr(events).filter((e) => e && typeof e.seq === "number" && Number.isFinite(e.seq));
  const max = list.reduce((m, e) => Math.max(m, e.seq), lastSeq ?? -Infinity);
  const next = Number.isFinite(max) ? max : lastSeq ?? null;
  if (lastSeq === null || lastSeq === undefined) return { fresh: [], lastSeq: next };
  const fresh = list.filter((e) => e.seq > lastSeq).sort((a, b) => a.seq - b.seq).slice(-cap);
  return { fresh, lastSeq: next };
}

/**
 * Fixture mode only: synthetic events for tick n, so the demo shows packets and flashes without
 * a live feed. Deterministic: the same doc and tick give the same events.
 */
export function demoEvents(doc, tick) {
  const base = arr(doc?.events).reduce((m, e) => Math.max(m, num(e?.seq)), 0);
  const flows = arr(doc?.edges).filter((e) => ["msg", "ask", "offer", "grant", "health"].includes(e?.class) && !String(e.dst).endsWith(":*"));
  const out = [];
  if (flows.length) {
    const e = flows[tick % flows.length];
    out.push({ seq: base + tick * 2 + 1, ts: doc?.ts ?? null, shoal: e.shoal ?? null, src: e.src, dst: e.dst, type: "msg", class: e.class, kind: "info", item: "", summary: "(demo) message" });
  }
  const workers = arr(doc?.nodes).filter((n) => n?.kind === "role" && n?.state?.class === "work");
  if (workers.length && tick % 3 === 2) {
    const w = workers[Math.floor(tick / 3) % workers.length];
    out.push({ seq: base + tick * 2 + 2, ts: doc?.ts ?? null, shoal: w.shoal ?? null, src: w.id, dst: null, type: "done", class: "done", kind: "fix", item: "", summary: "(demo) done" });
  }
  if (tick % 4 === 3) {
    const b = arr(doc?.edges).find((e) => e?.class === "broadcast");
    if (b) out.push({ seq: base + tick * 2 + 2, ts: doc?.ts ?? null, shoal: b.shoal ?? null, src: b.src, dst: b.dst, type: "msg", class: "broadcast", kind: "info", item: "", summary: "(demo) broadcast" });
  }
  return out;
}

// ---- the default view (`shoal` prop) and shared focus

/**
 * The view a copy returns to: `shoal` (a shoal id) when the doc has it live, else "global".
 * `missing` says the prop named a shoal the feed does not have (retired, or a typo).
 */
export function homeView(doc, shoal) {
  if (typeof shoal !== "string" || !shoal || shoal === "global") return { home: "global", missing: false };
  const live = shoalSummaries(doc).some((s) => s.id === shoal);
  return { home: live ? shoal : "global", missing: !live };
}

/**
 * What Escape does, in order: unpin the card, close the legend, then go back to the home view
 * (a shoal for a copy opened with `shoal`, else global). Null when there is nothing to undo.
 */
export function escapeStep({ pinned = null, legend = false, view = "global", home = "global" } = {}) {
  if (pinned) return { pinned: null };
  if (legend) return { legend: false };
  if (view !== home) return { view: home };
  return null;
}

/** The shared key a pulse row (or any mod) sets to ask a full lattice view to focus a node. */
export const FOCUS_KEY = "lattice.focus";

/**
 * Reads the shared focus value: a node id string, or {node?, shoal?, t?} (t in ms; a request older
 * than `maxAgeMs` is stale). Returns {key, node, shoal} for a new request (key differs from
 * `seenKey`), else null. Copies remember the key they saw at start, so a value persisted from an
 * earlier run is not replayed.
 */
export function readFocus(value, seenKey, now = Date.now(), maxAgeMs = 120000) {
  let f;
  if (typeof value === "string" && value) f = { node: value, shoal: null, t: null };
  else if (value && typeof value === "object" && (typeof value.node === "string" || typeof value.shoal === "string")) {
    f = { node: typeof value.node === "string" && value.node ? value.node : null, shoal: typeof value.shoal === "string" && value.shoal ? value.shoal : null, t: typeof value.t === "number" ? value.t : null };
  } else return null;
  const key = `${f.node ?? ""}|${f.shoal ?? ""}|${f.t ?? ""}`;
  if (key === seenKey) return null;
  if (f.t !== null && now - f.t > maxAgeMs) return { key, node: null, shoal: null, stale: true };
  return { key, node: f.node, shoal: f.shoal ?? (f.node && f.node.includes(":") ? f.node.slice(0, f.node.lastIndexOf(":")) : null) };
}

/** The key of a shared focus value as readFocus computes it (null when it is not one). */
export const focusKeyOf = (value) => readFocus(value, undefined, 0, Infinity)?.key ?? null;

// ---- the actions dialog (shoal-actions), opened by a right-click

/**
 * The overlay a right-click on node `id` opens: the shoal-actions dialog on that role (a role
 * node) or on its shoal (the shoal's system node), else null. `machine` is where this copy reads
 * its feed (`source.on`, null for this computer); a fixture copy opens the dialog on fixtures too,
 * so a demo never runs shoal against made-up ids.
 */
export function actionsOverlay(vm, id, source = null) {
  const n = vm?.byId?.get(id);
  if (!n || typeof n.shoal !== "string" || !n.shoal) return null;
  let role = null;
  if (n.kind === "role") role = n.slug ?? n.label ?? null;
  else if (n.kind !== "system") return null;
  const src = obj(source) ?? {};
  const machine = src.kind === "exec" || src.kind === "file" ? (typeof src.on === "string" && src.on ? src.on : null) : null;
  const props = { shoal: n.shoal, role, machine };
  if (src.kind === "fixture") props.fixture = true;
  return { mod: "shoal-actions", kind: "dialog", width: 640, height: 540, props };
}

// ---- pulse mode: a compact edge drawer

/** The pulse drawer's rows: one per live shoal, then up to `maxNeeds` needs-you roles, loudest first. */
export function pulseModel(doc, opts = {}) {
  const only = typeof opts.shoal === "string" && opts.shoal ? opts.shoal : null;
  const all = shoalSummaries(doc);
  const shoals = only && all.some((s) => s.id === only) ? all.filter((s) => s.id === only) : all;
  const live = new Set(shoals.map((s) => s.id));
  const roles = arr(doc?.nodes).filter((n) => n && typeof n.id === "string" && (n.kind ?? "role") === "role" && live.has(n.shoal));
  const byId = new Map(roles.map((n) => [n.id, n]));
  const ranked = needsYouList(roles).filter((r) => r.rank < 4);
  const needs = ranked.slice(0, Math.max(0, opts.maxNeeds ?? 5)).map((r) => {
    const n = byId.get(r.id);
    const ev = str(obj(n?.state).event) || r.key;
    return { id: r.id, label: r.label, shoal: r.shoal, short: shortShoal(r.shoal), color: r.color, badge: r.badge ?? "!", event: ev, host: typeof n?.host === "string" ? n.host : null, since_s: r.since_s };
  });
  return {
    shoals: shoals.map((s) => ({ id: s.id, short: s.short, tint: s.tint, urgent: s.urgent, urgentColor: s.urgentColor, needsYou: s.needsYou, msgsPerH: s.msgsPerH })),
    needs,
    more: Math.max(0, ranked.length - needs.length),
  };
}

/** A hash of what the pulse draws: a poll that hashes the same needs no redraw. */
export function pulseSignature(pm) {
  const parts = [];
  for (const s of pm.shoals) parts.push(s.id, s.urgent, s.needsYou, s.msgsPerH);
  for (const r of pm.needs) parts.push(r.id, r.event, r.host ?? "");
  parts.push(pm.more);
  return hash32(parts.join("\u0001"));
}

/** The text lines of a hover card for a node, shared by main.js and the preview. */
export function cardLines(vm, id) {
  const n = vm.byId.get(id);
  if (!n) return null;
  const s = n.state;
  const m = n.metrics;
  if (n.kind === "user") {
    const w = n.work;
    const askers = vm.needsYou.map((r) => `${r.badge ?? "•"} ${r.label}${vm.global && r.shoal ? ` @${shortShoal(r.shoal)}` : ""}`);
    return { title: "you", subtitle: "operator", rows: [["asks open", String(num(w.asks_open))], ["grants pending", String(n.visual.grants ?? 0)], ["msgs 1h out/in", `${num(m.msgs_out_1h)}/${num(m.msgs_in_1h)}`]], list: askers, links: [] };
  }
  const v = n.visual;
  const rows = [];
  if (n.kind === "role") {
    // "~": a posture reading (no role-time probe), less certain than a role-time one
    rows.push(["state", `${s.source === "posture" ? "~ " : ""}${v.label}${s.event && v.key !== "other" ? ` (${s.event})` : ""} ${fmtAge(s.since_s)}`]);
    if (arr(s.quality).length) rows.push(["quality", arr(s.quality).map(str).join(", ")]);
    rows.push(["agent", [n.ecosystem, n.model].filter(Boolean).join(" · ") || "?"]);
    rows.push(["host", n.host ?? "?"]);
    if (n.jobHost) rows.push(["CI job on", `${n.jobHost} (${str(s.event) || "ci"})`]);
    const job = obj(s.job);
    if (job.unit) rows.push(["job", `${str(job.unit)}${job.granted ? " granted" : job.placed ? " placed" : ""}${typeof job.started_age_s === "number" ? ` ${fmtAge(job.started_age_s)}` : ""}`]);
    if (n.grants.length) rows.push(["grants", n.grants.map((g) => `${str(g.kind) || "grant"} ${str(g.item)}`.trim()).join(", ")]);
    rows.push(["done 24h", String(num(m.done_24h))]);
    rows.push(["msgs 1h out/in", `${num(m.msgs_out_1h)}/${num(m.msgs_in_1h)}`]);
    if (typeof m.context_k === "number") rows.push(["context", `${m.context_k}k`]);
    if (arr(s.reasons).length) rows.push(["reasons", arr(s.reasons).join(", ")]);
  } else {
    rows.push(["kind", n.kind]);
    if (n.alias_of) rows.push(["alias of", n.alias_of]);
    for (const [k, val] of Object.entries(m)) rows.push([k.replace(/_/g, " "), String(val)]);
  }
  const links = n.items
    .filter((it) => it.kind !== "done" || n.items.length <= 6)
    .map((it) => ({ kind: it.kind, text: `${it.kind === "active" ? "▶" : it.kind === "next" ? "•" : "✓"} ${it.item}${it.blocker ? ` ← ${it.blocker}` : ""}${it.stale ? " (stale)" : ""}`, url: it.url, stale: it.stale === true }));
  const asks = arr(n.work.asks).map((a) => `? ${a}`);
  return {
    title: n.label,
    subtitle: n.shoal ? shortShoal(n.shoal) : n.kind,
    color: v.color,
    rows,
    list: asks,
    links,
    command: n.viewArgv ? n.viewArgv.join(" ") : null,
  };
}

/**
 * A hash of everything the view draws from the doc (node states and badges, edge tiers and rate
 * bands, items, the rail's numbers and ages as shown). A poll whose doc hashes the same needs no
 * redraw; on a software renderer a redraw is the expensive part.
 */
export function visualSignature(vm) {
  const parts = [vm.view];
  for (const n of vm.nodes) {
    const v = n.visual;
    parts.push(n.id, v.key, v.badge ?? "", v.grants ?? 0, v.needsYou ? 1 : 0, n.items.length, n.jobHost ?? "", n.host ?? "", n.label);
  }
  for (const e of vm.edges) parts.push(e.id, e.tier, rateBand(e.rate), e.cross ? 1 : 0, e.stale ? 1 : 0);
  for (const b of vm.broadcasts) parts.push(b.src, b.tier);
  for (const r of vm.needsYou) parts.push(r.id, r.text, fmtAge(r.since_s));
  for (const s of vm.shoals) parts.push(s.id, s.gen, s.needsYou, s.msgsPerH, s.done24, s.done7, s.asks, s.grants, s.pages24, s.workShare ?? "n");
  for (const h of hostSummaries(vm)) parts.push(h.name, h.roles, h.working, h.waiting, h.msgsPerH, h.jobsIn);
  return hash32(parts.join("\u0001"));
}

/** A hash of what needs you: a change here redraws at once, whatever the tier. */
export function urgentSignature(vm) {
  return hash32(vm.needsYou.map((r) => `${r.id}:${r.key}`).join("|"));
}

export { withAlpha };
