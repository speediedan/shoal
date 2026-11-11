// shoal-usage: the subscription usage dashboard (Herdr app mod), a tab of the home space opened by the top bar's Usage.
//
// A thin View: model.js turns `shoal ui usage --json` (cached meters, no model turns) into cards, each window with its
// reset and its pace, the tightest meter as the headline, and the decomposition panels (by shoal, by role, standalone
// seats) that fill in once the document carries a `breakdown`. It reads on a timer and on Refresh (`--refresh`: the
// providers' account APIs, still no model turn), from the first machine with a usage cache.
//
// props:
//   source: {argv?: [...], on?: machine} (default: shoal ui usage --json, tried on every machine, then here)
//   poll_ms: read interval (default 60000, at least 15000)

import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { exec, props, theme, machines, nextCommand } from "herdr";
import { cards, tightest, windowNote, breakdownRows, updatedAge, parseReply, usageArgv, duration, BREAKDOWNS, localClock } from "./model.js";

const UI = "Geist";
const MONO = "Geist Mono";
const CARD_W = 318;

const safe = (f, d) => {
  try {
    return f() ?? d;
  } catch (_e) {
    return d;
  }
};

function colors() {
  const t = safe(theme, {});
  const pick = (...names) => names.map((n) => t[n]).find((v) => typeof v === "string" && v);
  return {
    bg: pick("canvas", "background") ?? "#0b1016", panel: pick("surface", "raised") ?? "#121a22",
    raised: pick("raised", "surface") ?? "#18222c", border: pick("border") ?? "#24313d", track: pick("border") ?? "#24313d",
    ink: pick("foreground") ?? "#d8e2ea", muted: pick("muted") ?? "#93a4b3", dim: pick("dim", "muted") ?? "#5f7282",
    ok: "#5cc8ff", warn: "#ffb547", high: "#ff4d5e", accent: pick("accent") ?? "#5cc8ff", hover: pick("hover") ?? "#1f2b36",
  };
}

export default class Usage extends View {
  init(_props, cx) {
    const p = safe(props, {}) ?? {};
    this.source = p.source && typeof p.source === "object" ? p.source : {};
    this.doc = null;
    this.readAt = null;
    this.note = "reading usage…";
    this.busy = false;
    this.read(cx, false);
    cx.timer.every(Math.max(15000, Number(p.poll_ms) || 60000), (cx) => this.read(cx, false));
    cx.spawn(async (cx) => {
      for (;;) {
        if ((await nextCommand()) === "refresh") this.read(cx, true);
        cx.notify();
      }
    });
  }

  /** Where to read: the source's machine, else every machine and then this computer (not every host keeps the cache). */
  where() {
    if (this.source.on !== undefined) return [this.source.on ?? null];
    const names = safe(machines, []).map((m) => m?.name).filter((n) => typeof n === "string" && n);
    return [...new Set([...names, null])];
  }

  read(cx, refresh) {
    if (this.busy) return;
    this.busy = true;
    if (refresh) this.note = "refreshing from the account APIs…";
    cx.spawn(async (cx) => {
      let got = null;
      for (const on of this.where()) {
        let reply = null;
        try {
          reply = await exec(usageArgv(this.source, refresh)[0], usageArgv(this.source, refresh).slice(1), on ? { on } : {});
        } catch (_e) {
          reply = null;
        }
        const doc = parseReply(reply);
        if (doc && doc.accounts.some((a) => Array.isArray(a?.rows) && a.rows.length)) {
          got = doc;
          break;
        }
        got = got ?? doc;
      }
      this.busy = false;
      if (got) {
        this.doc = got;
        this.readAt = Date.now() / 1000;
        this.note = null;
      } else if (!this.doc) {
        this.note = "no usage reading on any machine yet (`shoal ui usage --refresh` reads one)";
      } else {
        this.note = "the last read failed: showing the previous reading";
      }
      cx.notify();
    });
  }

  bar(id, w, c) {
    const fill = c[w.severity];
    const track = div().id(id).relative().h(8).w_full().rounded(4).bg(c.track).overflow_hidden()
      .child(div().absolute().top(0).left(0).h(8).rounded(4).bg(fill).w(Math.max(w.pct > 0 ? 3 : 0, Math.round((CARD_W - 40) * w.pct / 100))));
    if (w.elapsed !== null) {  // where an even pace would be now: a fill past this mark is running ahead of the window
      track.child(div().absolute().top(0).h(8).w(2).bg(c.ink).opacity(0.55).left(Math.round((CARD_W - 40) * w.elapsed)));
    }
    return track;
  }

  windowBlock(cardId, w, c) {
    return v_flex().id(`${cardId}-${w.key}`).gap(5)
      .child(h_flex().items_end().gap(8)
        .child(div().text_size(12).text_color(c.muted).child(w.label))
        .child(div().flex_1())
        .child(div().font_family(MONO).text_size(22).font_semibold().text_color(w.severity === "ok" ? c.ink : c[w.severity]).child(`${Math.round(w.pct)}%`)))
      .child(this.bar(`${cardId}-${w.key}-bar`, w, c))
      .child(div().text_size(11).text_color(c.dim).child(windowNote(w, localClock(), Date.now() / 1000) || " "));
  }

