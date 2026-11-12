// shoal-board: each shoal's priority board as an interactive dependency graph (Herdr app mod), the home space's `board`
// tab, opened by the top bar's Board.
//
// A thin View over model.js. It reads `shoal ui board --json` (this host's board digest and the roles' queues; no model
// turn, no board read of its own) from the first machine that has a digest, on a timer and on Refresh. Above the graph,
// the board's metrics: the rail card's counts and two trends (open items, closes a day). The graph has the board at
// the top and every open item below what it hangs from (its parent issue, its open blockers), drawn by where it stands
// with the roles: in progress, queued (its place in the queue), queued but blocked, or not queued.
//
// Two drawings: cards (each item's key, where it stands and its title) and nodes (a shape per repo, its number under
// it, so the relationships read at a glance), each at a zoom (- and +, or Fit for the whole tree; keys - + 0, n for
// the drawing). Hover an item for its details. Click it to select it: the side panel (x or Esc closes it) opens it on
// GitHub, or hands it to a role through the actions dialog (Offer for an item no role holds; Move, through the
// role's Queue, for one another role holds), picked from a list, which previews and confirms as everywhere else.
// Nothing here writes.
//
// props:
//   source: {argv?: [...], on?: machine} (default: shoal ui board --json, tried on every machine, then here)
//   shoal: the shoal to open on (else shared `board.focus`, else the first with a board)
//   poll_ms: read interval (default 60000, at least 15000)

import { View, div, PathBuilder, Background } from "gpui-kit";
import { h_flex, v_flex, Link } from "gpui-base";
import { exec, props, theme, machines, shared, nextCommand, openOverlay } from "herdr";
import {
  parseReply, boardArgv, pickView, layout, edgeCmds, arrowCmds, nodeStyle, hoverLines, itemActions, actionOverlay,
  tiles, sparkPoints, withAlpha, palette, WORK_LABEL, DIMS, ZOOM, scaleDims, stepZoom, fitZoom, repoShapes, shapeCmds,
  shapeCentre,
} from "./model.js";

const UI = "Geist";
const MONO = "Geist Mono";
const PANEL_W = 300;
const TOP_H = 290; // about what the header, metrics and legend take: the rest is the graph's view, for Fit
const store = (k, v) => { try { localStorage.setItem(k, v); } catch (_e) { /* a convenience */ } };
const recall = (k) => { try { return localStorage.getItem(k); } catch (_e) { return null; } };

const safe = (f, d) => {
  try {
    return f() ?? d;
  } catch (_e) {
    return d;
  }
};

function path(cmds, color, { fill = false, width = 1.4, dash = null } = {}) {
  let b = fill ? PathBuilder.fill() : PathBuilder.stroke(width);
  if (!fill && dash) b = b.dash_array(dash);
  for (const c of cmds) {
    if (c[0] === "M") b = b.move_to(c[1], c[2]);
    else if (c[0] === "L") b = b.line_to(c[1], c[2]);
    else if (c[0] === "C") b = b.cubic_bezier_to(c[5], c[6], c[1], c[2], c[3], c[4]);
    else if (c[0] === "Z") b = b.close();
  }
  return window.paint_path(b.build(), Background.solid(color)).absolute().top(0).left(0).size_full();
}

export default class Board extends View {
  init(_props, cx) {
    const p = safe(props, {}) ?? {};
    this.source = p.source && typeof p.source === "object" ? p.source : {};
    this.want = { shoal: typeof p.shoal === "string" ? p.shoal : null, board: null };
    this.doc = null;
    this.from = null; // the machine the boards were read on: actions run there
    this.hover = null;
    this.selected = null;
    this.size = { width: 1200, height: 800 };
    this.mode = recall("mode") === "nodes" ? "nodes" : "cards";
    this.zoom = 1;
    this.menu = null; // the side panel's open list: "offer" or "move"
    this.handle = cx.focus_handle();
    this.note = "reading the boards…";
    this.busy = false;
    this.read(cx);
    cx.timer.every(Math.max(15000, Number(p.poll_ms) || 60000), (cx) => this.read(cx));
    cx.spawn(async (cx) => {
      for (;;) {
        const c = await nextCommand();
        if (c === "refresh") this.read(cx);
        if (c === "focus") {
          this.applyFocus();
          this.handle.focus();
        }
        cx.notify();
      }
    });
  }

