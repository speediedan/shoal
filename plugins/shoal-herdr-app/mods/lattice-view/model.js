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
  "research-proto": "research-proto",
  lease: "lease",
  upstream: "upstream",
  "session-blocked": "peer",
  handover: "handover",
  refill: "refill",
  hold: "hold",
};
const GROUP_KEY = { human: "ask", system: "system", upstream: "upstream", peer: "peer", research: "research-proto" };

const SOFT_MAGENTA = "#d985b6";
// Five colors, by what you would do: needs you (magenta, hot for auth; red when something broke), working (teal), waiting on
// a system (blue), idle (slate), off (steel). The badge and hatch say which event; the hover says the rest.
const WAITING = "#5b9dff";
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
  "local-ci": { label: "local CI", color: WAITING, fill: 0.3, hatch: 4, spinner: true, border: "solid", badge: "▣", urgency: null },
  "remote-ci": { label: "remote CI", color: WAITING, fill: 0.22, hatch: 7, spinner: true, border: "solid", badge: "☁", urgency: null },
  // a research or prototyping run: a run like CI (the spinner), but not CI, so its outline is dashed and its hatch wide
  "research-proto": { label: "research run", color: WAITING, fill: 0.2, hatch: 11, spinner: true, border: "dashed", badge: "⚗", urgency: null },
  lease: { label: "waits for a lease", color: WAITING, fill: 0.3, hatch: 2.6, border: "solid", badge: "⧖", urgency: null },
  system: { label: "waits on system", color: WAITING, fill: 0.28, hatch: 4, border: "solid", urgency: null },
  upstream: { label: "waits upstream", color: WAITING, fill: 0.26, hatch: 5, border: "solid", badge: "↑", urgency: null },
  peer: { label: "blocked on a peer", color: WAITING, fill: 0.22, border: "dashed", badge: "⊘", urgency: null },
  handover: { label: "handing over", color: WAITING, fill: 0.35, border: "dashed", badge: "⇆", urgency: null },
  refill: { label: "refilling", color: WAITING, fill: 0.3, border: "dashed", badge: "↺", urgency: null },
  hold: { label: "on hold", color: WAITING, fill: 0.3, border: "solid", badge: "∥", urgency: null },
  other: { label: "other wait", color: WAITING, fill: 0.25, border: "dashed", badge: "…", urgency: null },
  idle: { label: "idle", color: P.slate, fill: 0.75, urgency: null },
  queued: { label: "idle, work queued", color: P.slate, fill: 0.75, badge: "•", urgency: null },
  starved: { label: "no work assigned", color: P.slate, fill: 0.4, badge: "∅", urgency: null },
  dismissed: { label: "dismissed: you cleared it", color: P.slate, fill: 0.3, border: "solid", opacity: 0.7, badge: "✓", urgency: null },
  held: { label: "held: you are offline", color: P.steel2, fill: 0.2, border: "solid", opacity: 0.7, badge: "☾", urgency: null },
  dead: { label: "session ended", color: P.red, fill: 0.12, border: "dotted", badge: "✕", urgency: 4, stall: true },
  excluded: { label: "excluded", color: P.steel2, fill: 0, border: "solid", opacity: 0.42, urgency: null },
  unknown: { label: "unknown", color: P.red, fill: 0.12, border: "dotted", badge: "✕", urgency: 4, stall: true },
};

/** Loudness for picking a shoal's most urgent state (pulse dot, cluster tint). */
export const STATE_RANK = ["auth", "ask", "prompt", "design", "assign", "sources", "unknown", "peer", "local-ci", "remote-ci", "research-proto", "lease", "system", "upstream", "handover", "refill", "hold", "other", "dead", "working", "held", "queued", "starved", "idle", "excluded"];

