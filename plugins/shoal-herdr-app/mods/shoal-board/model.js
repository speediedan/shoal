// shoal-board model: pure, with no Herdr imports, so it tests under Node.
//
// `shoal ui board --json` gives each shoal's priority boards as graphs: open items as nodes (`work`: active, queued at
// `pos`, queued-blocked, unqueued; `off_board` for a parent or blocker not open on the board) and edges (`parent`: an
// issue to its sub-issue; `blocks`: an open blocker to what it blocks), with each board's metrics. Here they become a
// layered drawing: the board's root at the top, each item a layer below everything it hangs from (its parent, its
// blockers), a wide layer wrapped into rows.

const arr = (v) => (Array.isArray(v) ? v : []);
const str = (v) => (typeof v === "string" ? v : v == null ? "" : String(v));
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

export const NODE = { w: 184, h: 54, gapX: 16, gapY: 42, rowGap: 14, pad: 16 };
export const ROOT = { w: 260, h: 56 };
// the two drawings: cards (each item's key, badge and title) and nodes (a shape per repo, its number under it, so a
// board's relationships read at a glance); `scale` is the zoom
export const DIMS = {
  cards: { ...NODE, rootW: ROOT.w, rootH: ROOT.h, looseHead: 30, scale: 1 },
  nodes: { w: 58, h: 52, gapX: 10, gapY: 34, rowGap: 10, pad: 16, rootW: 220, rootH: 56, looseHead: 30, r: 13, scale: 1 },
};
export const ZOOM = { min: 0.3, max: 2, step: 1.25 };

/** The sizes of a drawing at zoom `z`: every length scaled, the text with it (never below a readable floor in main.js). */
export function scaleDims(d, z) {
  const out = { ...d, scale: z };
  for (const k of ["w", "h", "gapX", "gapY", "rowGap", "pad", "rootW", "rootH", "looseHead", "r"]) if (typeof d[k] === "number") out[k] = d[k] * z;
  return out;
}

/** The next zoom in or out, held to its range. */
export function stepZoom(z, dir) {
  return Math.max(ZOOM.min, Math.min(ZOOM.max, dir > 0 ? z * ZOOM.step : z / ZOOM.step));
}

/** The zoom at which the whole tree fits a `width` x `height` view (never above 1): tried from 1 down, since a smaller
 * zoom fits more items to a row and so changes the height too. */
export function fitZoom(board, width, height, base = DIMS.cards) {
  for (let z = 1; z >= ZOOM.min; z /= 1.1) {
    const lay = layout(board, width, scaleDims(base, z));
    if (lay.height <= height && lay.width <= width + 1) return z;
  }
  return ZOOM.min;
}

// eight, so a board over seven repos (one measured) has a shape for each before any repeats
const SHAPES = ["circle", "square", "diamond", "triangle", "hexagon", "pentagon", "octagon", "star"];

/** Each repo's shape in the node drawing, by first appearance on the board: {repo: shape}. */
export function repoShapes(board) {
  const out = {};
  for (const n of arr(board?.nodes)) {
    const repo = str(n.key).split("#")[0];
    if (repo && !(repo in out)) out[repo] = SHAPES[Object.keys(out).length % SHAPES.length];
  }
  return out;
}

/** A shape of radius `r` centred on (x, y) as display-list commands (a circle as four cubic arcs). */
export function shapeCmds(shape, x, y, r) {
  const poly = (k, rot) => {
    const pts = Array.from({ length: k }, (_, i) => {
      const a = rot + (i * 2 * Math.PI) / k;
      return [x + r * Math.cos(a), y + r * Math.sin(a)];
    });
    return [["M", ...pts[0]], ...pts.slice(1).map((p) => ["L", ...p]), ["Z"]];
  };
  switch (shape) {
    case "square": return [["M", x - r * 0.86, y - r * 0.86], ["L", x + r * 0.86, y - r * 0.86], ["L", x + r * 0.86, y + r * 0.86], ["L", x - r * 0.86, y + r * 0.86], ["Z"]];
    case "diamond": return poly(4, -Math.PI / 2);
    case "triangle": return poly(3, -Math.PI / 2).map((c) => (c[0] === "Z" ? c : [c[0], c[1], c[2] + r * 0.18]));
    case "hexagon": return poly(6, 0);
    case "pentagon": return poly(5, -Math.PI / 2);
    case "octagon": return poly(8, Math.PI / 8);
    case "star": {
      const pts = Array.from({ length: 10 }, (_, i) => {
        const a = -Math.PI / 2 + (i * Math.PI) / 5, rr = i % 2 ? r * 0.45 : r;
        return [x + rr * Math.cos(a), y + rr * Math.sin(a)];
      });
      return [["M", ...pts[0]], ...pts.slice(1).map((q) => ["L", ...q]), ["Z"]];
    }
    default: {
      const k = 0.5523 * r;
      return [["M", x, y - r], ["C", x + k, y - r, x + r, y - k, x + r, y], ["C", x + r, y + k, x + k, y + r, x, y + r],
              ["C", x - k, y + r, x - r, y + k, x - r, y], ["C", x - r, y - k, x - k, y - r, x, y - r], ["Z"]];
    }
  }
}