  /** The top bar's Board, from a shoal's space, says which shoal to show (shared `board.focus`). */
  applyFocus() {
    const f = safe(() => shared("board.focus"), null);
    if (f?.shoal && f.t !== this.focusT) {
      this.focusT = f.t;
      this.want = { shoal: f.shoal, board: null };
      this.selected = null;
    }
  }

  where() {
    if (this.source.on !== undefined) return [this.source.on ?? null];
    const names = safe(machines, []).map((m) => m?.name).filter((n) => typeof n === "string" && n);
    return [...new Set([...names, null])];
  }

  read(cx) {
    if (this.busy) return;
    this.busy = true;
    cx.spawn(async (cx) => {
      let got = null, from = null;
      for (const on of this.where()) {
        let reply = null;
        try {
          const argv = boardArgv(this.source);
          reply = await exec(argv[0], argv.slice(1), on ? { on } : {});
        } catch (_e) {
          reply = null;
        }
        const d = parseReply(reply);
        if (d && d.shoals.some((s) => (s.boards ?? []).length)) {
          got = d;
          from = on;
          break;
        }
        if (d && !got) {
          got = d;
          from = on;
        }
      }
      this.busy = false;
      if (got) {
        this.doc = got;
        this.from = from;
        this.note = null;
      } else if (!this.doc) {
        this.note = "no board digest on any machine yet (a shoal with a board writes one with `shoal digest`)";
      }
      cx.notify();
    });
  }

  key(e, cx, view) {
    const k = e.keystroke;
    if (k === "escape") {
      if (this.menu) this.menu = null;
      else if (this.selected) this.selected = null;
      else return;
    } else if (k === "+" || k === "=") this.zoom = stepZoom(this.zoom, 1);
    else if (k === "-") this.zoom = stepZoom(this.zoom, -1);
    else if (k === "0") this.fit(view);
    else if (k === "n") this.setMode(this.mode === "cards" ? "nodes" : "cards");
    else return;
    cx.notify();
  }

  setMode(m) {
    this.mode = m;
    this.menu = null;
    store("mode", m);
  }

  fit(view) {
    if (!view.board) return;
    this.zoom = fitZoom(view.board, this.graphWidth(), Math.max(200, this.size.height - TOP_H), DIMS[this.mode]);
  }

  graphWidth() {
    return Math.max(400, this.size.width - (this.selected ? PANEL_W : 0) - 56);
  }

  dims() {
    return scaleDims(DIMS[this.mode], this.zoom);
  }

  toolbar(view, c) {
    const seg = (id, text, on, click) => div().id(id).px(10).py(3).cursor_pointer().text_size(12)
      .bg(on ? withAlpha(c.accent, 0.2) : c.panel).text_color(on ? c.ink : c.muted).hover((s) => s.bg(c.hover))
      .on_click(click).child(text);
    const btn = (id, text, tip, click) => div().id(id).w(28).h(24).flex().items_center().justify_center().rounded(6)
      .cursor_pointer().border_1().border_color(c.border).bg(c.panel).text_size(13).text_color(c.ink)
      .hover((s) => s.bg(c.hover)).tooltip(tip).on_click(click).child(text);
    return h_flex().id("toolbar").gap(8).items_center()
      .child(h_flex().rounded(7).overflow_hidden().border_1().border_color(c.border)
        .child(seg("mode-cards", "Cards", this.mode === "cards", (_e, cx) => { this.setMode("cards"); cx.notify(); }))
        .child(seg("mode-nodes", "Nodes", this.mode === "nodes", (_e, cx) => { this.setMode("nodes"); cx.notify(); })))
      .child(btn("zoom-out", "−", "zoom out (-)", (_e, cx) => { this.zoom = stepZoom(this.zoom, -1); cx.notify(); }))
      .child(div().w(42).text_size(11.5).text_color(c.muted).flex().justify_center().child(`${Math.round(this.zoom * 100)}%`))
      .child(btn("zoom-in", "+", "zoom in (+)", (_e, cx) => { this.zoom = stepZoom(this.zoom, 1); cx.notify(); }))
      .child(div().id("zoom-fit").px(9).h(24).flex().items_center().rounded(6).cursor_pointer().border_1().border_color(c.border)
        .bg(c.panel).text_size(12).text_color(c.ink).hover((s) => s.bg(c.hover)).tooltip("the whole tree in view (0)")
        .on_click((_e, cx) => { this.fit(view); cx.notify(); }).child("Fit"));
  }