/** The legend's groups, loudest first: what you would do about a node. */
export const LEGEND_GROUPS = [
  { id: "you", label: "NEEDS YOU", keys: ["auth", "ask", "prompt", "design", "assign", "sources", "dead", "unknown"] },
  { id: "working", label: "WORKING", keys: ["working"] },
  { id: "waiting", label: "WAITING ON A SYSTEM", keys: ["local-ci", "remote-ci", "research-proto", "lease", "system", "upstream", "peer", "handover", "refill", "hold", "other"] },
  { id: "idle", label: "IDLE", keys: ["idle", "queued", "starved", "dismissed"] },
  { id: "off", label: "OFF", keys: ["held", "excluded"] },
];

/** Per-host palette for host tags, bands and tethers (distinct from state hues). */
export const HOST_COLORS = ["#9ad1ff", "#f2a7c6", "#b7e08a", "#e6c08f", "#c3a6ff", "#8fe0d2", "#f0e68c"];

// Traffic is one color: an edge's hover card says which kind. What stands apart is what stops work (blocked-on).
const TRAFFIC = "#7fb6cc";
export const EDGE_COLOR = {
  msg: TRAFFIC,
  broadcast: TRAFFIC,
  ask: TRAFFIC,
  health: TRAFFIC,
  offer: TRAFFIC,
  grant: TRAFFIC,
  handoff: TRAFFIC,
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
/** A link a click may open: http or https only, since item and event urls are ledger text a role wrote. */
export const webUrl = (v) => (typeof v === "string" && /^https?:\/\//i.test(v) ? v : null);
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

/**
 * `wait:self` (idle with runnable next items, nothing running) says what the role could do, not what
 * holds it, so it is drawn as its cause: an open question, a session that ended (offline or not: nothing
 * else would show a silent stall), the operator being offline (`parked-work` does not wake then), or
 * plain idle with work queued. Role-time accounting keeps the event.
 */
function selfKey(s, node) {
  const reasons = arr(s.reasons);
  if (reasons.includes("blocked-on-question")) return "prompt";
  if (arr(obj(node?.work).asks).length || reasons.includes("waiting-on-you")) return "ask";
  if (reasons.includes("dead-worker")) return "dead";
  return s.shoal_offline === true ? "held" : "queued";
}

/**
 * How long a role can wait for work before that reads as a choice. `wait:assignment` stays human wait in the accounting, but
 * an empty queue on a fill=assigned role is usually deliberate, so after this it stops asking for attention: no halo, out of
 * needs-you, a count on the rail. An open question still counts.
 */
export const STARVED_AFTER_S = 7200;

function assignKey(s, node) {
  const reasons = arr(s.reasons);
  const asking = arr(obj(node?.work).asks).length || reasons.includes("waiting-on-you") || reasons.includes("blocked-on-question");
  return !asking && num(s.since_s) >= STARVED_AFTER_S ? "starved" : "assign";
}

/** The state key of a node, from its role-time class and event. Never throws. */
export function stateKey(node) {
  const s = obj(node?.state);
  if (s.dismissed === true) return "dismissed";
  const cls = str(s.class);
  const ev = str(s.event);
  const grp = str(s.group);
  if (cls === "work") return "working";
  if (cls === "wait") {
    if (ev === "self" || (!ev && grp === "self")) return selfKey(s, node);
    if (ev === "assignment") return assignKey(s, node);
    if (Object.prototype.hasOwnProperty.call(EVENT_KEY, ev)) return EVENT_KEY[ev];
    if (!ev && Object.prototype.hasOwnProperty.call(GROUP_KEY, grp)) return GROUP_KEY[grp];
    return "other";
  }
  if (cls === "idle") return "idle";
  if (cls === "excluded") return "excluded";
  return "unknown";
}

/** A role's identity tint: a pastel picked by the hash of its id. */
export const roleTint = (id) => PASTELS[hash32(id) % PASTELS.length];

/** Grants waiting on the operator for a role (`work.grants_pending`, optional). */
export const grantsOf = (node) => arr(obj(node?.work).grants_pending).filter((g) => g && typeof g === "object");

/** Everything the renderer needs to draw one node. */
/** Keys the lattice draws as asking for you that the status row may not count (an empty queue): its needs_you decides. */
const ROW_DECIDES = new Set(["assign", "sources"]);

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
  if (kind === "writer") {
    // an unknown or removed ledger writer: a small dashed node, quieter than an external
    return { kind, key: "writer-unknown", shape: "circle", r: 6, color: "#7a8a94", fill: 0.04, border: "dashed", urgency: null, needsYou: false, opacity: 0.7, badge: null, grants: 0 };
  }
  if (kind === "external") {
    return { kind, key: "external", shape: "circle", r: 8, color: node.alias_of ? P.lavender : "#8fa3ad", fill: 0.05, border: "dashed", urgency: null, needsYou: false, opacity: 0.8, badge: null, grants: 0 };
  }
  const st = STATE_STYLE[key] ?? STATE_STYLE.unknown;
  const done = num(metrics.done_24h);
  // a role held by the operator being offline asks nothing of them until they are back
  // one rule with the status and the agents list: for an empty queue, the row's needs_you decides (2026-11-08: the
  // lattice counted a wait:assignment role as needing you while the agents list, rightly by the row, did not)
  const rowSays = obj(node?.state).needs_you === true;
  const needsYou = key !== "held" && key !== "starved" && (ROW_DECIDES.has(key) ? rowSays : Boolean(st.needsYou || rowSays));
  return {
    kind: "role",
    key,
    shape: "circle",  // the agent's type is in the hover card
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

/**
 * The legend (toggle `l`): the states drawn now, in `LEGEND_GROUPS` order; `all` lists every one. Without a view model every
 * state counts as drawn. `hiddenStates` says how many more `all` would show.
 */
export function legendEntries(vm = null, all = false) {
  const entry = (key) => {
    const s = STATE_STYLE[key];
    return {
      key, label: key === "other" ? "other (unknown wait event)" : s.label, color: s.color, badge: s.badge ?? null,
      style: s.hatch ? "hatch" : s.border === "dashed" ? "dashed" : s.border === "dotted" ? "dotted" : s.fill === 0 ? "outline" : "fill",
      pulse: Boolean(s.ring), needsYou: Boolean(s.needsYou),
    };
  };
  const drawn = vm ? new Set(vm.nodes.filter((n) => n.kind === "role").map((n) => n.visual.key)) : null;
  const shown = (key) => all || !drawn || drawn.has(key);
  const groups = LEGEND_GROUPS.map((g) => ({ id: g.id, label: g.label, states: g.keys.filter(shown).map(entry) })).filter((g) => g.states.length);
  const states = groups.flatMap((g) => g.states);
  const hiddenStates = Object.keys(STATE_STYLE).length - states.length;
  const nodes = vm ? vm.nodes : null;
  const shapes = [["ring", "you (operator hub)"]];
  if (!nodes || nodes.some((n) => n.kind === "external" || n.kind === "writer")) shapes.push(["dashed", "outside writer"]);
  const edges = [{ cls: "messages", color: TRAFFIC, dash: null }];
  if (!vm || vm.edges.some((e) => e.cls === "blocked-on")) edges.push({ cls: "blocked on", color: EDGE_COLOR["blocked-on"], dash: EDGE_DASH["blocked-on"] });
  if (!vm || vm.edges.some((e) => e.cross)) edges.push({ cls: "cross-host", color: TRAFFIC, dash: CROSS_HOST_DASH });
  const hosts = vm ? [...vm.hostColor.entries()].map(([name, color]) => ({ name, color })) : [];
  const extra = [];
  if (!nodes || nodes.some((n) => n.grants?.length)) extra.push(["grant pending", P.olive, "⚑"]);
  if (!nodes || nodes.some((n) => n.jobHost)) extra.push(["CI or research run on another host", "#9ad1ff", "→"]);
  return { groups, states, hiddenStates, shapes, edges, hosts: hosts.length > 1 ? hosts : [], extra };
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
  for (const [name, color] of vm.hostColor) rows.set(name, { name, color, home: false, roles: 0, working: 0, waiting: 0, needsYou: 0, msgsPerH: 0, msgs24: 0, jobsIn: 0 });
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
      r.msgs24 += num(n.metrics.msgs_out_24h) + num(n.metrics.msgs_in_24h);
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
    r.msgs24 = num(h.msgs_24h, r.msgs24);
  }
  return [...rows.values()].filter((r) => r.roles > 0 || r.jobsIn > 0);
}

/** Satellite items of a role: active, next and recently done, for the starburst layer. */
export function roleItems(node, caps = { next: 4, done: 5 }) {
  const work = obj(node?.work);
  const out = [];
  const active = work.active && typeof work.active === "object" ? work.active : null;
  if (active?.item) out.push({ kind: "active", item: active.item, url: webUrl(active.url), title: active.title ?? null });
  for (const n of arr(work.next).slice(0, caps.next)) if (n?.item) out.push({ kind: "next", item: n.item, url: webUrl(n.url), blocker: n.blocker ?? null, stale: n.stale === true, title: n.title ?? null });
  for (const d of arr(work.done_recent).slice(-caps.done)) if (d?.item) out.push({ kind: "done", item: d.item, url: webUrl(d.url), ts: d.ts ?? null, title: d.title ?? null });
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
      text: asks[0] ?? (s.says ? str(s.says) : s.event === "self" ? v.label : s.event ? `${s.class}:${s.event}` : s.class ?? ""),
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
      starved: keys.filter((k) => k === "starved").length,
      offline: obj(s.mode).offline === true,
      freeze: obj(s.mode).freeze === true,
      ledgerAge: typeof s.ledger_age_s === "number" ? s.ledger_age_s : null,
      boards: boardsOf(s, doc?.host),
    };
  });
}