/** Where a node's shape sits in its cell: centred across, near the top (its label goes under it). */
export function shapeCentre(n, d) {
  return { x: n.x + d.w / 2, y: n.y + d.r + 3 * d.scale };
}
export const ROOT_KEY = "__board__";
export const LOOSE_HEAD = 30; // the "No dependencies" heading above the loose grid (at zoom 1)

/** The CLI's reply as a document, or null for a failed or unreadable one (the last good one then stays). */
export function parseReply(reply) {
  if (!reply || reply.code !== 0) return null;
  try {
    const d = JSON.parse(reply.stdout);
    return d && d.kind === "shoal-board" && Array.isArray(d.shoals) ? d : null;
  } catch (_e) {
    return null;
  }
}

/** The argv that reads the boards. */
export function boardArgv(source) {
  return Array.isArray(source?.argv) && source.argv.length ? source.argv.map(String) : ["shoal", "ui", "board", "--json"];
}

/** The shoal and board in view: the asked-for ones when they exist, else the first shoal with a board. */
export function pickView(doc, want = {}) {
  const shoals = arr(doc?.shoals);
  const s = shoals.find((x) => x.id === want.shoal) ?? shoals.find((x) => arr(x.boards).length) ?? shoals[0] ?? null;
  if (!s) return { shoal: null, board: null };
  const b = arr(s.boards).find((x) => x.id === want.board) ?? arr(s.boards)[0] ?? null;
  return { shoal: s, board: b };
}

/**
 * Each node's layer: 1 below the deepest of what it hangs from, the root's children at 1. Prerequisites sit below: a
 * sub-issue hangs from its parent and a blocker hangs from what it blocks, so the board reads one way throughout (each
 * item waits on what is under it, the goal at the top waits on everything). A cycle (two items blocking each other, or a
 * parent blocking its own sub-issue) is cut where it closes, so every node gets a layer.
 */
export function layers(board) {
  const nodes = arr(board?.nodes);
  const keys = new Set(nodes.map((n) => n.key));
  const preds = new Map(nodes.map((n) => [n.key, []]));
  for (const e of arr(board?.edges)) {
    if (!keys.has(e.from) || !keys.has(e.to) || e.from === e.to) continue;
    // a `blocks` edge runs blocker -> blocked: the blocker is the prerequisite, so it hangs under the item it blocks
    if (e.kind === "blocks") preds.get(e.from).push(e.to);
    else preds.get(e.to).push(e.from);
  }
  const layer = new Map();
  const visiting = new Set();
  const visit = (k) => {
    if (layer.has(k)) return layer.get(k);
    if (visiting.has(k)) return 0; // a cycle: this edge does not count
    visiting.add(k);
    const l = 1 + Math.max(0, ...preds.get(k).map(visit));
    visiting.delete(k);
    layer.set(k, l);
    return l;
  };
  for (const n of nodes) visit(n.key);
  return { layer, preds };
}

const WORK_ORDER = { active: 0, queued: 1, "queued-blocked": 2, unqueued: 3 };