  chip(id, text, on, c, click) {
    return div().id(id).px(10).py(3).rounded(12).cursor_pointer().text_size(12)
      .border_1().border_color(on ? c.accent : c.border).bg(on ? withAlpha(c.accent, 0.18) : c.panel)
      .text_color(on ? c.ink : c.muted).hover((s) => s.bg(c.hover)).on_click(click).child(text);
  }

  header(view, c) {
    const row = h_flex().id("head").gap(8).items_center().flex_wrap()
      .child(div().text_size(20).font_semibold().mr(6).child("Board"));
    for (const s of this.doc?.shoals ?? []) {
      row.child(this.chip(`sh-${s.id}`, s.tag || s.id, view.shoal?.id === s.id, c, (_e, cx) => {
        this.want = { shoal: s.id, board: null };
        this.selected = null;
        cx.notify();
      }));
    }
    if ((view.shoal?.boards ?? []).length > 1) {
      for (const b of view.shoal.boards) {
        row.child(this.chip(`bd-${b.id}`, b.title || b.id, view.board?.id === b.id, c, (_e, cx) => {
          this.want = { shoal: view.shoal.id, board: b.id };
          this.selected = null;
          cx.notify();
        }));
      }
    }
    row.child(div().flex_1());
    if (view.board) row.child(this.toolbar(view, c));
    const age = view.shoal?.digest_age_s;
    if (age !== null && age !== undefined) row.child(div().text_size(11.5).text_color(c.dim).child(`digest ${Math.round(age / 60)} min old`));
    row.child(div().id("refresh").px(12).py(5).rounded(7).cursor_pointer().border_1().border_color(c.border).bg(c.panel)
      .hover((s) => s.bg(c.hover)).text_size(12).text_color(this.busy ? c.dim : c.ink)
      .on_click((_e, cx) => { this.read(cx); cx.notify(); }).child(this.busy ? "Reading…" : "↻ Refresh"));
    return row;
  }

  spark(id, label, values, color, c) {
    const w = 220, h = 40;
    const pts = sparkPoints(values, w, h);
    const box = div().relative().w(w).h(h).flex_none().rounded(6).bg(c.bg);
    if (pts.length) box.child(path(pts.map(([x, y], i) => [i ? "L" : "M", x, y]), color, { width: 1.6 }));
    const last = (values ?? []).filter((v) => v !== null && v !== undefined).slice(-1)[0];
    return v_flex().id(id).gap(4).p(10).rounded(10).bg(c.panel).border_1().border_color(c.border)
      .child(h_flex().gap(6).child(div().text_size(11.5).text_color(c.muted).child(label)).child(div().flex_1())
        .child(div().font_family(MONO).text_size(11.5).text_color(c.ink).child(last === undefined ? "–" : String(last))))
      .child(pts.length ? box : div().w(w).h(h).flex_none().text_size(11).text_color(c.dim).child("collecting: one reading every half hour"));
  }

  metrics(board, c) {
    const m = board.metrics;
    const row = h_flex().id("metrics").gap(10).flex_wrap().items_start();
    for (const t of tiles(m)) {
      row.child(v_flex().id(`tile-${t.id}`).w(132).flex_none().gap(2).p(10).rounded(10).bg(c.panel).border_1().border_color(c.border)
        .child(div().text_size(11).text_color(c.muted).child(t.label))
        .child(div().font_family(MONO).text_size(20).font_semibold().text_color(c[t.tone] ?? c.ink).child(t.value))
        .child(div().text_size(10.5).text_color(c.dim).truncate().child(t.note || " ")));
    }
    row.child(this.spark("spark-open", "Open, 14 days", m?.series?.open, c.accent, c));
    row.child(this.spark("spark-closed", "Closed a day, 14 days", m?.series?.closed, c.done, c));
    return row;
  }