/**
 * A shoal's priority boards for the rail: each a link and, where this host holds a digest, its counts. `note` says why
 * a board has none; a digest is read on the shoal's home only.
 */
export function boardsOf(s, host = null) {
  const b = obj(s?.boards);
  const count = (c) => ({ done: num(c.done), open: num(c.open), inFlight: num(c.in_flight), assigned: num(c.assigned) });
  const items = (Array.isArray(b.items) ? b.items : [])
    .filter((x) => typeof x?.url === "string" && x.url.startsWith("https://"))
    .map((x) => ({ id: String(x.id ?? x.url), title: String(x.title || x.id || "board"), url: x.url, counts: x.counts ? count(x.counts) : null }));
  const off = obj(b.off_board);
  return {
    items,
    note: s?.home && host && s.home !== host ? `counts are read on ${s.home}` : "no board read yet",
    offBoard: { inFlight: num(off.in_flight), assigned: num(off.assigned) },
    age: typeof b.age_s === "number" ? b.age_s : null,
    stale: b.stale === true,
  };
}

/** The hover id of a shoal's name on the lattice. */
export const BOARD_CARD = "board-card:";

/** One line of a board's numbers, for a tooltip. */
export const boardSummary = (c) => `${c.done} done · ${c.open} open · ${c.inFlight} in flight · ${c.assigned} assigned`;