/**
 * Where everything goes for a canvas `width` px wide: {nodes: [{...node, x, y}], root: {x, y, w, h}, edges: [{kind, from,
 * to, x1, y1, x2, y2}], loose: {y, count}, width, height}. Items in a dependency (a parent, a sub-issue, a blocker or a
 * blocked item) are drawn as the graph: within a layer, nodes follow the mean place of what they hang from (then board
 * order), so a parent's sub-issues sit under it, and a layer wider than the canvas wraps into rows. Items in none hang
 * straight off the board: drawn as edges, dozens of them crossed the graph (measured on a 78-item board, 48 of them), so
 * they go in a grid of their own below it, by where they stand with the roles, then board order.
 */
export function layout(board, width = 1000, dims = DIMS.cards) {
  const D = dims;
  const { layer, preds } = layers(board);
  const linked = new Set();
  for (const [k, ps] of preds) if (ps.length) [k, ...ps].forEach((x) => linked.add(x));
  const all = arr(board?.nodes);
  const nodes = all.filter((n) => linked.has(n.key));
  const loose = all.filter((n) => !linked.has(n.key));
  const perRow = Math.max(1, Math.floor((Math.max(width, D.w + 2 * D.pad) - 2 * D.pad + D.gapX) / (D.w + D.gapX)));
  const byLayer = new Map();
  for (const n of nodes) {
    const l = layer.get(n.key);
    if (!byLayer.has(l)) byLayer.set(l, []);
    byLayer.get(l).push(n);
  }
  const placed = new Map();
  const root = { x: Math.max(D.pad, (width - D.rootW) / 2), y: D.pad, w: D.rootW, h: D.rootH };
  let y = root.y + D.rootH + D.gapY;
  const order = (n) => num(n.pos_board) ?? 1e6;
  for (const l of [...byLayer.keys()].sort((a, b) => a - b)) {
    const centre = (n) => {
      const xs = preds.get(n.key).map((p) => placed.get(p)).filter(Boolean).map((p) => p.x + D.w / 2);
      return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : root.x + D.rootW / 2;
    };
    const row = byLayer.get(l).slice().sort((a, b) => centre(a) - centre(b) || order(a) - order(b));
    for (let i = 0; i < row.length; i += perRow) {
      const chunk = row.slice(i, i + perRow);
      const span = chunk.length * D.w + (chunk.length - 1) * D.gapX;
      let x = Math.max(D.pad, (width - span) / 2);
      for (const n of chunk) {
        placed.set(n.key, { ...n, x, y, layer: l });
        x += D.w + D.gapX;
      }
      y += D.h + (i + perRow < row.length ? D.rowGap : D.gapY);
    }
  }
  const at = (k) => (k === ROOT_KEY ? { x: root.x, y: root.y, w: D.rootW, h: D.rootH } : placed.get(k) && { ...placed.get(k), w: D.w, h: D.h });
  const edges = [];
  const link = (kind, from, to) => {
    const a = at(from), b = at(to);
    if (!a || !b) return;
    // from a's bottom down to b's top; a target above (a blocker's edge up into what it unblocks) from a's top to b's
    // bottom; one beside it (a cut cycle) bottom to bottom
    const up = b.y + b.h <= a.y, beside = !up && b.y < a.y + a.h;
    edges.push({ kind, from, to, x1: a.x + a.w / 2, y1: up ? a.y : a.y + a.h, x2: b.x + b.w / 2,
                 y2: up || beside ? b.y + b.h : b.y, dir: up ? "up" : beside ? "beside" : "down" });
  };
  for (const n of nodes) if (!preds.get(n.key).length) link("root", ROOT_KEY, n.key);
  for (const e of arr(board?.edges)) link(e.kind === "blocks" ? "blocks" : "parent", e.from, e.to);
  const looseAt = { y: nodes.length ? y : root.y + D.rootH + D.gapY, count: loose.length };
  if (loose.length) {
    y = looseAt.y + D.looseHead;
    const sorted = loose.slice().sort((a, b) => (WORK_ORDER[a.work] ?? 9) - (WORK_ORDER[b.work] ?? 9) || order(a) - order(b));
    for (let i = 0; i < sorted.length; i += perRow) {
      const chunk = sorted.slice(i, i + perRow);
      let x = D.pad;
      for (const n of chunk) {
        placed.set(n.key, { ...n, x, y, layer: 0, loose: true });
        x += D.w + D.gapX;
      }
      y += D.h + D.rowGap;
    }
    y += D.gapY - D.rowGap;
  }
  return { nodes: [...placed.values()], root, edges, loose: looseAt,
           width: Math.max(width, perRow * (D.w + D.gapX) + 2 * D.pad), height: y, dims: D };
}