  legend(c, board = null) {
    const swatch = (k, label) => {
      const s = nodeStyle({ work: k, role: "", pos: 1 }, c);
      return h_flex().gap(5).items_center().child(div().w(14).h(10).rounded(3).bg(s.fill).border_1().border_color(s.border))
        .child(div().text_size(11).text_color(c.muted).child(label));
    };
    const line = (color, label) => h_flex().gap(5).items_center().child(div().w(16).h(2).bg(color))
      .child(div().text_size(11).text_color(c.muted).child(label));
    const row = h_flex().id("legend").gap(14).flex_wrap().items_center()
      .child(swatch("active", WORK_LABEL.active)).child(swatch("queued", "queued (#: its place)"))
      .child(swatch("queued-blocked", WORK_LABEL["queued-blocked"])).child(swatch("unqueued", WORK_LABEL.unqueued))
      .child(line(c.edge, "sub-issue")).child(line(c.dep, "unblocks"));
    if (this.mode === "nodes" && board) {
      for (const [repo, shape] of Object.entries(repoShapes(board))) {
        const icon = div().relative().w(14).h(14).flex_none().child(path(shapeCmds(shape, 7, 7, 6), c.muted, { fill: true }));
        row.child(h_flex().id(`shape-${repo}`).gap(5).items_center().child(icon).child(div().text_size(11).text_color(c.muted).child(repo)));
      }
    }
    return row;
  }

  node(n, c, d) {
    const s = nodeStyle(n, c);
    const on = this.selected === n.key, hov = this.hover === n.key;
    const t = (px) => Math.max(8, px * d.scale);
    const box = v_flex().id(`n-${n.key}`).absolute().left(n.x).top(n.y).w(d.w).h(d.h).px(9 * d.scale).py(6 * d.scale).gap(2)
      .rounded(8).bg(s.fill).border_color(on ? c.accent : s.border).cursor_pointer()
      .on_hover((h, cx) => { if (h) this.hover = n.key; else if (this.hover === n.key) this.hover = null; cx.notify(); })
      .on_click((_e, cx) => { this.selected = on ? null : n.key; cx.notify(); })
      .child(h_flex().gap(6).items_center()
        .child(div().flex_1().min_w(0).truncate().font_family(MONO).text_size(t(11)).text_color(s.tone).child(n.key))
        .child(div().flex_none().max_w(96 * d.scale).truncate().text_size(t(10.5)).text_color(s.tone).child(s.badge)))
      .child(div().text_size(t(11.5)).text_color(s.ink).truncate().child(n.title || (n.off_board ? "not open on this board" : "")));
    if (s.dashed) box.border_dashed();
    return (on || hov ? box.border_2() : box.border_1());
  }

  shapes(lay, board, c) {
    const d = lay.dims, shapes = repoShapes(board), groups = new Map();
    for (const n of lay.nodes) {
      const s = nodeStyle(n, c);
      const k = `${s.fill}|${s.border}|${s.dashed}`;
      if (!groups.has(k)) groups.set(k, { s, cmds: [] });
      const o = shapeCentre(n, d);
      groups.get(k).cmds.push(...shapeCmds(shapes[String(n.key).split("#")[0]] ?? "circle", o.x, o.y, d.r));
    }
    const out = [];
    for (const { s, cmds } of groups.values()) {
      out.push(path(cmds, s.fill === c.panel ? c.raised : s.fill, { fill: true }));
      out.push(path(cmds, s.border, { width: 1.5, dash: s.dashed ? [3, 2] : null }));
    }
    const ring = lay.nodes.filter((n) => n.key === this.selected || n.key === this.hover)
      .flatMap((n) => { const o = shapeCentre(n, d); return shapeCmds(shapes[String(n.key).split("#")[0]] ?? "circle", o.x, o.y, d.r + 4); });
    if (ring.length) out.push(path(ring, c.accent, { width: 2 }));
    return out;
  }

  nodeHit(n, c, d) {
    const s = nodeStyle(n, c);
    const num = String(n.key).split("#")[1] ?? n.key;
    return div().id(`h-${n.key}`).absolute().left(n.x).top(n.y).w(d.w).h(d.h).cursor_pointer()
      .on_hover((h, cx) => { if (h) this.hover = n.key; else if (this.hover === n.key) this.hover = null; cx.notify(); })
      .on_click((_e, cx) => { this.selected = this.selected === n.key ? null : n.key; this.menu = null; cx.notify(); })
      .child(div().absolute().left(0).w(d.w).top(2 * d.r + 6 * d.scale).flex().justify_center()
        .font_family(MONO).text_size(Math.max(8, 10.5 * d.scale)).text_color(s.tone).child(`#${num}`));
  }