/** Queued work that is not open on the board, in words; "" when there is none. */
export function offBoardText(off) {
  const bits = [];
  if (off?.inFlight) bits.push(`+${off.inFlight} in flight`);
  if (off?.assigned) bits.push(`+${off.assigned} assigned`);
  return bits.length ? `${bits.join(", ")} off the board` : "";
}

/** The hover card of a shoal's name: its boards' numbers, drawn as the rail draws them. */
export function boardCardLines(vm, sid) {
  const s = vm.shoals.find((x) => x.id === sid);
  const B = s?.boards;
  if (!B?.items.length) return null;
  const rows = [];
  if (B.age !== null) rows.push(["read", `${fmtAge(B.age)} ago${B.stale ? " (old)" : ""}`]);
  if (!B.items.some((b) => b.counts)) rows.push(["counts", B.note]);
  const off = offBoardText(B.offBoard);
  if (off) rows.push(["off the board", off.replace(" off the board", "")]);
  rows.push(["click", "opens the board"]);
  return { title: B.items.length === 1 ? B.items[0].title : `${s.short} boards`, subtitle: "board", color: s.tint, rows, list: [], links: [], command: null,
           boards: B.items.filter((b) => b.counts) };
}

/** The rail's table, in reading order: done, open, in flight, assigned. */
export function boardCells(c) {
  return [
    { label: "done", n: c.done, color: PALETTE.steel2, about: "closed on the board however it closed, or in a Done status" },
    { label: "open", n: c.open, color: PALETTE.ink, about: "open on the board and in no role's queue" },
    { label: "in flight", n: c.inFlight, color: PALETTE.teal, about: "a role's active item" },
    { label: "assigned", n: c.assigned, color: PALETTE.violet, about: "in a role's queue, and nobody's active item" },
  ];
}

