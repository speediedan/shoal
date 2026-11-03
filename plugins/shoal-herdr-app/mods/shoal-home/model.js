// model.js: the launch list (`shoal ui launch --list --json --summary`) to a home view model.
// Pure: no Herdr or GPUI imports, so it runs under Node.
//
// The list is additive (v 1): unknown fields are ignored, a missing optional field is absent,
// never an error. Contract:
//   {"v":1, "kind":"shoal-launch-list", "last": "<name>" | null,
//    "hosts": [{"name", "local", "route", "reachable": true | false | null, "hint", "machine",
//               "summary": null | {"shoals": [{"id", "tag", "roles", "working", "waiting",
//                                              "needs_you"}], "needs_you", "pin_ok"}}]}
// `reachable: null` means not probed yet.

export const KIND = "shoal-launch-list";

// The lattice palette (lattice-view/model.js), so the two mods read as one product.
export const PALETTE = {
  bgTop: "#14111a",
  bgBottom: "#03050b",
  navy: "#0a1222",
  panelSolid: "#0b0e12",
  steel0: "#1c2a30",
  steel1: "#3c4a52",
  steel2: "#6a8592",
  ink: "#d4d4d4",
  dim: "#8c8c8c",
  faint: "#555555",
  teal: "#2ee6c0",
  magenta: "#ff4f9a",
  blue: "#4ea3ff",
  amber: "#ffb547",
  red: "#ff4d5e",
  cyan: "#38c8ff",
  mint: "#50d090",
  logo: "#5fe3ff",
  logoDeep: "#1f4fd8",
};

/** Identity tints for shoal tags (the lattice's pastels). */
export const PASTELS = ["#b0b070", "#50d090", "#70b090", "#d098b0", "#a898d8", "#88c8d8", "#d8b088"];

/** Reachable dot colors: green up, red down, grey not probed yet. */
export const DOT = { up: PALETTE.mint, down: PALETTE.red, unknown: "#6b7780" };

/** How many shoal rows a card lists before "+n more". */
export const MAX_SHOALS = 4;

const num = (v, d = 0) => (typeof v === "number" && Number.isFinite(v) ? v : d);
const arr = (v) => (Array.isArray(v) ? v : []);
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const strOrNull = (v) => (typeof v === "string" && v.trim() ? v.trim() : null);

