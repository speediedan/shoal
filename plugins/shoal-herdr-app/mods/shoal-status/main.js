// shoal-status: the window's status bar in the shoal layout (Herdr app mod). Where you are and how many agents need you,
// as in Herdr's own bar, plus two things that are always within reach: the way back to a lattice (a button and
// Ctrl+Alt+L: `shoal ui app-lattice` reopens a closed tab) and each provider's usage. Ctrl+Alt+N, or a click on the
// needs-you count, goes to the next agent that needs you, in the lattice pulse's order. Reads usage from the cached
// `shoal ui usage --json` (no model turns) of every machine at once, saying `usage …` until a reading lands and drawing
// no percentage from a reading over an hour old, and on a slow timer runs `shoal ui app-sync`, so roles tabs follow the roles.

import { View, div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { spaces, agents, machines, terminals, panes, theme, run, exec, nextCommand, props, setShared } from "herdr";
import { parseUsage, usageMachines, usageComplete, bestUsage, usageShown, usageRetry, USAGE_RETRY_MS, USAGE_WARM_MS, usageTip, nextNeedsYou, latticeTab, homeTab,
  shoalInView } from "./model.js";

const USAGE_MS = 60000;
const SYNC_MS = 120000;

const safe = (f, d) => {
  try {
    return f() ?? d;
  } catch (_e) {
    return d;
  }
};

export default class ShoalStatus extends View {
  init(_props, cx) {
    this.rows = null;  // no reading yet: the meters draw as refreshing
    this.born = Date.now();
    this.reading = 0;  // when the pass in flight started
    this.note = null;
    this.readUsage(cx);
    cx.timer.every(USAGE_MS, (cx) => this.readUsage(cx));
    cx.timer.every(SYNC_MS, (cx) => this.sync(cx));
    cx.spawn(async (cx) => {
      for (;;) {
        const c = await nextCommand();
        if (c === "lattice") this.lattice(cx);
        else if (c === "home") this.home(cx);
        else if (c === "usage") this.usage(cx);
        else if (c === "board") this.board(cx);
        else if (c === "next") this.nextNeedsYou();
        cx.notify();
      }
    });
  }

  readUsage(cx) {
    // a warm-up retry and the timer do not overlap; a pass stuck on a machine that never answers holds the next one back
    // for one interval only
    if (this.reading && Date.now() - this.reading < USAGE_MS) return;
    const pass = this.reading = Date.now();
    const where = usageMachines(safe(spaces, []), safe(machines, []).map((m) => m.name));
    cx.spawn(async (cx) => {
      // every machine at once: asked in turn, a slow ssh answer held the fresh one back
      const seen = where.map(() => null);
      let drawn = false;
      await Promise.all(where.map(async (on, i) => {
        let reply = null;
        try {
          reply = await exec("shoal", ["ui", "usage", "--json"], on ? { on } : {});
        } catch (_e) {
          reply = null;
        }
        seen[i] = parseUsage(reply);
        if (!drawn && usageComplete(seen[i])) {  // every account read and fresh: drawn as it lands
          drawn = true;
          this.rows = seen[i];
          cx.notify();
        }
      }));
      if (this.reading === pass) this.reading = 0;
      // the best machine's rows, not the first to answer: a host whose cache no feed refreshes answers stale rows
      const best = bestUsage(seen);
      const since = Date.now() - this.born;
      const rows = usageShown(this.rows, best, since < USAGE_WARM_MS);
      if (rows !== this.rows) {
        this.rows = rows;
        cx.notify();
      }
      if (usageRetry(best, since)) cx.timer.after(USAGE_RETRY_MS, (cx) => this.readUsage(cx));
    });
  }

  sync(cx) {
    cx.spawn(async () => {
      try {
        await exec("shoal", ["ui", "app-sync"]);
      } catch (_e) {
        // the next tick tries again; `shoal ui app-open` does the same by hand
      }
    });
  }

  lattice(cx) {
    // an open lattice tab is a switch away: no process (`ui app-lattice` took a second or more, and more over ssh)
    const sps = safe(spaces, []);
    if (this.go(latticeTab(sps))) return;
    // the global lattice, in the home space, drilled into the shoal in view (the lattice honors lattice.focus)
    const sid = shoalInView(sps, safe(terminals, []));
    const home = homeTab(sps, "SHOAL-home", "lattice");
    if (home) {
      if (sid) try { setShared("lattice.focus", { shoal: sid, t: Date.now() }); } catch (_e) { /* the lattice opens on its home view */ }
      if (this.go(home)) return;
    }
    this.show(cx, "lattice", "app-lattice");
  }

  home(cx) {
    if (this.go(homeTab(safe(spaces, [])))) return;
    this.show(cx, "home", "app-home");
  }

  board(cx) {
    // the board graphs, on the shoal of the space in view (the board honors board.focus), a tab of the home space
    const sps = safe(spaces, []);
    const sid = shoalInView(sps, safe(terminals, []));
    if (sid) try { setShared("board.focus", { shoal: sid, t: Date.now() }); run("shoal-board.focus"); } catch (_e) { /* it opens on its own pick */ }
    if (this.go(homeTab(sps, "SHOAL-home", "board"))) return;
    this.show(cx, "board", "app-board");
  }

  usage(cx) {
    // the dashboard is a tab of the home space: a switch away when it is open, else the CLI makes it
    if (this.go(homeTab(safe(spaces, []), "SHOAL-home", "usage"))) return;
    this.show(cx, "usage", "app-usage");
  }

  /** Shows a space's tab at once; false when there is none to show (the CLI opens one). */
  go(where) {
    if (!where) return false;
    try {
      run("space.select", { id: where.space });
      run("tab.select", { id: where.tab });
      this.note = null;
      return true;
    } catch (_e) {
      return false;
    }
  }

  /** The next agent that needs you, its pane focused; a zoomed view stays zoomed, on it (as a click in the agents list). */
  nextNeedsYou() {
    const list = safe(agents, []);
    const kinds = new Map(safe(terminals, []).map((x) => [x.address, x.report?.kind ?? null]));
    const a = nextNeedsYou(list, (x) => kinds.get(x.address), list.find((x) => x.focused)?.address);
    if (!a) {
      this.note = "nobody needs you";
      return;
    }
    this.note = null;
    const all = safe(panes, []);
    const there = all.find((p) => p.pane === a.pane);
    const zoomed = all.some((p) => p.zoomed && (p.tab === there?.tab || p.focused));
    run("pane.focus", { pane: a.pane });
    if (zoomed && !there?.zoomed) run("pane.zoom", { pane: a.pane });
  }

  show(cx, what, verb) {
    this.note = `opening ${what}`;
    cx.spawn(async (cx) => {
      let r = null;
      try {
        r = await exec("shoal", ["ui", verb]);
      } catch (e) {
        r = { code: 1, stderr: String(e) };
      }
      this.note = r?.code === 0 ? null : `${what}: ${String(r?.stdout || r?.stderr || "failed").trim().slice(0, 90)}`;
      cx.notify();
    });
  }

  meter(u, t) {
    const row = h_flex().id(`u-${u.name}`).gap(5).items_center().flex_none()
      .tooltip(usageTip(u, Date.now() / 1000))
      .child(div().text_color(u.stale ? t.dim : t.muted).child(u.name));
    // a reading too old to draw: an empty bar and an ellipsis, not its old percentages (the hover says how old)
    if (u.old) return row.child(div().w(42).h(6).rounded(3).bg(t.border)).child(div().text_size(10).text_color(t.dim).child("…"));
    if (u.error) return row.child(div().text_color(t.dim).child("?"));
    for (const w of u.windows) {
      row.child(h_flex().gap(3).items_center()
        .child(div().text_size(10).text_color(t.dim).child(w.name))
        // a long bar: 29% has to read differently from 5% at a glance
        .child(div().w(42).h(6).rounded(3).bg(t.border).overflow_hidden()
          .child(div().h(6).rounded(3).bg(w.color).opacity(u.stale ? 0.45 : 1).w(Math.max(w.pct > 0 ? 2 : 0, Math.round(0.42 * w.pct)))))
        .child(div().w(26).text_size(10).text_color(t.muted).child(`${Math.round(w.pct)}%`)));
    }
    return row;
  }

  button(id, text, tip, onClick, t) {
    return div().id(id).flex_none().px(7).py(1).rounded(4).cursor_pointer().text_color(t.foreground)
      .border_1().border_color(t.border).hover((s) => s.bg(t.hover)).tooltip(tip).on_click(onClick).child(text);
  }

  render() {
    const t = theme();
    const space = safe(spaces, []).find((s) => s.active);
    const list = safe(agents, []);
    const needs = list.filter((a) => a.status === "blocked").length;
    // the shoal layout says so when its top bar is shoal's copy (`ui app-install` sets this block's props.topbar)
    const topBar = safe(props, {})?.topbar === true;
    const bar = h_flex().size_full().overflow_hidden().px(14).gap(14).items_center().bg(t.chrome).border_t(1).border_color(t.border).text_size(11.5).text_color(t.muted)
      .child(div().flex_none().child(space?.machine ?? "This computer"))
      .child(h_flex().flex_none().gap(6).items_center().child(`${list.length} agents`)
        .when(needs > 0, (el) => el.child(div().id("to-next").cursor_pointer().text_color(t.blocked).tooltip("the next one (Ctrl+Alt+N)")
          .on_click((_e, cx) => { this.nextNeedsYou(); cx.notify(); }).child(`· ${needs} need you`))))
      // Home and Lattice live in the top bar (shoal-tabs); here only when that copy is not in the layout (a Herdr release moved it)
      .when(!topBar, (el) => el
        .child(this.button("to-home", "⌂ Home", "the SHOAL-home space on this computer", (_e, cx) => { this.home(cx); cx.notify(); }, t))
        .child(this.button("to-lattice", "◎ Lattice", "the lattice of this space, or the global one (Ctrl+Alt+L)", (_e, cx) => { this.lattice(cx); cx.notify(); }, t)))
      .when(!topBar, (el) => el.child(this.button("to-actions", "Actions", "shoal actions (Ctrl+Alt+S)", () => run("mod.open", { name: "shoal-actions" }), t))
        .child(this.button("to-board", "Board", "the board's dependency graph", (_e, cx) => { this.board(cx); cx.notify(); }, t))
        .child(this.button("to-usage", "Usage", "the usage dashboard", (_e, cx) => { this.usage(cx); cx.notify(); }, t)))
      .when(Boolean(this.note), (el) => el.child(div().min_w(0).truncate().text_color(t.dim).child(this.note)))
      .child(div().flex_1());
    if (this.rows === null) {  // no reading has landed yet: a pulsing notice, never the old percentages a cache holds
      bar.child(div().id("u-loading").flex_none().text_size(10.5).text_color(t.dim).tooltip("reading usage from each machine")
        .loop("opacity", { from: 0.35, to: 1, period: 1200, easing: "ease-in-out" }).child("usage …"));
      return bar;
    }
    for (const u of this.rows) bar.child(this.meter(u, t));
    return bar;
  }
}