/** The table as a bar `width` wide: a cell with anything in it stays visible, however large the rest. */
export function boardBar(c, width) {
  const cells = boardCells(c).filter((x) => x.n > 0);
  const total = cells.reduce((t, x) => t + x.n, 0);
  if (!total) return [];
  const widths = cells.map((x) => Math.max(2, Math.round((width * x.n) / total)));
  const big = widths.indexOf(Math.max(...widths));
  widths[big] -= widths.reduce((t, w) => t + w, 0) - width;
  return cells.map((x, i) => ({ color: x.color, w: widths[i] }));
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
  const homeOf = new Map(shoals.map((s) => [s.id, s.home]));
  const hostColor = hostColors(doc);
  const live = new Set(shoals.map((s) => s.id));
  // a shoal's status lives in the middle of its circle (`shoalCardLines`), not on a node of its own
  const hidden = new Set(arr(opts.hidden));
  const all = arr(doc?.nodes).filter((n) => n && typeof n.id === "string" && n.kind !== "system" && !hidden.has(n.id));
  const hiddenN = arr(doc?.nodes).filter((n) => n && hidden.has(n.id)).length;
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
      // a role running away from its shoal's home says where in its label: the global view has no host bands
      awayHost: (n.kind ?? "role") === "role" && typeof n.host === "string" && homeOf.get(n.shoal) && n.host !== homeOf.get(n.shoal) ? n.host : null,
      jobHost: jobHostOf(n),
      grants: grantsOf(n),
      alias_of: n.alias_of ?? null,
      saved: n.saved === true,
      seat: typeof n.seat === "string" && n.seat ? n.seat : null, // a renamed seat: its saved label (Remove uses it)
      standalone: obj(n.standalone).id ? obj(n.standalone) : null,
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
      recent: arr(e.recent).filter((r) => r && typeof r.summary === "string"),
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
    hiddenN,
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
  if (n?.kind === "external" && n.saved && !n.alias_of)
    return dialogSpec({ shoal: null, role: null, session: n.seat ?? n.label }, source);
  if (!n || n.kind !== "role" || typeof n.shoal !== "string" || !n.shoal) return null;
  return dialogSpec({ shoal: n.shoal, role: n.slug ?? n.label ?? null }, source);
}

/** The same dialog on a whole shoal (its circle), or across every shoal (outside any circle, the hub). */
export function scopeOverlay(vm, shoal, source = null) {
  if (!vm) return null;
  return dialogSpec(shoal ? { shoal, role: null } : { shoal: null, role: null, scope: "all" }, source);
}

function dialogSpec(target, source) {
  const src = obj(source);
  const machine = src.kind === "exec" || src.kind === "file" ? (typeof src.on === "string" && src.on ? src.on : null) : null;
  const props = { ...target, machine };
  if (src.kind === "fixture") props.fixture = true;
  return { mod: "shoal-actions", kind: "dialog", width: 640, height: 600, props };
}

/**
 * The removal dialog for a node, or null: a role, for good (stop, drop from the shoal and its harness's list), or an outside
 * writer, as the saved standalone session of that name. An aliased writer is a role elsewhere, so it has no removal here.
 */
export function removeOverlay(vm, id, source = null) {
  const n = vm?.byId?.get(id);
  if (n?.kind === "role" && n.shoal) return dialogSpec({ shoal: n.shoal, role: n.slug ?? n.label ?? null, verb: "remove-role" }, source);
  // a writer with no saved session has nothing to remove: its card offers to hide it
  if (n?.kind === "external" && !n.alias_of && n.saved) return dialogSpec({ shoal: null, role: null, session: n.seat ?? n.label, verb: "remove-session" }, source);
  return null;
}