  card(k, c) {
    const box = v_flex().id(`card-${k.id}`).w(CARD_W).flex_none().gap(14).p(16).rounded(12).bg(c.panel).border_1()
      .border_color(k.blocked ? c.high : c.border)
      .child(h_flex().items_center().gap(8)
        .child(div().text_size(15).font_semibold().text_color(c.ink).child(k.title))
        .when(Boolean(k.plan), (el) => el.child(div().px(7).py(1).rounded(9).bg(c.raised).text_size(10.5).text_color(c.muted).child(k.plan)))
        .child(div().flex_1())
        .when(k.stale, (el) => el.child(div().text_size(10.5).text_color(c.warn).tooltip("this reading is older than its poll interval").child("stale")))
        .when(k.blocked, (el) => el.child(div().text_size(10.5).text_color(c.high).child("blocked"))));
    for (const w of k.windows) box.child(this.windowBlock(k.id, w, c));
    if (k.error) box.child(div().text_size(11.5).text_color(k.windows.length ? c.dim : c.warn).child(k.error));
    box.child(div().text_size(10.5).text_color(c.dim)
      .child([k.source, k.ageS !== null ? `read ${duration(k.ageS)} ago` : ""].filter(Boolean).join(" · ") || " "));
    return box;
  }

  breakdown(b, c) {
    const rows = breakdownRows(this.doc, b.key);
    const box = v_flex().id(`bd-${b.key}`).w(CARD_W).flex_none().gap(10).p(16).rounded(12).bg(c.panel).border_1().border_color(c.border)
      .child(div().text_size(13.5).font_semibold().text_color(c.ink).child(b.title));
    if (rows === null) {
      return box.child(div().text_size(11.5).text_color(c.dim)
        .child("Not collected yet: this fills in once shoal records each session's usage."));
    }
    if (!rows.length) return box.child(div().text_size(11.5).text_color(c.dim).child("No usage recorded in this window."));
    rows.slice(0, 8).forEach((r, i) => box.child(v_flex().id(`bd-${b.key}-${i}`).gap(3)
      .child(h_flex().gap(6).text_size(11.5)
        .child(div().min_w(0).truncate().text_color(c.ink).child(r.name))
        .when(Boolean(r.provider), (el) => el.child(div().text_color(c.dim).child(r.provider)))
        .child(div().flex_1())
        .child(div().font_family(MONO).text_color(c.muted).child(`${Math.round(r.share * 100)}%`)))
      .child(div().h(5).w_full().rounded(3).bg(c.track).overflow_hidden()
        .child(div().h(5).rounded(3).bg(c.accent).w(Math.max(2, Math.round((CARD_W - 32) * r.share)))))));
    return box;
  }

  render(_cx) {
    const c = colors();
    const now = Date.now() / 1000;
    const list = this.doc ? cards(this.doc, now) : [];
    const top = tightest(list);
    const age = this.doc ? updatedAge(this.doc) : null;
    const root = v_flex().id("usage").size_full().min_h(0).bg(c.bg).font_family(UI).text_color(c.ink)
      .overflow_y_scrollbar().px(28).py(22).gap(18);
    root.child(h_flex().items_center().gap(12)
      .child(v_flex().gap(2)
        .child(div().text_size(22).font_semibold().child("Usage"))
        .child(div().text_size(12).text_color(c.muted).child("Subscription meters from each provider's account API. Reading them takes no model turn.")))
      .child(div().flex_1())
      .when(age !== null, (el) => el.child(div().text_size(11.5).text_color(c.dim).child(`updated ${duration(age)} ago`)))
      .child(div().id("refresh").px(12).py(5).rounded(7).cursor_pointer().border_1().border_color(c.border).bg(c.panel)
        .hover((s) => s.bg(c.hover)).text_size(12).text_color(this.busy ? c.dim : c.ink)
        .tooltip("read the account APIs now (no model turn)")
        .on_click((_e, cx) => { this.read(cx, true); cx.notify(); }).child(this.busy ? "Reading…" : "↻ Refresh")));
    if (top) {
      const w = top.window;
      root.child(h_flex().id("headline").gap(10).items_center().px(14).py(10).rounded(10).bg(c.panel).border_1()
        .border_color(w.severity === "ok" ? c.border : c[w.severity])
        .child(div().w(8).h(8).rounded(4).bg(c[w.severity]))
        .child(div().text_size(12.5).text_color(c.muted).child("Tightest"))
        .child(div().text_size(12.5).font_semibold().child(`${top.card.title} ${w.label.toLowerCase()} at ${Math.round(w.pct)}%`))
        .child(div().text_size(12).text_color(c.dim).child(windowNote(w, localClock(), now))));
    }
    if (this.note) root.child(div().text_size(12).text_color(c.dim).child(this.note));
    const grid = h_flex().id("cards").flex_wrap().gap(16).items_start();
    for (const k of list) grid.child(this.card(k, c));
    root.child(grid);
    root.child(v_flex().gap(4).pt(6)
      .child(div().text_size(15).font_semibold().child("Where it goes"))
      .child(div().text_size(12).text_color(c.muted).child("Usage split by shoal, by role and by standalone seat, for the current windows.")));
    const bd = h_flex().id("breakdowns").flex_wrap().gap(16).items_start();
    for (const b of BREAKDOWNS) bd.child(this.breakdown(b, c));
    root.child(bd);
    return root;
  }
}