/** "#rrggbb" with an alpha (0..1) as "#rrggbbaa". */
export function withAlpha(hex, a) {
  let h = String(hex).replace("#", "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const v = Math.round(Math.min(1, Math.max(0, num(a, 1))) * 255).toString(16).padStart(2, "0");
  return `#${h.slice(0, 6)}${v}`;
}

/** "alpha-20261101-000000" -> "alpha" (the shoal id's tag when the list gives none). */
export const shortShoal = (id) => String(id ?? "").replace(/-\d{8}-\d{6}$/, "");

function normSummary(s) {
  const o = obj(s);
  if (!o) return null;
  const shoals = arr(o.shoals)
    .map(obj)
    .filter((x) => x && (strOrNull(x.id) || strOrNull(x.tag)))
    .map((x) => {
      const id = strOrNull(x.id) ?? strOrNull(x.tag);
      return {
        id,
        tag: strOrNull(x.tag) ?? shortShoal(id),
        roles: num(x.roles),
        working: num(x.working),
        waiting: num(x.waiting),
        needsYou: num(x.needs_you),
      };
    });
  return {
    shoals,
    needsYou: num(o.needs_you, shoals.reduce((t, x) => t + x.needsYou, 0)),
    pinOk: typeof o.pin_ok === "boolean" ? o.pin_ok : null,
  };
}

/**
 * The launch list (JSON text or a parsed object) as {last, hosts}. Hosts: `local` first, then the
 * routes in the order given; a repeated name keeps its first row. Throws on text that is not JSON,
 * on another `kind`, or on an unsupported `v`.
 */
export function parseList(input) {
  let doc = input;
  if (typeof input === "string") {
    try {
      doc = JSON.parse(input);
    } catch (_e) {
      throw new Error("launch list is not JSON");
    }
  }
  const d = obj(doc);
  if (!d) throw new Error("launch list is not an object");
  if (typeof d.v === "number" && d.v !== 1) throw new Error(`launch list v${d.v} is not supported (v1)`);
  if (typeof d.kind === "string" && d.kind !== KIND) throw new Error(`not a launch list (${d.kind})`);
  const seen = new Set();
  const hosts = [];
  for (const raw of arr(d.hosts)) {
    const h = obj(raw);
    const name = strOrNull(h?.name);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    hosts.push({
      name,
      local: h.local === true || name === "local",
      route: strOrNull(h.route),
      reachable: typeof h.reachable === "boolean" ? h.reachable : null,
      hint: strOrNull(h.hint),
      machine: strOrNull(h.machine),
      summary: normSummary(h.summary),
    });
  }
  // stable: local rows first, routes keep their order
  const ordered = [...hosts.filter((h) => h.local), ...hosts.filter((h) => !h.local)];
  const last = strOrNull(d.last);
  return { last: last && seen.has(last) ? last : null, hosts: ordered };
}

/** What a poll changed, cheaply: a redraw only when this differs. */
export const listSignature = (list) => JSON.stringify(list ?? null);

/** "up", "down" or "unknown" (not probed yet). */
export const hostState = (h) => (h?.reachable === true ? "up" : h?.reachable === false ? "down" : "unknown");

/** A card can be clicked unless its probe says it is unreachable. */
export const canOpen = (h) => Boolean(h) && h.reachable !== false;

/** The selection on load: the last choice, else the first host that can open, else the first. */
export function initialSelection(list) {
  const hosts = list?.hosts ?? [];
  if (!hosts.length) return 0;
  const i = list.last ? hosts.findIndex((h) => h.name === list.last) : -1;
  if (i >= 0) return i;
  const j = hosts.findIndex(canOpen);
  return j >= 0 ? j : 0;
}

/** Keeps the selection on the same host name across a poll that reorders or drops rows. */
export function keepSelection(prevHosts, nextList, sel) {
  const name = prevHosts?.[sel]?.name;
  const hosts = nextList?.hosts ?? [];
  const i = name ? hosts.findIndex((h) => h.name === name) : -1;
  if (i >= 0) return i;
  return Math.min(Math.max(0, sel), Math.max(0, hosts.length - 1));
}

/**
 * Keyboard: {sel, action} for a keystroke. Arrows or j/k (and left/right) move, wrapping; a digit
 * jumps to that host (selects it, does not open); Enter opens; `r` refreshes. Unknown keys: null.
 */
export function keyAction(keystroke, sel, n) {
  const k = String(keystroke ?? "");
  if (k === "r") return { sel, action: "refresh" };
  if (!n) return null;
  if (k === "down" || k === "j" || k === "right" || k === "tab") return { sel: (sel + 1) % n, action: null };
  if (k === "up" || k === "k" || k === "left" || k === "shift-tab") return { sel: (sel - 1 + n) % n, action: null };
  if (k === "home" || k === "g") return { sel: 0, action: null };
  if (k === "end" || k === "shift-g") return { sel: n - 1, action: null };
  if (k === "enter" || k === "space") return { sel, action: "open" };
  if (/^[1-9]$/.test(k)) {
    const d = Number(k) - 1;
    return d < n ? { sel: d, action: null } : null;
  }
  return null;
}

/** The argv that opens a host's shoal spaces in the app (run here: `exec("shoal", argv)`). */
export const openArgv = (h) => ["ui", "app-open", "--machine", h.name];

const lastLine = (s) => String(s ?? "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean).pop() ?? "";
const cap = (s, n = 160) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/**
 * A one-line result for a card from an exec result {code, stdout, stderr}, or from the Error an
 * exec rejected with. A JSON stdout's `summary`, `message` or `error` wins over its last line.
 */
export function openResult(r) {
  if (r instanceof Error || (r && typeof r === "object" && typeof r.code !== "number")) {
    return { ok: false, text: cap(`could not run shoal: ${String(r?.message ?? r)}`) };
  }
  const out = String(r?.stdout ?? "");
  let j = null;
  try {
    j = obj(JSON.parse(out));
  } catch (_e) {
    j = null;
  }
  const said = j ? strOrNull(j.summary) ?? strOrNull(j.message) ?? strOrNull(j.error) : null;
  if (r.code === 0) return { ok: true, text: cap(said ?? (lastLine(out) || "opened")) };
  const why = said ?? (lastLine(r.stderr) || lastLine(out) || "no output");
  return { ok: false, text: cap(`exit ${r.code}: ${why}`) };
}

/**
 * The text lines of one host card below its header, in order: the route line, then shoal rows
 * (or one note when there is no summary), "+n more", the pin warning, and the open status. One
 * entry is one fixed-height line, so layout.js can size the card from the count alone.
 * `status`: {phase: "opening" | "ok" | "error", text} or null.
 */
export function cardLines(h, status = null) {
  const lines = [];
  const where = h.local ? "this machine" : h.route ?? h.name;
  const machine = h.machine && h.machine !== h.name ? ` · machine ${h.machine}` : "";
  lines.push({ kind: "sub", text: `${where}${machine}` });
  const st = hostState(h);
  if (st === "down") lines.push({ kind: "note", tone: "down", text: h.hint ? `unreachable: ${h.hint}` : "unreachable now" });
  else if (h.summary) {
    const shoals = h.summary.shoals;
    if (!shoals.length) lines.push({ kind: "note", tone: "dim", text: "no shoals running" });
    shoals.slice(0, MAX_SHOALS).forEach((s, i) => lines.push({ kind: "shoal", tint: PASTELS[i % PASTELS.length], ...s }));
    if (shoals.length > MAX_SHOALS) lines.push({ kind: "more", text: `+${shoals.length - MAX_SHOALS} more shoals` });
    if (h.summary.pinOk === false) lines.push({ kind: "pin", text: "⚠ Herdr pin differs: shoal ui doctor" });
  } else lines.push({ kind: "note", tone: "dim", text: st === "unknown" ? "not probed yet" : "no summary" });
  if (status) lines.push({ kind: "status", tone: status.phase, text: status.phase === "opening" ? "opening…" : status.text });
  return lines;
}

/** The text after a shoal tag on its row: roles working and waiting. */
export const shoalCounts = (s) => `${s.working} working · ${s.waiting} waiting · ${s.roles} roles`;

/** Total needs-you across a host's shoals (0 without a summary). */
export const hostNeedsYou = (h) => (h?.summary ? h.summary.needsYou : 0);