/** Nodes a person can take off the picture: dashed outside writers, and roles parked or paused for good. */
export const hideable = (n) => n?.kind === "external" || n?.kind === "writer" || (n?.kind === "role" && n.visual?.key === "excluded");

/** The persistent list of hidden node ids (shared `lattice.hidden`), cleaned of anything that is not a string. */
export const HIDDEN_KEY = "lattice.hidden";
export const hiddenList = (v) => arr(v).filter((x) => typeof x === "string");

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
    shoals: shoals.map((s) => ({ id: s.id, short: s.short, tint: s.tint, urgent: s.urgent, urgentColor: s.urgentColor, needsYou: s.needsYou, msgsPerH: s.msgsPerH, msgs24: s.msgs24 })),
    needs,
    more: Math.max(0, ranked.length - needs.length),
  };
}

/** A hash of what the pulse draws: a poll that hashes the same needs no redraw. */
export function pulseSignature(pm) {
  const parts = [];
  for (const s of pm.shoals) parts.push(s.id, s.urgent, s.needsYou, s.msgsPerH, s.msgs24);
  for (const r of pm.needs) parts.push(r.id, r.event, r.host ?? "");
  parts.push(pm.more);
  return hash32(parts.join("\u0001"));
}

/** The text lines of a hover card for an edge: its counts per window and its last few events. */
export function edgeCardLines(vm, id) {
  const e = vm.edges.find((x) => x.id === id);
  if (!e) return null;
  const a = vm.byId.get(e.src), b = vm.byId.get(e.dst);
  const rows = [["1h / 24h", `${e.n_1h} / ${e.n_24h}`]];
  if (e.last_age_s !== null) rows.push(["last", fmtAge(e.last_age_s)]);
  if (e.open !== null) rows.push(["open", String(e.open)]);
  if (e.stale) rows.push(["blocker", "stale"]);
  if (e.cross) rows.push(["hosts", `${a.host ?? "?"} → ${b.host ?? "?"}`]);
  const links = e.recent.map((r) => ({
    kind: "event",
    text: `${typeof r.age_s === "number" ? `${fmtAge(r.age_s)} ` : ""}${r.summary}`,
    url: webUrl(r.url),
    stale: false,
  }));
  return { title: `${a.label} → ${b.label}`, subtitle: e.cls, color: e.color, rows, list: [], links, command: null };
}

/** A ledger clone older than this reads as a warning: the feed never pulls, so someone else must. */
export const LEDGER_STALE_S = 3600;
export const ledgerStale = (s) => typeof s?.ledgerAge === "number" && s.ledgerAge > LEDGER_STALE_S;

/** A hover target for the middle of a shoal's circle. */
export const SHOAL_CARD = "shoal-card:";

/** The text lines of the shoal status card (the middle of the circle). */
export function shoalCardLines(vm, sid) {
  const s = vm.shoals.find((x) => x.id === sid);
  if (!s) return null;
  const mode = [s.offline ? "offline" : "", s.freeze ? "freeze" : ""].filter(Boolean).join(", ") || "on";
  const rows = [["mode", mode], ["roles", `${s.roles}: ${s.working} working, ${s.waiting} waiting`], ["needs you", String(s.needsYou)], ...(s.starved ? [["no work assigned", String(s.starved)]] : []),
    ["asks / grants", `${s.asks} / ${s.grants}`], ["pages 24h", String(s.pages24)], ["done 24h / 7d", `${s.done24} / ${s.done7}`], ["msgs 1h / 24h", `${s.msgsPerH} / ${s.msgs24}`]];
  if (s.home) rows.push(["home", s.home]);
  if (ledgerStale(s)) rows.push(["ledger clone", `${fmtAge(s.ledgerAge)} old on ${vm.host ?? "this host"}`]);
  return { title: s.short, subtitle: "shoal", color: s.urgentColor, rows, list: [], links: [], command: null, boards: s.boards.items.filter((b) => b.counts) };
}

