// shoal-status: the window's status bar in the shoal layout (Herdr app mod). Where you are and how many agents need you,
// as in Herdr's own bar, plus two things that are always within reach: the way back to a lattice (a button and
// Ctrl+Alt+L: `shoal ui app-lattice` reopens a closed tab) and each provider's usage. Reads usage from the cached
// `shoal ui usage --json` (no model turns), and on a slow timer runs `shoal ui app-sync`, so roles tabs follow the roles.

import { View, div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { spaces, agents, machines, theme, run, exec, nextCommand } from "herdr";
import { parseUsage, usageMachines } from "./model.js";

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
    this.rows = [];
    this.note = null;
    this.readUsage(cx);
    cx.timer.every(USAGE_MS, (cx) => this.readUsage(cx));
    cx.timer.every(SYNC_MS, (cx) => this.sync(cx));
    cx.spawn(async (cx) => {
      for (;;) {
        if ((await nextCommand()) === "lattice") this.lattice(cx);
        cx.notify();
      }
    });
  }

  readUsage(cx) {
    const where = usageMachines(safe(spaces, []), safe(machines, []).map((m) => m.name));
    cx.spawn(async (cx) => {
      for (const on of where) {
        let reply = null;
        try {
          reply = await exec("shoal", ["ui", "usage", "--json"], on ? { on } : {});
        } catch (_e) {
          reply = null;
        }
        const rows = parseUsage(reply);
        if (rows?.length) {  // a host with no usage cache says nothing: the last good rows stay until one has some
          this.rows = rows;
          cx.notify();
          return;
        }
      }
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
    this.note = "opening the lattice";
    cx.spawn(async (cx) => {
      let r = null;
      try {
        r = await exec("shoal", ["ui", "app-lattice"]);
      } catch (e) {
        r = { code: 1, stderr: String(e) };
      }
      this.note = r?.code === 0 ? null : `lattice: ${String(r?.stdout || r?.stderr || "failed").trim().slice(0, 90)}`;
      cx.notify();
    });
  }

  meter(u, t) {
    const row = h_flex().id(`u-${u.name}`).gap(5).items_center().flex_none()
      .tooltip(u.error || `${u.name}: ${u.windows.map((w) => `${w.name} ${Math.round(w.pct)}%`).join(", ")}${u.stale ? " (stale)" : ""}`)
      .child(div().text_color(u.stale ? t.dim : t.muted).child(u.name));
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
    const bar = h_flex().size_full().overflow_hidden().px(14).gap(14).items_center().bg(t.chrome).border_t(1).border_color(t.border).text_size(11.5).text_color(t.muted)
      .child(div().flex_none().child(space?.machine ?? "This computer"))
      .child(h_flex().flex_none().gap(6).items_center().child(`${list.length} agents`)
        .when(needs > 0, (el) => el.child(div().text_color(t.blocked).child(`· ${needs} need you`))))
      .child(this.button("to-lattice", "◎ Lattice", "the lattice of this space, or the global one (Ctrl+Alt+L)", (_e, cx) => { this.lattice(cx); cx.notify(); }, t))
      .child(this.button("to-actions", "Actions", "shoal actions (Ctrl+Alt+S)", () => run("mod.open", { name: "shoal-actions" }), t))
      .when(Boolean(this.note), (el) => el.child(div().min_w(0).truncate().text_color(t.dim).child(this.note)))
      .child(div().flex_1());
    for (const u of this.rows) bar.child(this.meter(u, t));
    return bar;
  }
}
