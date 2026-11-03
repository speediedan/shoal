// shoal-home: the shoal splash and host chooser, drawn natively (Herdr app mod).
//
// A thin View. The decisions live in pure modules that also run under Node: model.js (the launch
// list to cards, keys, open results) and layout.js (where the logo, creatures and cards go for a
// window size); motion.js is lattice-view's motion budget, copied verbatim (a test keeps the two
// identical). This file reads the host list on a gentle timer, draws it and handles input.
//
// It reads one list, `shoal ui launch --list --json --summary`, and runs one command when a card
// is clicked: `shoal ui app-open --machine <name>` here (that verb creates or focuses the host's
// shoal spaces and remembers the choice). Nothing else is read, and nothing from the environment
// is shown or logged.
//
// props:
//   source: {kind: "exec", argv: [...], on} (default argv: shoal ui launch --list --json --summary,
//           here) | {kind: "fixture", path?} (the bundled fixtures/launch-list.json; a click then
//           only says what it would run)
//   motion: "auto" (default) | "full" | "balanced" | "low" | "off" (motion.js). Only `full` moves
//           anything: a slow native drift of the creatures, stopped for good once a host is opened.
//   renderer: "cpu" | "gpu" (a launcher hint for auto; else read from the app process)
//   art: false draws the text screen (logo and host list, no creatures)
//   poll_ms: list poll interval (default 20000, at least 10000)

import { View, div, image, PathBuilder, Background } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { exec, readTextFile, appHome, props, theme, fonts, nextCommand, window as appWindow } from "herdr";
import * as fs from "fs/promises";
import {
  PALETTE, DOT, parseList, listSignature, hostState, canOpen, initialSelection, keepSelection, keyAction,
  openArgv, openResult, cardLines, shoalCounts, hostNeedsYou, withAlpha,
} from "./model.js";
import { homeLayout, cardHeight, CREATURES, CARD, PAD } from "./layout.js";
import { resolveTier, motionPolicy, rendererFromProcess, MOTION_CYCLE } from "./motion.js";
import { oceanScene, logoLetters, rippleBands, LETTER_SHADES, SEA } from "./ocean.js";

const MOD = "shoal-home";
const DEFAULT_ARGV = ["shoal", "ui", "launch", "--list", "--json", "--summary"];
const P = PALETTE;
const UI = "Geist";
const MONO = "Geist Mono";
const TAGLINE = ["Stigmergic", "Heterogeneous", "Open", "Agent", "Lattice"];

function normalizeSource(src) {
  const s = src && typeof src === "object" ? src : { kind: "exec" };
  if (s.kind === "fixture") return { kind: "fixture", path: typeof s.path === "string" ? s.path : null };
  const argv = Array.isArray(s.argv) && s.argv.length ? s.argv.map(String) : DEFAULT_ARGV;
  return { kind: "exec", argv, on: s.on ?? null };
}