/** A saved standalone session's state, in the words its card uses. */
const STANDALONE_NOW = { busy: "running a turn", alive: "alive, between turns", blocked: "alive, waiting on you", stopped: "stopped" };

/** A satellite's item, from its id (`<role node id>#<index>`, as the layout names it), or null. */
export function itemOf(vm, id) {
  const cut = typeof id === "string" ? id.lastIndexOf("#") : -1;
  if (cut < 0) return null;
  const n = vm?.byId?.get(id.slice(0, cut));
  const it = n?.items?.[Number(id.slice(cut + 1))];
  return it ? { node: n, it } : null;
}

const ITEM_KIND = { active: "working on it", next: "queued", done: "done in the last 24 h" };

/** A satellite's hover card: the item, its title, why it waits, and the issue a click opens. */
export function itemCardLines(vm, id) {
  const { node: n, it } = itemOf(vm, id);
  const rows = [["role", `${n.label}${vm.global && n.shoal ? ` @${shortShoal(n.shoal)}` : ""}`], ["now", ITEM_KIND[it.kind] ?? it.kind]];
  if (it.title) rows.push(["title", it.title]);
  // as last recorded: a blocker that cleared reads as held until it is re-recorded or ages to stale
  if (it.blocker) rows.push(["blocked on", `${it.blocker}${it.stale ? " (stale)" : " (as recorded)"}`]);
  else if (it.kind === "next") rows.push(["blocked on", "nothing: it can start"]);
  if (it.ts) rows.push(["when", it.ts]);
  const drawn = n.items.filter((x) => x.kind === "next").length;
  const total = num(obj(n.work).next_total, arr(obj(n.work).next).length);
  if (it.kind === "next" && total > drawn) rows.push(["queue", `${total} queued, ${drawn} drawn`]);
  return { title: it.item, subtitle: it.kind, color: n.visual.color, rows, list: [], links: it.url ? [{ kind: it.kind, text: "open it", url: it.url }] : [], command: null };
}

/** The text lines of a hover card for a node, an edge or a shoal, shared by main.js and the preview. */
export function cardLines(vm, id) {
  if (typeof id === "string" && id.startsWith(SHOAL_CARD)) return shoalCardLines(vm, id.slice(SHOAL_CARD.length));
  if (typeof id === "string" && id.startsWith(BOARD_CARD)) return boardCardLines(vm, id.slice(BOARD_CARD.length));
  const n = vm.byId.get(id);
  if (!n) return itemOf(vm, id) ? itemCardLines(vm, id) : edgeCardLines(vm, id);
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
    rows.push(["state", `${s.source === "posture" ? "~ " : ""}${v.label}${s.event && s.event !== "self" && v.key !== "other" ? ` (${s.event})` : ""} ${fmtAge(s.since_s)}`]);
    if (arr(s.quality).length) rows.push(["quality", arr(s.quality).map(str).join(", ")]);
    rows.push(["agent", [n.ecosystem, n.model].filter(Boolean).join(" · ") || "?"]);
    rows.push(["host", n.host ?? "?"]);
    if (n.jobHost) rows.push([str(s.event) === "research-proto" ? "research run on" : "CI job on", `${n.jobHost} (${str(s.event) || "ci"})`]);
    const job = obj(s.job);
    if (job.unit) rows.push(["job", `${str(job.unit)}${job.granted ? " granted" : job.placed ? " placed" : ""}${typeof job.started_age_s === "number" ? ` ${fmtAge(job.started_age_s)}` : ""}`]);
    if (n.grants.length) rows.push(["grants", n.grants.map((g) => `${str(g.kind) || "grant"} ${str(g.item)}`.trim()).join(", ")]);
    rows.push(["done 24h", String(num(m.done_24h))]);
    rows.push(["msgs 1h out/in", `${num(m.msgs_out_1h)}/${num(m.msgs_in_1h)}`]);
    rows.push(["context", typeof m.context_k === "number" ? `${m.context_k}k` : "not read here"]);
    // only a role not mid-turn can be said to be ready: its doc is refreshed and it stopped at a boundary
    if (str(s.class) !== "work") rows.push(["restart-ready", s.restart_ready === true ? "yes" : "no"]);
    if (arr(s.reasons).length) rows.push(["reasons", arr(s.reasons).join(", ")]);
  } else {
    rows.push(["kind", n.kind]);
    if (n.alias_of) rows.push(["alias of", n.alias_of]);
    else if (n.kind === "external" && n.standalone) {
      rows.push(["session", `${n.standalone.harness} ${n.standalone.id}`]);
      rows.push(["now", STANDALONE_NOW[n.standalone.state] ?? "not read here"]);
      if (n.standalone.dismissed) rows.push(["app", "dismissed: no space of its own"]);
    } else if (n.kind === "external" && !n.saved) rows.push(["session", "none saved here"]);
    for (const [k, val] of Object.entries(m)) rows.push([k.replace(/_/g, " "), String(val)]);
  }
  const links = n.items
    .filter((it) => it.kind !== "done" || n.items.length <= 6)
    .map((it) => ({ kind: it.kind, text: `${it.kind === "active" ? "▶" : it.kind === "next" ? "•" : "✓"} ${it.item}${it.blocker ? ` ← ${it.blocker}` : ""}${it.stale ? " (stale)" : ""}`, url: it.url, stale: it.stale === true }));
  const asks = [...arr(n.work.asks), ...(s?.says ? [str(s.says)] : [])].map((a) => `? ${a}`);
  return {
    title: n.label,
    subtitle: n.shoal ? shortShoal(n.shoal) : n.kind,
    color: v.color,
    rows,
    list: asks,
    links,
    command: n.viewArgv ? n.viewArgv.join(" ") : null,
    hideable: hideable(n),
  };
}