/** An edge as a display-list path: a vertical S-curve, rising when the target sits higher, dipping under a row beside. */
export function edgeCmds(e) {
  const dy = Math.max(24, Math.abs(e.y2 - e.y1) / 2);
  const [c1, c2] = e.dir === "up" ? [e.y1 - dy, e.y2 + dy] : e.dir === "beside" ? [e.y1 + dy, e.y2 + dy] : [e.y1 + dy, e.y2 - dy];
  return [["M", e.x1, e.y1], ["C", e.x1, c1, e.x2, c2, e.x2, e.y2]];
}

/** The small arrowhead at a `blocks` edge's end, pointing into the item it unblocks (up, from below, as a rule). */
export function arrowCmds(e, size = 6) {
  const back = e.dir === "up" || e.dir === "beside" ? size * 1.4 : -size * 1.4; // the head's base: below the tip when rising
  return [["M", e.x2 - size, e.y2 + back], ["L", e.x2, e.y2], ["L", e.x2 + size, e.y2 + back], ["Z"]];
}

export const WORK_LABEL = { active: "in progress", queued: "queued", "queued-blocked": "queued, blocked", unqueued: "not queued" };

/** How a node reads: {fill, border, dashed, ink, badge, tone} from its work and the palette. */
export function nodeStyle(n, c) {
  if (n.off_board) return { fill: c.bg, border: c.dim, dashed: true, ink: c.muted, badge: "off board", tone: c.dim };
  switch (n.work) {
    case "active": return { fill: withAlpha(c.working, 0.18), border: c.working, dashed: false, ink: c.ink, badge: `▶ ${n.role}`, tone: c.working };
    case "queued": return { fill: withAlpha(c.queued, 0.14), border: c.queued, dashed: false, ink: c.ink, badge: `${n.role} #${n.pos}`, tone: c.queued };
    case "queued-blocked": return { fill: withAlpha(c.queued, 0.1), border: c.blocked, dashed: true, ink: c.ink, badge: `⛔ ${n.role} #${n.pos}`, tone: c.blocked };
    default: return { fill: c.panel, border: c.border, dashed: false, ink: c.muted, badge: "not queued", tone: c.dim };
  }
}

/** The hover card's lines: [label, value], only those with something to say. */
export function hoverLines(n) {
  const rows = [];
  const add = (k, v) => { if (str(v).trim()) rows.push([k, str(v)]); };
  add("status", [WORK_LABEL[n.work] ?? n.work, n.status ? `board: ${n.status}` : ""].filter(Boolean).join(" · "));
  if (n.role) add("role", n.pos ? `${n.role}, #${n.pos} in its queue` : n.role);
  add("blocker", n.blocker);
  if (arr(n.blocked_by).length) add("blocked by", n.blocked_by.join(", "));
  add("parent", n.parent);
  if (n.sub_issues) add("sub-issues", String(n.sub_issues));
  if (arr(n.assignees).length) add("assignees", n.assignees.join(", "));
  if (arr(n.labels).length) add("labels", n.labels.slice(0, 6).join(", "));
  add("milestone", n.milestone);
  if (arr(n.linked_prs).length) add("PRs", n.linked_prs.map((p) => `#${p}`).join(", "));
  add("updated", str(n.updated).replace("T", " ").replace("Z", " UTC"));
  return rows;
}

/**
 * What a selected item offers: open it on GitHub, and per role either Offer (an item no role holds: it goes into that
 * role's queue through an offer) or Move (another role holds it: that role's Queue opens with it picked, and the plan
 * transfers it). The role that holds it now is left out. Each opens the actions dialog, which previews and confirms.
 */