  hoverCard(n, c, lay) {
    const rows = hoverLines(n);
    const left = Math.min(n.x + lay.dims.w + 8, Math.max(0, lay.width - 330));
    const card = v_flex().id("hover-card").absolute().left(left).top(n.y).w(320).p(10).gap(4).rounded(10)
      .bg(c.raised).border_1().border_color(c.border).text_size(11.5)
      .child(div().font_semibold().text_color(c.ink).child(`${n.key}  ${n.title || ""}`.trim()));
    for (const [k, v] of rows) {
      card.child(h_flex().gap(8).items_start().child(div().w(70).flex_none().text_color(c.dim).child(k))
        .child(div().flex_1().min_w(0).text_color(c.muted).child(v)));
    }
    if (n.excerpt) card.child(div().text_size(11).text_color(c.dim).child(String(n.excerpt).slice(0, 220)));
    return card;
  }

  side(n, view, c) {
    const col = v_flex().id("side").w(PANEL_W).flex_none().gap(10).p(14).bg(c.panel).border_l(1).border_color(c.border)
      .overflow_y_scrollbar()
      .child(h_flex().items_center()
        .child(div().flex_1().font_family(MONO).text_size(12).text_color(c.muted).child(n.key))
        .child(div().id("side-close").w(22).h(22).flex().items_center().justify_center().rounded(5).cursor_pointer()
          .text_size(14).text_color(c.muted).hover((s) => s.bg(c.hover).text_color(c.ink)).tooltip("close (Esc)")
          .on_click((_e, cx) => { this.selected = null; this.menu = null; cx.notify(); }).child("×")))
      .child(div().text_size(14).font_semibold().text_color(c.ink).child(n.title || "(not open on this board)"));
    for (const [k, v] of hoverLines(n)) {
      col.child(h_flex().gap(8).items_start().text_size(11.5).child(div().w(70).flex_none().text_color(c.dim).child(k))
        .child(div().flex_1().min_w(0).text_color(c.muted).child(v)));
    }
    const acts = itemActions(n, view.shoal?.roles);
    if (acts.length) col.child(div().pt(4).text_size(11).text_color(c.dim).child(n.role ? `held by ${n.role}: open it, or move it` : "open it, or hand it to a role"));
    const open = acts.find((a) => a.kind === "open");
    if (open) col.child(Link.new(`open-${n.key}`).href(open.url).text_color(c.accent).child(open.label));
    const handTo = acts.filter((a) => a.kind !== "open");
    if (handTo.length) {
      const kind = handTo[0].kind;
      const label = kind === "move" ? "Move to a role" : "Offer to a role";
      const expanded = this.menu === kind;
      col.child(h_flex().id(`menu-${kind}`).px(10).py(6).gap(6).rounded(7).cursor_pointer().border_1()
        .border_color(expanded ? c.accent : c.border).bg(c.raised).hover((s) => s.bg(c.hover)).text_size(12).text_color(c.ink)
        .tooltip(kind === "move" ? "that role's Queue opens with this item picked: place it, preview, confirm (a transfer)"
          : "an offer to that role, previewed before it is sent")
        .on_click((_e, cx) => { this.menu = expanded ? null : kind; cx.notify(); })
        .child(div().flex_1().child(label)).child(div().text_color(c.muted).child(expanded ? "▴" : "▾")));
      if (expanded) {
        const list = v_flex().id("menu-list").gap(1).p(4).rounded(7).bg(c.bg).border_1().border_color(c.border).max_h(240).overflow_y_scrollbar();
        for (const a of handTo) {
          list.child(div().id(`act-${a.kind}-${a.role}`).px(9).py(5).rounded(5).cursor_pointer().text_size(12).text_color(c.ink)
            .hover((s) => s.bg(c.hover))
            .on_click((_e, cx) => { this.menu = null; openOverlay(actionOverlay(a, view.shoal.id, this.from)); cx.notify(); })
            .child(a.role));
        }
        col.child(list);
      }
    }
    return col;
  }