const lastLine = (s) => String(s ?? "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean).pop() ?? "";
const dot = (color, size = 8) => div().w(size).h(size).flex_none().rounded(size / 2).bg(color);

function rectPath(w, h) {
  return PathBuilder.fill().move_to(0, 0).line_to(w, 0).line_to(w, h).line_to(0, h).close().build();
}

/** One ocean.js item as a full-canvas paint_path layer. */
function seaEl(it) {
  let b = it.fill ? PathBuilder.fill() : PathBuilder.stroke(it.width ?? 1);
  for (const c of it.cmds) {
    switch (c[0]) {
      case "M": b = b.move_to(c[1], c[2]); break;
      case "L": b = b.line_to(c[1], c[2]); break;
      // display list: C c1x c1y c2x c2y x y; PathBuilder: to first, then the two controls
      case "C": b = b.cubic_bezier_to(c[5], c[6], c[1], c[2], c[3], c[4]); break;
      case "A": b = b.arc_to(c[1], c[2], c[3], c[4], c[5], c[6], c[7]); break;
      case "Z": b = b.close(); break;
      default: break;
    }
  }
  return window.paint_path(b.build(), Background.solid(withAlpha(it.color, it.alpha))).absolute().top(0).left(0).size_full();
}

// A display serif reads as a ship's name; the shipped Geist when it is not installed.
const LOGO_FONTS = ["Noto Serif Display", "DejaVu Serif", "Liberation Serif"];
let logoFont = null;
function logoFamily() {
  if (logoFont === null) {
    let have = [];
    try { have = fonts(); } catch (_e) { have = []; }
    logoFont = LOGO_FONTS.find((f) => have.includes(f)) ?? UI;
  }
  return logoFont;
}

export default class Home extends View {
  init(_props, cx) {
    const p = props() ?? {};
    this.source = normalizeSource(p.source);
    this.motionReq = MOTION_CYCLE.includes(p.motion) ? p.motion : "auto";
    this.renderer = p.renderer === "cpu" || p.renderer === "gpu" ? p.renderer : null;
    this.artReq = p.art !== false;
    this.art = null; // null until the assets are checked; false when one is missing
    this.pollMs = Math.min(300000, Math.max(10000, Number(p.poll_ms) || 20000));
    this.size = { width: 1100, height: 760 };
    this.list = null;
    this.sig = null;
    this.error = null;
    this.sel = 0;
    this.status = new Map();
    this.busy = null;
    this.opened = false;
    this.loading = false;
    this.handle = cx.focus_handle();
    if (!this.renderer) this.probeRenderer(cx);
    this.checkAssets(cx);
    this.load(cx);
    cx.timer.every(this.pollMs, (cx) => this.load(cx));
    cx.spawn(async (cx) => {
      for (;;) {
        const c = await nextCommand();
        if (c === "focus") this.handle.focus();
        else if (c === "refresh") this.load(cx);
        else if (c === "open") this.open(this.sel, cx);
        cx.notify();
      }
    });
  }

  // ---- motion

  probeRenderer(cx) {
    cx.spawn(async (cx) => {
      try {
        if (appWindow()?.platform !== "linux") return;
        const env = (await readTextFile("/proc/self/environ")) ?? "";
        let hint = rendererFromProcess(env);
        if (!hint) hint = rendererFromProcess(env, (await readTextFile("/proc/self/maps")) ?? "");
        if (hint) { this.renderer = hint; cx.notify(); }
      } catch (_e) {
        // no hint: auto stays at balanced, which does not move the creatures
      }
    });
  }

  /** Drift only at the full tier, only with art, and never again once a host was opened. */
  drifting(mode) {
    const tier = resolveTier(this.motionReq, { renderer: this.renderer, focused: appWindow()?.active !== false });
    this.tier = tier;
    return mode === "art" && !this.opened && motionPolicy(tier.tier).loops;
  }

  // ---- data

  checkAssets(cx) {
    cx.spawn(async (cx) => {
      let ok = true;
      try {
        for (const c of CREATURES) {
          if (!(await fs.exists(`${appHome()}/mods/${MOD}/assets/${c.file}`))) { ok = false; break; }
        }
      } catch (_e) {
        ok = true; // no way to ask: draw the images, a missing one just shows nothing
      }
      if (this.art !== ok) { this.art = ok; cx.notify(); }
    });
  }

  load(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      const before = this.error;
      let changed = false;
      try {
        const list = parseList(await this.fetchList());
        const sig = listSignature(list);
        if (sig !== this.sig) {
          this.sel = this.list ? keepSelection(this.list.hosts, list, this.sel) : initialSelection(list);
          this.list = list;
          this.sig = sig;
          changed = true;
        }
        this.error = null;
      } catch (e) {
        this.error = String(e?.message ?? e).slice(0, 200);
      } finally {
        this.loading = false;
      }
      if (changed || this.error !== before) cx.notify();
    });
  }

  async fetchList() {
    const s = this.source;
    if (s.kind === "fixture") {
      const text = await readTextFile(s.path ?? `${appHome()}/mods/${MOD}/fixtures/launch-list.json`);
      if (text == null) throw new Error("fixture not found");
      return text;
    }
    const r = await exec(s.argv[0], s.argv.slice(1), { on: s.on });
    if (r.code !== 0) throw new Error(`host list exit ${r.code}: ${lastLine(r.stderr) || lastLine(r.stdout) || "no output"}`);
    return r.stdout;
  }

  open(i, cx) {
    const h = this.list?.hosts?.[i];
    if (!h || !canOpen(h) || this.busy) return;
    this.sel = i;
    this.busy = h.name;
    this.opened = true; // every native animation redraws the window: the drift stops for good
    this.status.set(h.name, { phase: "opening" });
    cx.notify();
    cx.spawn(async (cx) => {
      let res;
      if (this.source.kind === "fixture") {
        res = { ok: true, text: `fixture: would run shoal ${openArgv(h).join(" ")}` };
      } else {
        try {
          res = openResult(await exec("shoal", openArgv(h), { on: null }));
        } catch (e) {
          res = openResult(e instanceof Error ? e : new Error(String(e)));
        }
      }
      this.status.set(h.name, { phase: res.ok ? "ok" : "error", text: res.text });
      this.busy = null;
      cx.notify();
      if (res.ok) this.load(cx); // the list's `last` follows the choice
    });
  }

  key(e, cx) {
    const n = this.list?.hosts?.length ?? 0;
    const a = keyAction(e.keystroke, this.sel, n);
    if (!a) return;
    this.sel = a.sel;
    if (a.action === "open") this.open(this.sel, cx);
    else if (a.action === "refresh") this.load(cx);
    cx.notify();
  }

  // ---- render

  render(_cx) {
    const hosts = this.list?.hosts ?? [];
    const lines = hosts.map((h) => cardLines(h, this.status.get(h.name) ?? null));
    const L = homeLayout(this.size, lines.map((l) => cardHeight(l.length)), { art: this.artReq && this.art !== false });
    const drift = this.drifting(L.mode);
    const W = L.width;
    const H = Math.max(L.height, L.contentH);
    const scroll = L.contentH > L.height;

    const canvas = div().id("canvas").relative().w_full().h(H).flex_none().overflow_hidden()
      .child(window.paint_path(rectPath(W, H), Background.linear_gradient(165, P.bgTop, P.bgBottom)).absolute().top(0).left(0).size_full())
      .child(window.paint_path(rectPath(W, H), Background.linear_gradient(35, withAlpha(P.navy, 0.7), withAlpha(P.navy, 0))).absolute().top(0).left(0).size_full());

    // a soft static bloom behind the logo: stacked translucent ellipses (layered alpha: GPUI has
    // no blur or additive blending), static so it costs nothing after the first frame
    const lg = L.logo;
    const ecx = lg.x + lg.w / 2, ecy = lg.y + lg.h / 2;
    for (let k = 0; k < 6; k++) {
      const rx = lg.w * (1.25 - k * 0.15), ry = lg.h * (1.5 - k * 0.18);
      const path = PathBuilder.fill().move_to(ecx - rx, ecy).arc_to(rx, ry, 0, false, true, ecx + rx, ecy)
        .arc_to(rx, ry, 0, false, true, ecx - rx, ecy).close().build();
      canvas.child(window.paint_path(path, Background.solid(withAlpha(k < 3 ? P.blue : P.cyan, 0.022))).absolute().top(0).left(0).size_full());
    }

    // the sea: kelp strands whose floats and tendrils make a network with the creatures (ocean.js)
    const sea = oceanScene(L);
    for (const it of sea?.items ?? []) {
      let el = seaEl(it);
      if (drift && it.sway) el = el.loop("left", { from: 0, to: it.sway.amp, period: it.sway.period, easing: "ease-in-out" });
      canvas.child(el);
    }

    for (const c of L.creatures) {
      let im = image(`mods/${MOD}/assets/${c.file}`).id(`fish-${c.name}`).absolute().left(c.x).top(c.y).w(c.size).h(c.size);
      if (drift) im = im.loop("top", { from: c.y, to: c.y + c.drift, period: c.period, easing: "ease-in-out" });
      canvas.child(im);
    }

    canvas.child(this.logoEl(L));
    if (L.tagline) {
      // Stigmergic · Heterogeneous · Open · Agent · Lattice, each initial underlined (wavy: we are under the sea)
      const row = h_flex().absolute().left(0).top(L.tagline.y).w(W).h(L.tagline.h).items_center().justify_center()
        .text_size(L.tagline.size).text_color(withAlpha(P.amber, 0.86));
      TAGLINE.forEach((word, i) => {
        if (i) row.child(div().px(Math.round(L.tagline.size * 0.45)).text_color(withAlpha(P.amber, 0.45)).child("·"));
        row.child(div().font_semibold().text_color(P.amber).underline().text_decoration_wavy().child(word[0]));
        row.child(div().child(word.slice(1)));
      });
      canvas.child(row);
    }
    const ask = this.error && !this.list ? `host list: ${this.error}` : this.list ? (hosts.length ? "which host serves the view?" : "no hosts in the list") : "reading the host list…";
    canvas.child(div().absolute().left(PAD).top(L.ask.y).w(W - 2 * PAD).h(L.ask.h).flex().items_center().justify_center().truncate()
      .text_size(L.ask.size).font_semibold().text_color(this.error && !this.list ? P.red : P.ink).child(ask));

    hosts.forEach((h, i) => { if (L.cards[i]) this.cardEls(canvas, h, i, L.cards[i], lines[i]); });

    if (hosts.length) {
      const foot = this.error ? `list refresh failed, showing the last good list: ${this.error}`
        : "click a host to open · ↑↓ or j k move · 1-9 jump · Enter open · r refresh";
      canvas.child(div().absolute().left(PAD).top(L.foot.y).w(W - 2 * PAD).h(L.foot.h).flex().items_center().justify_center().truncate()
        .text_size(10.5).text_color(this.error ? P.red : P.faint).child(foot));
    }
    canvas.child(this.refreshEl(L.refresh));

    return div().id("root").size_full().bg(P.bgBottom).font_family(UI).text_color(P.ink)
      .when(scroll, (el) => el.overflow_y_scrollbar())
      .track_focus(this.handle)
      .on_key_down((e, cx) => this.key(e, cx))
      .on_mouse_down("left", (_e, _cx) => this.handle.focus())
      .on_size_change((s, cx) => {
        if (Math.abs(s.width - this.size.width) < 0.5 && Math.abs(s.height - this.size.height) < 0.5) return;
        this.size = s;
        cx.notify();
      })
      .child(canvas);
  }

  logoEl(L) {
    const lg = L.logo;
    const fam = logoFamily();
    // one letter, its box at (x, y) relative to `top` (a band's clip offset)
    const glyph = (l, dx, dy, color, top = 0) => div().absolute().left(dx).top(dy - top).w(l.w).h(l.h)
      .flex().items_center().justify_center().whitespace_nowrap().font_family(fam).text_size(lg.font)
      .line_height(`${l.h}px`).font_weight(800).text_color(color).child(l.ch);
    // Oceanic and still plain to read: every letter rides its own crest of one wave (GPUI cannot warp text), with a
    // deep drop shadow, a navy outline (eight offset copies: text has no stroke), and underwater light shading
    // each letter from aqua at the top to deep blue below, in thin clipped bands so there is no seam.
    const o = Math.max(1.5, lg.font / 34);
    const depth = Math.max(3, Math.round(lg.font / 16));
    const out = div().absolute().left(0).top(0).size_full();
    const letters = logoLetters(lg);
    for (const l of letters) out.child(glyph(l, l.x + depth, l.y + depth, withAlpha(SEA.logoShadow, 0.7)));
    for (const l of letters) {
      for (const [dx, dy] of [[-1, -1], [0, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [0, 1], [1, 1]]) {
        out.child(glyph(l, l.x + dx * o, l.y + dy * o, SEA.logoOutline));
      }
    }
    // the shading: each letter is cut into vertical strips, and in each strip the band edges follow a ripple
    // (ocean.js rippleBands), so the light inside the letters moves like water
    for (const l of letters) {
      for (const c of rippleBands(l, lg.font, LETTER_SHADES.length)) {
        out.child(div().absolute().left(l.x + c.x).top(l.y + c.y0).w(c.w).h(c.y1 - c.y0).overflow_hidden()
          .child(glyph(l, -c.x, 0, LETTER_SHADES[c.k], c.y0)));
      }
    }
    return out;
  }

  refreshEl(b) {
    return h_flex().id("refresh").absolute().left(b.x).top(b.y).w(b.w).h(b.h).gap(6).px(8).items_center().justify_center()
      .rounded(6).cursor_pointer().text_size(11).text_color(P.dim).bg(withAlpha(P.steel0, 0.5)).border(1).border_color(withAlpha(P.steel1, 0.6))
      .hover((s) => s.bg(withAlpha(P.steel1, 0.6)).text_color(P.ink))
      .tooltip("read the host list again (r)")
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .on_click((_e, cx) => { this.load(cx); cx.notify(); })
      .child(div().child("↻"))
      .child(div().child(this.loading ? "reading" : "refresh"));
  }

  cardEls(canvas, h, i, box, lines) {
    const t = theme();
    const blocked = t.blocked ?? P.magenta;
    const st = hostState(h);
    const openable = canOpen(h);
    const isLast = this.list?.last === h.name;
    const selected = i === this.sel;
    const status = this.status.get(h.name);
    if (isLast) {
      // the ring on the last choice: a cyan halo just outside the card
      canvas.child(div().absolute().left(box.x - 5).top(box.y - 5).w(box.w + 10).h(box.h + 10).rounded(14)
        .bg(withAlpha(P.cyan, 0.05)).border(2).border_color(withAlpha(P.cyan, 0.6)));
    }
    const border = selected ? withAlpha(P.ink, 0.75) : withAlpha(P.steel1, 0.75);
    let card = v_flex().id(`host-${i}`).absolute().left(box.x).top(box.y).w(box.w).h(box.h).overflow_hidden()
      .p(CARD.pad).rounded(10).bg(withAlpha(P.panelSolid, 0.92)).border(selected ? 1.5 : 1).border_color(border);
    if (openable) {
      card = card.cursor_pointer().hover((s) => s.bg(withAlpha("#15212b", 0.96)))
        .on_click((_e, cx) => { this.handle.focus(); this.open(i, cx); cx.notify(); });
    } else {
      card = card.opacity(0.5).tooltip(h.hint ?? "unreachable now");
    }
    const ny = hostNeedsYou(h);
    let dotEl = div().id(`dot-${i}`).w(14).h(14).flex_none().flex().items_center().justify_center().child(dot(DOT[st], 10));
    if (st === "down") dotEl = dotEl.tooltip(h.hint ?? "unreachable now");
    else if (st === "unknown") dotEl = dotEl.tooltip("not probed yet");
    card = card.child(h_flex().h(CARD.head).gap(8).items_center()
      .child(dotEl)
      .child(div().flex_none().text_size(14.5).font_semibold().text_color(P.ink).child(h.name))
      .when(isLast, (el) => el.child(div().px(5).rounded(4).text_size(9.5).text_color(P.cyan).border(1).border_color(withAlpha(P.cyan, 0.5)).child("last")))
      .child(div().flex_1())
      .when(ny > 0, (el) => el.child(div().px(6).rounded(8).bg(blocked).text_color("#0b0b10").font_bold().text_size(10).child(`${ny} need you`)))
      .when(i < 9, (el) => el.child(div().w(14).text_size(11).font_family(MONO).text_color(selected ? P.ink : P.faint).child(String(i + 1)))));
    card = card.child(div().h(4));
    lines.forEach((ln, j) => { card = card.child(this.lineEl(ln, `l-${i}-${j}`, blocked, status)); });
    canvas.child(card);
  }

  lineEl(ln, id, blocked, _status) {
    const row = () => h_flex().id(id).h(CARD.line).gap(6).items_center().text_size(11).min_w(0);
    const clip = (text, color) => div().flex_1().min_w(0).truncate().text_color(color).child(text);
    switch (ln.kind) {
      case "sub":
        return row().child(div().flex_1().min_w(0).truncate().font_family(MONO).text_size(10.5).text_color(P.dim).child(ln.text));
      case "note":
        return row().when(ln.tone === "down", (el) => el.tooltip(ln.text)).child(clip(ln.text, ln.tone === "down" ? withAlpha(P.red, 0.95) : P.faint));
      case "shoal":
        return row()
          .child(dot(ln.tint, 6))
          .child(div().flex_none().font_semibold().text_color(ln.tint).child(ln.tag))
          .when(ln.needsYou > 0, (el) => el.child(div().flex_none().font_bold().text_color(blocked).child(`${ln.needsYou} need you`)))
          .child(clip(shoalCounts(ln), P.dim));
      case "more":
        return row().child(clip(ln.text, P.faint));
      case "pin":
        return row().child(clip(ln.text, P.amber));
      case "status": {
        const color = ln.tone === "error" ? P.red : ln.tone === "ok" ? P.mint : P.dim;
        const mark = ln.tone === "error" ? "✕ " : ln.tone === "ok" ? "✓ " : "";
        return row().tooltip(ln.text).child(clip(`${mark}${ln.text}`, color));
      }
      default:
        return row();
    }
  }
}