export function itemActions(n, roles) {
  const out = [];
  if (/^https:\/\//i.test(str(n?.url))) out.push({ kind: "open", label: "Open on GitHub ↗", url: n.url });
  if (!n || n.off_board) return out;
  for (const r of arr(roles)) {
    if (!r?.slug || r.slug === n.role) continue;
    out.push(n.role ? { kind: "move", label: `Move to ${r.slug}`, role: r.slug, verb: "queue", pick: n.key }
      : { kind: "offer", label: `Offer to ${r.slug}`, role: r.slug, verb: "offer", item: offerItem(n) });
  }
  return out;
}

/** The item an offer names: `owner/repo#n:fix` (a PR is reviewed). */
export function offerItem(n) {
  const repo = str(n.repo) || str(n.key).split("#")[0];
  return `${repo}#${n.number ?? str(n.key).split("#")[1]}:${n.kind === "pr" ? "review" : "fix"}`;
}

/** The overlay that runs one action: the actions dialog on that role, opened on the verb, the item filled or picked. */
export function actionOverlay(a, shoal, machine = null) {
  const props = { shoal, role: a.role, verb: a.verb, machine };
  if (a.item) props.item = a.item;
  if (a.pick) props.pick = a.pick;
  return { mod: "shoal-actions", kind: "dialog", width: 640, height: 600, props };
}

/** The metrics panel's tiles: [{id, label, value, note, tone}]. */
export function tiles(m) {
  if (!m) return [];
  const pace = m.close_rate_7d ? `${m.close_rate_7d}/day over 7 days` : "no closes this week";
  const change = m.open_change_14d === null || m.open_change_14d === undefined ? "" : `${m.open_change_14d > 0 ? "+" : ""}${m.open_change_14d} in 14 days`;
  return [
    { id: "open", label: "Open", value: String(m.open), note: change, tone: "ink" },
    { id: "done", label: "Done", value: `${m.pct_done}%`, note: `${m.done} of ${m.total}`, tone: "done" },
    { id: "active", label: "In progress", value: String(m.active), note: "a role's active item", tone: "working" },
    { id: "queued", label: "Queued", value: String(m.queued), note: m.queued_blocked ? `+${m.queued_blocked} blocked` : "none blocked", tone: "queued" },
    { id: "unqueued", label: "Not queued", value: String(m.unqueued), note: m.dep_blocked ? `${m.dep_blocked} wait on a blocker` : "", tone: "dim" },
    { id: "closed", label: "Closed, 7 days", value: String(m.closed_7d), note: pace, tone: "done" },
    { id: "eta", label: "At this pace", value: m.eta_days === null || m.eta_days === undefined ? "–" : `${m.eta_days} d`, note: "until none are open", tone: "dim" },
  ];
}

/** A series as sparkline points in a w×h box: [[x, y]], nulls skipped; [] with fewer than two values. */
export function sparkPoints(values, w, h, max = null) {
  const vs = arr(values).map((v, i) => [i, num(v)]).filter(([, v]) => v !== null);
  if (vs.length < 2) return [];
  const n = arr(values).length;
  const top = Math.max(1, max ?? Math.max(...vs.map(([, v]) => v)));
  return vs.map(([i, v]) => [(i * (w - 2)) / Math.max(1, n - 1) + 1, h - 1 - (v / top) * (h - 3)]);
}

/** "#rrggbb" with an alpha (0..1) as "#rrggbbaa". */
export function withAlpha(hex, a) {
  const h = str(hex).replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(h)) return hex;
  return `#${h}${Math.round(Math.max(0, Math.min(1, a)) * 255).toString(16).padStart(2, "0")}`;
}

/** Theme colors for the board, with fallbacks. */
export function palette(t = {}) {
  const pick = (...names) => names.map((k) => t?.[k]).find((v) => typeof v === "string" && v) ?? null;
  return {
    bg: pick("canvas", "background") ?? "#0b1016", panel: pick("surface", "raised") ?? "#121a22",
    raised: pick("raised", "surface") ?? "#18222c", border: pick("border") ?? "#24313d", hover: pick("hover") ?? "#1f2b36",
    ink: pick("foreground") ?? "#d8e2ea", muted: pick("muted") ?? "#93a4b3", dim: pick("dim", "muted") ?? "#5f7282",
    accent: pick("accent") ?? "#5cc8ff", working: pick("working") ?? "#ffb547", queued: "#5b8def",
    blocked: pick("blocked") ?? "#ff4f9a", done: pick("done") ?? "#50d090", edge: "#7f93a6", dep: "#ff7a59",
  };
}