  graph(view, c) {
    const lay = layout(view.board, this.graphWidth(), this.dims());
    const d = lay.dims, t = (px) => Math.max(8, px * d.scale);
    const canvas = div().id("canvas").relative().w(lay.width).h(lay.height + 24).flex_none();
    const draw = (kind) => lay.edges.filter((e) => (kind === "blocks" ? e.kind === "blocks" : e.kind !== "blocks"));
    const tree = draw("tree").flatMap(edgeCmds);
    if (tree.length) canvas.child(path(tree, withAlpha(c.edge, 0.75), { width: 1.4 }));
    const blocks = draw("blocks");
    if (blocks.length) {
      canvas.child(path(blocks.flatMap(edgeCmds), c.dep, { width: 1.8, dash: [5, 4] }));
      canvas.child(path(blocks.flatMap((e) => arrowCmds(e)), c.dep, { fill: true }));
    }
    const b = view.board;
    const r = lay.root;
    canvas.child(v_flex().id("root-node").absolute().left(r.x).top(r.y).w(r.w).h(r.h).px(12 * d.scale).py(7 * d.scale).gap(2).rounded(10)
      .bg(withAlpha(b.closed ? c.done : c.accent, 0.16)).border_2().border_color(b.closed ? c.done : c.accent)
      // flex_none: a box too short for both lines clipped the title's top instead of letting the box size them
      .child(div().flex_none().text_size(t(13)).font_semibold().text_color(c.ink).truncate().child(b.title || b.id))
      .child(div().flex_none().text_size(t(11)).text_color(c.muted).child(b.closed ? `closed: all ${b.done} done` : `${b.open} open · ${b.done} done`)));
    if (lay.loose.count) {
      canvas.child(h_flex().id("loose-head").absolute().left(d.pad).top(lay.loose.y).h(Math.max(14, d.looseHead - 8)).gap(8).items_center()
        .child(div().text_size(12.5).font_semibold().text_color(c.muted).child(`No dependencies (${lay.loose.count})`))
        .child(div().text_size(11).text_color(c.dim).child("no parent, sub-issue or blocker on this board; in progress and queued first")));
    }
    if (this.mode === "nodes") {
      for (const el of this.shapes(lay, b, c)) canvas.child(el);
      for (const n of lay.nodes) canvas.child(this.nodeHit(n, c, d));
    } else {
      for (const n of lay.nodes) canvas.child(this.node(n, c, d));
    }
    const hov = lay.nodes.find((n) => n.key === this.hover);
    if (hov) canvas.child(this.hoverCard(hov, c, lay));
    return canvas;
  }

  render(_cx) {
    const c = palette(safe(theme, {}));
    this.applyFocus();
    const view = pickView(this.doc, this.want);
    const root = v_flex().id("board").size_full().min_h(0).bg(c.bg).font_family(UI).text_color(c.ink)
      .track_focus(this.handle)
      .on_key_down((e, cx) => this.key(e, cx, view))
      .on_mouse_down("left", () => this.handle.focus())
      .on_size_change((s, cx) => {
        if (Math.abs(s.width - this.size.width) < 1 && Math.abs(s.height - this.size.height) < 1) return;
        this.size = s;
        cx.notify();
      });
    const top = v_flex().px(24).pt(18).pb(10).gap(12).flex_none().child(this.header(view, c));
    if (this.note) top.child(div().text_size(12).text_color(c.dim).child(this.note));
    if (view.board) top.child(this.metrics(view.board, c)).child(this.legend(c, view.board));
    else if (view.shoal) top.child(div().text_size(12).text_color(c.dim).child(`${view.shoal.tag}: no board digest on this machine`));
    root.child(top);
    if (!view.board) return root;
    const body = h_flex().flex_1().min_h(0)
      .child(div().id("graph").flex_1().min_w(0).min_h(0).overflow_y_scrollbar().px(24).pb(24).child(this.graph(view, c)));
    const sel = this.selected && (view.board.nodes ?? []).find((n) => n.key === this.selected);
    if (sel) body.child(this.side(sel, view, c));
    return root.child(body);
  }
}