/**
 * A hash of everything the view draws from the doc (node states and badges, edge tiers and rate
 * bands, items, the rail's numbers and ages as shown). A poll whose doc hashes the same needs no
 * redraw; on a software renderer a redraw is the expensive part. `cardId`: the node or edge whose
 * card is open; an edge's card shows counts and events no other part of the picture does.
 */
export function visualSignature(vm, cardId = null) {
  const parts = [vm.view];
  const card = cardId ? vm.edges.find((e) => e.id === cardId) : null;
  if (card) parts.push(card.id, card.n_rate, card.n_1h, card.n_24h, card.recent[0]?.ts ?? "");
  for (const n of vm.nodes) {
    const v = n.visual;
    parts.push(n.id, v.key, v.badge ?? "", v.grants ?? 0, v.needsYou ? 1 : 0, n.items.length, n.jobHost ?? "", n.host ?? "", n.label);
  }
  for (const e of vm.edges) parts.push(e.id, e.tier, rateBand(e.rate), e.cross ? 1 : 0, e.stale ? 1 : 0);
  for (const b of vm.broadcasts) parts.push(b.src, b.tier);
  for (const r of vm.needsYou) parts.push(r.id, r.text, fmtAge(r.since_s));
  for (const s of vm.shoals) parts.push(s.id, s.starved, s.offline ? 1 : 0, ledgerStale(s) ? 1 : 0, s.needsYou, s.msgsPerH, s.done24, s.done7, s.asks, s.grants, s.pages24, s.workShare ?? "n");
  for (const s of vm.shoals) {
    const B = s.boards;
    parts.push(fmtAge(B.age), B.stale ? 1 : 0, B.offBoard.inFlight, B.offBoard.assigned);
    for (const b of B.items) parts.push(b.id, b.title, b.url, ...(b.counts ? Object.values(b.counts) : []));
  }
  for (const h of hostSummaries(vm)) parts.push(h.name, h.roles, h.working, h.waiting, h.msgsPerH, h.msgs24, h.jobsIn);
  return hash32(parts.join("\u0001"));
}

/** A hash of what needs you: a change here redraws at once, whatever the tier. */
export function urgentSignature(vm) {
  return hash32(vm.needsYou.map((r) => `${r.id}:${r.key}`).join("|"));
}

export { withAlpha };
