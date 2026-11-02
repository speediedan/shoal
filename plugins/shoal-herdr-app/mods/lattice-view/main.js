// lattice-view: the shoal graph doc v1 as an animated, hoverable network (Herdr app mod).
//
// A thin View. All decisions live in pure modules that also run under Node: model.js (doc to
// view model and encodings), layout.js (deterministic layout), geom.js (curves, hit tests,
// pan/zoom) and scene.js (the display list). This file fetches the doc on a data timer, turns the
// display list into GPUI elements and handles input. Motion is native (.loop, .frames,
// .transition, .spin); the view redraws only on data ticks, hover target changes, pan and zoom,
// and at most every 100 ms while a changed layout cools.
//
// props:
//   source: {kind: "exec", argv: [...], on} | {kind: "file", path, on} | {kind: "fixture", path?}
//   mode: "full" (default) | "pulse" (one compact row per shoal, for a floating edge drawer)
//   motion: "auto" (default) | "full" | "balanced" | "low" | "off" (motion.js; `m` cycles)
//   budget: {max_dots, max_pulses, frame_ms, packets} overrides for the tier
//   renderer: "cpu" | "gpu" (a launcher hint for auto; else read from the app process)
//   items: false hides the satellite layer
//   poll_ms: data poll interval (default 4000)

import { View, div, svg, PathBuilder, Background } from "gpui-kit";
import { h_flex, v_flex, Link } from "gpui-base";
import { exec, readTextFile, appHome, props, theme, shared, setShared, nextCommand, placement, window as appWindow } from "herdr";
import {
  PALETTE, STATE_STYLE, EDGE_COLOR, buildModel, eventsSince, demoEvents, shoalSummaries, hostSummaries,
  cardLines, legendEntries, shortShoal, fmtAge, visualSignature, urgentSignature,
} from "./model.js";
import { Layout } from "./layout.js";
import { motionPolicy, resolveTier, rendererFromProcess, shouldRender, MOTION_CYCLE } from "./motion.js";
import { buildScene, headerText } from "./scene.js";
import { hitNode, hitDisc, panBy, zoomAt, wheelFactor, boundsOf, fitTransform, clamp, withAlpha } from "./geom.js";

const MOD = "lattice-view";
const RAIL_W = 272;
const FOCUS_KEY = "lattice-view.focus";
const P = PALETTE;
const MONO = "Geist Mono";

function normalizeSource(src) {
  const s = src && typeof src === "object" ? src : { kind: "fixture" };
  if (s.kind === "exec") {
    const argv = Array.isArray(s.argv) && s.argv.length ? s.argv.map(String) : ["shoal", "ui", "graph", "--json"];
    return { kind: "exec", argv, on: s.on ?? null };
  }
  if (s.kind === "file" && typeof s.path === "string") return { kind: "file", path: s.path, on: s.on ?? null };
  return { kind: "fixture", path: typeof s.path === "string" ? s.path : null };
}

function background(bg) {
  if (!bg || bg.kind === "solid") return Background.solid(bg?.color ?? "#000000");
  if (bg.kind === "linear") return Background.linear_gradient(bg.angle, bg.from, bg.to);
  if (bg.kind === "slash") return Background.pattern_slash(bg.color, bg.width, bg.interval);
  return Background.solid("#000000");
}

/** One display-list path as a full-canvas paint_path layer. */
function pathEl(it) {
  let b = it.fill ? PathBuilder.fill() : PathBuilder.stroke(it.width ?? 1);
  if (!it.fill && Array.isArray(it.dash) && it.dash.length) b = b.dash_array(it.dash);
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
  return window.paint_path(b.build(), background(it.bg)).absolute().top(0).left(0).size_full();
}

function boxEl(it) {
  let d = div().id(it.id).absolute().left(it.x).top(it.y).w(it.w).h(it.h);
  if (it.radius) d = d.rounded(it.radius);
  if (it.bg) d = d.bg(it.bg);
  if (it.border) d = d.border(it.border.w).border_color(it.border.color);
  if (typeof it.opacity === "number" && it.opacity !== 1) d = d.opacity(clamp(it.opacity, 0, 1));
  for (const l of it.loops ?? []) d = d.loop(l.prop, { from: l.from, to: l.to, period: l.period, easing: l.easing });
  for (const t of it.transitions ?? []) d = d.transition(t.prop, { duration: t.duration, easing: t.easing });
  return d;
}

function textEl(it) {
  let d = div().id(it.id).absolute().left(it.x).top(it.y).w(it.w).h(it.h).flex().items_center()
    .whitespace_nowrap().overflow_hidden().text_size(it.size).line_height(`${it.h}px`).text_color(it.color);
  d = it.align === "center" ? d.justify_center().text_center() : d.text_left();
  if (it.weight === "semibold") d = d.font_semibold();
  if (it.weight === "bold") d = d.font_bold();
  if (typeof it.opacity === "number" && it.opacity !== 1) d = d.opacity(it.opacity);
  if (it.frames) return d.frames(it.frames.list, it.frames.ms);
  return d.child(it.text);
}

function spinEl(it) {
  return svg(`mods/${MOD}/arc.svg`).absolute().left(it.x).top(it.y).size(it.size).text_color(it.color).spin(it.ms);
}

function el(it) {
  if (it.t === "path") return it.cmds.length ? pathEl(it) : null;
  if (it.t === "box") return boxEl(it);
  if (it.t === "text") return textEl(it);
  if (it.t === "spin") return spinEl(it);
  return null;
}

/** A tiny sparkline in a relative box. */
function sparkline(values, w, h, color) {
  const box = div().relative().w(w).h(h).flex_none();
  const vs = values.slice(-30);
  if (vs.length < 2) return box;
  const max = Math.max(1, ...vs);
  let b = PathBuilder.stroke(1.2);
  vs.forEach((v, i) => {
    const x = (i * (w - 2)) / (vs.length - 1) + 1;
    const y = h - 1 - (v / max) * (h - 3);
    b = i === 0 ? b.move_to(x, y) : b.line_to(x, y);
  });
  return box.child(window.paint_path(b.build(), color).absolute().top(0).left(0).size_full());
}

const dot = (color, size = 8) => div().w(size).h(size).flex_none().rounded(size / 2).bg(color);

export default class Lattice extends View {
  init(_props, cx) {
    const p = props() ?? {};
    this.mode = p.mode === "pulse" ? "pulse" : "full";
    this.source = normalizeSource(p.source);
    this.motionReq = MOTION_CYCLE.includes(p.motion) ? p.motion : "auto";
    this.budget = p.budget && typeof p.budget === "object" ? p.budget : null;
    this.renderer = p.renderer === "cpu" || p.renderer === "gpu" ? p.renderer : null;
    this.tier = { tier: "balanced", auto: true, why: "starting" };
    this.policy = motionPolicy("balanced", this.budget);
    this.items = p.items !== false;
    this.pollMs = clamp(Number(p.poll_ms) || 4000, 1500, 60000);
    this.doc = null;
    this.vm = null;
    this.lay = null;
    this.error = null;
    this.cursor = null;
    this.lastSeq = null;
    this.tick = 0;
    this.view = typeof p.view === "string" ? p.view : "global";
    this.layouts = new Map();
    this.tf = null;
    this.autoFit = true;
    this.size = { width: 1000, height: 700 };
    this.hover = null;
    this.pinned = null;
    this.press = null;
    this.drag = false;
    this.packets = [];
    this.legend = p.legend === true;
    this.history = new Map();
    this.hits = [];
    this.discs = [];
    this.cooling = null;
    this.batch = 0;
    this.focusSeen = null;
    this.pinned = typeof p.pin === "string" ? p.pin : null;
    this.handle = cx.focus_handle();
    if (!this.renderer) this.probeRenderer(cx);
    this.load(cx);
    cx.timer.every(this.pollMs, (cx) => this.load(cx));
    if (this.mode === "full") {
      cx.spawn(async (cx) => {
        for (;;) {
          const c = await nextCommand();
          if (c === "focus") this.handle.focus();
          else this.command(c, cx);
          cx.notify();
        }
      });
    }
  }

  // ---- motion

  /** Once: is this window drawn by a software renderer? Reads the app process's own environment. */
  probeRenderer(cx) {
    cx.spawn(async (cx) => {
      try {
        if (appWindow()?.platform !== "linux") return;
        const env = await readTextFile("/proc/self/environ");
        let hint = rendererFromProcess(env ?? "");
        if (!hint) hint = rendererFromProcess("", (await readTextFile("/proc/self/maps")) ?? "");
        if (hint) { this.renderer = hint; cx.notify(); }
      } catch (_e) {
        // no hint: auto stays at balanced
      }
    });
  }

  /** The tier for this render: auto adapts to the renderer, pulse mode, pane size and focus. */
  resolveMotion() {
    const size = this.canvasSize();
    this.tier = resolveTier(this.motionReq, {
      renderer: this.renderer,
      pulse: this.mode === "pulse",
      small: this.mode !== "pulse" && (size.width < 700 || size.height < 420),
      focused: appWindow()?.active !== false,
    });
    this.policy = motionPolicy(this.tier.tier, this.budget);
    return this.policy;
  }

  /** A status dot: steady, or blinking on the policy's interval when it asks for attention. */
  pulseDot(color, size, attention) {
    const p = this.policy;
    const base = div().w(size + 4).h(size + 4).flex_none().flex().items_center().justify_center().text_size(size + 3).text_color(color).line_height(`${size + 4}px`);
    if (attention && p.pulseMode === "loop") return dot(color, size).loop("opacity", { from: 0.3, to: 1, period: 850 });
    if (attention && p.pulseMode === "frames") return base.frames(["\u25cf", " "], p.frameMs);
    return dot(color, size);
  }

  // ---- data

  load(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      const hadError = this.error;
      let draw = false;
      try {
        const doc = await this.fetchDoc();
        if (doc) draw = this.accept(doc, cx);
        this.error = null;
      } catch (e) {
        this.error = String(e?.message ?? e).slice(0, 300);
      } finally {
        this.loading = false;
      }
      // a redraw is the expensive part on a software renderer: only when something shown changed
      if (draw || this.error !== hadError) {
        this.lastRender = Date.now();
        cx.notify();
      }
    });
  }

  async fetchDoc() {
    const s = this.source;
    if (s.kind === "fixture") {
      if (!this.fixture) {
        const text = await readTextFile(s.path ?? `${appHome()}/mods/${MOD}/fixtures/graph-v1.json`);
        if (text == null) throw new Error("fixture not found");
        this.fixture = JSON.parse(text);
      }
      const tick = this.tick++;
      if (tick === 0) return this.fixture;
      // demo: synthetic events so packets and flashes show without a live feed
      return { ...this.fixture, events: [...(this.fixture.events ?? []), ...demoEvents(this.fixture, tick)] };
    }
    if (s.kind === "file") {
      const text = await readTextFile(s.path, { on: s.on });
      if (text == null) throw new Error(`no graph doc at ${s.path}`);
      return JSON.parse(text);
    }
    const argv = [...s.argv];
    if (this.cursor) argv.push("--since", this.cursor);
    const r = await exec(argv[0], argv.slice(1), { on: s.on });
    if (r.code !== 0) throw new Error(`feed exit ${r.code}: ${(r.stderr || r.stdout || "").trim().slice(-240)}`);
    return JSON.parse(r.stdout);
  }

  accept(doc, cx) {
    if (!doc || typeof doc !== "object") throw new Error("graph doc is not an object");
    if (typeof doc.v === "number" && doc.v !== 1) throw new Error(`graph doc v${doc.v} is not supported (v1)`);
    this.doc = doc;
    if (typeof doc.cursor === "string") this.cursor = doc.cursor;
    for (const s of shoalSummaries(doc)) {
      const h = this.history.get(s.id) ?? [];
      h.push(s.msgsPerH);
      if (h.length > 30) h.shift();
      this.history.set(s.id, h);
    }
    this.rebuild(cx);
    const { fresh, lastSeq } = eventsSince(this.lastSeq, doc);
    this.lastSeq = lastSeq;
    const sig = visualSignature(this.vm);
    const urgentSig = urgentSignature(this.vm);
    const draw = shouldRender({ sig, urgentSig, lastSig: this.sig, lastUrgentSig: this.urgentSig, sinceMs: Date.now() - (this.lastRender ?? 0), renderMs: this.policy.renderMs });
    if (draw) {
      this.sig = sig;
      this.urgentSig = urgentSig;
    }
    return this.spawnPackets(fresh, cx) || draw;
  }

  rebuild(cx) {
    if (!this.doc) return;
    this.vm = buildModel(this.doc, this.view, { items: this.items });
    this.view = this.vm.view;
    let lay = this.layouts.get(this.view);
    if (!lay) {
      lay = new Layout();
      this.layouts.set(this.view, lay);
    }
    const cold = lay.pos.size === 0;
    lay.update(this.vm);
    // a topology change glides into place at 10 Hz; anything else settles at once
    if (!cold && lay.iters > 30 && cx && this.policy.any) this.cool(cx);
    else lay.run();
    this.lay = lay.result();
    if (this.autoFit || !this.tf) this.fit();
  }

  cool(cx) {
    if (this.cooling) return;
    this.cooling = cx.timer.every(100, (cx) => {
      const lay = this.layouts.get(this.view);
      if (lay) {
        lay.step(10);
        this.lay = lay.result();
        if (this.autoFit) this.fit();
      }
      if (!lay || lay.settled) {
        this.cooling?.cancel();
        this.cooling = null;
      }
      cx.notify();
    });
  }

  canvasSize() {
    const w = this.size.width >= 640 ? this.size.width - RAIL_W : this.size.width;
    return { width: Math.max(200, w), height: Math.max(160, this.size.height) };
  }

  fit() {
    if (!this.lay) return;
    const pts = [...this.lay.pos.values()].map((p) => ({ x: p.x, y: p.y, r: 30 }));
    for (const s of this.lay.sats) pts.push({ x: s.x, y: s.y, r: 8 });
    for (const c of this.lay.clusters) pts.push({ x: c.x, y: c.y, r: c.r + 26 });
    for (const a of this.lay.anchors?.values() ?? []) pts.push({ x: a.x, y: a.y, r: 40 });
    const size = this.canvasSize();
    this.tf = fitTransform(boundsOf(pts), size, 30, 1.6);
  }

  spawnPackets(fresh, cx) {
    if (!this.policy.packets || !fresh.length || !this.vm) return false;
    const batch = ++this.batch;
    let added = 0;
    for (const e of fresh) {
      const dst = typeof e.dst === "string" ? e.dst : null;
      const broadcast = e.class === "broadcast" || (dst && dst.endsWith(":*"));
      const kind = broadcast ? "broadcast" : !dst || e.type === "done" ? "done" : "packet";
      if (!this.vm.byId.has(e.src)) continue;
      if (kind === "packet" && !this.vm.byId.has(dst)) continue;
      const color = kind === "done" ? P.mint : kind === "broadcast" ? P.cyan : EDGE_COLOR[e.class] ?? EDGE_COLOR.msg;
      this.packets.push({ seq: e.seq, kind, src: e.src, dst, color, phase: "start", ms: kind === "packet" ? 1100 : 900, batch });
      added++;
    }
    if (!added) return false;
    // two renders: drawn at the source, then given the target so .transition moves it natively
    cx.timer.after(60, (cx) => {
      for (const p of this.packets) if (p.batch === batch) p.phase = "go";
      cx.notify();
    });
    cx.timer.after(1500, (cx) => {
      this.packets = this.packets.filter((p) => p.batch !== batch);
      cx.notify();
    });
    return true;
  }

  // ---- input

  drill(view, cx) {
    this.view = view;
    this.pinned = null;
    this.hover = null;
    this.autoFit = true;
    this.rebuild(cx);
  }

  focusNode(id, cx) {
    const n = this.vm?.byId.get(id) ?? buildModel(this.doc, "global").byId.get(id);
    if (n?.shoal && this.view !== n.shoal) this.drill(n.shoal, cx);
    this.pinned = id;
  }

  command(c, cx) {
    if (c === "back") this.key({ keystroke: "escape" }, cx);
    else if (c === "legend") this.legend = !this.legend;
    else if (c === "motion") this.motionReq = MOTION_CYCLE[(MOTION_CYCLE.indexOf(this.motionReq) + 1) % MOTION_CYCLE.length];
    else if (c === "fit") { this.autoFit = true; this.fit(); }
  }

  key(e, cx) {
    const k = e.keystroke;
    const center = { x: this.canvasSize().width / 2, y: this.canvasSize().height / 2 };
    if (k === "escape") {
      if (this.pinned) this.pinned = null;
      else if (this.legend) this.legend = false;
      else if (this.vm && !this.vm.global) this.drill("global", cx);
    } else if (k === "l") this.legend = !this.legend;
    else if (k === "m") this.motionReq = MOTION_CYCLE[(MOTION_CYCLE.indexOf(this.motionReq) + 1) % MOTION_CYCLE.length];
    else if (k === "f" || k === "0") { this.autoFit = true; this.fit(); }
    else if (k === "i") { this.items = !this.items; this.rebuild(cx); }
    else if (k === "=" || k === "+" || k === "shift-=") { this.tf = zoomAt(this.tf, center, 1.2); this.autoFit = false; }
    else if (k === "-") { this.tf = zoomAt(this.tf, center, 1 / 1.2); this.autoFit = false; }
    else if (k === "left" || k === "right" || k === "up" || k === "down") {
      const d = 48;
      this.tf = panBy(this.tf, k === "left" ? d : k === "right" ? -d : 0, k === "up" ? d : k === "down" ? -d : 0);
      this.autoFit = false;
    } else return;
    cx.notify();
  }

  move(e, cx) {
    const p = e.local_position;
    if (this.press) {
      const dx = e.position.x - this.press.x, dy = e.position.y - this.press.y;
      if (!this.drag && Math.abs(dx) + Math.abs(dy) > 4) this.drag = true;
      if (this.drag) {
        this.tf = panBy(this.press.tf, dx, dy);
        this.autoFit = false;
        cx.notify();
        return;
      }
    }
    if (!p) return;
    const id = hitNode(this.hits, p, 4);
    if (id !== this.hover) {
      this.hover = id;
      cx.notify();
    }
  }

  release(e, cx) {
    const press = this.press;
    this.press = null;
    if (!press) return;
    if (this.drag) {
      this.drag = false;
      return;
    }
    const p = e.local_position ?? { x: press.lx, y: press.ly };
    const id = hitNode(this.hits, p, 4);
    if (id) this.pinned = this.pinned === id ? null : id;
    else if (this.vm?.global && hitDisc(this.discs, p)) this.drill(hitDisc(this.discs, p), cx);
    else this.pinned = null;
    cx.notify();
  }

  wheel(e, cx) {
    if (!this.tf) return;
    const anchor = e.local_position ?? { x: this.canvasSize().width / 2, y: this.canvasSize().height / 2 };
    this.tf = zoomAt(this.tf, anchor, wheelFactor(e.delta?.y ?? 0), 0.25, 4);
    this.autoFit = false;
    cx.notify();
  }

  // A pulse row click asks the full view (any copy) to focus a shoal and role.
  applySharedFocus() {
    const f = shared(FOCUS_KEY);
    if (!f || typeof f !== "object" || f.t === this.focusSeen) return;
    this.focusSeen = f.t;
    if (!this.doc || typeof f.t !== "number" || Date.now() - f.t > 120000) return;
    if (f.shoal) this.drill(f.shoal, null);
    if (f.node) this.pinned = f.node;
  }

  // ---- render

  render(_cx) {
    this.resolveMotion();
    if (this.mode === "pulse") return this.renderPulse();
    this.applySharedFocus();
    const size = this.canvasSize();
    const root = h_flex().id("root").size_full().bg(P.bgBottom).font_family(MONO).text_color(P.ink)
      .track_focus(this.handle)
      .on_key_down((e, cx) => this.key(e, cx))
      .on_size_change((s, cx) => {
        if (Math.abs(s.width - this.size.width) < 0.5 && Math.abs(s.height - this.size.height) < 0.5) return;
        this.size = s;
        if (this.autoFit) this.fit();
        cx.notify();
      });
    root.child(this.renderCanvas(size));
    if (this.size.width >= 640) root.child(this.renderRail());
    return root;
  }

  renderCanvas(size) {
    const canvas = div().id("canvas").relative().flex_1().min_w(0).h_full().overflow_hidden().bg(P.bgBottom)
      .on_mouse_down("left", (e, _cx) => {
        this.handle.focus();
        const lp = e.local_position ?? { x: 0, y: 0 };
        this.press = { x: e.position.x, y: e.position.y, lx: lp.x, ly: lp.y, tf: { ...this.tf } };
        this.drag = false;
      })
      .on_mouse_move((e, cx) => this.move(e, cx))
      .on_mouse_up("left", (e, cx) => this.release(e, cx))
      .on_hover((h, cx) => {
        if (h) return;
        const had = this.hover !== null || this.press !== null;
        this.press = null;
        this.drag = false;
        this.hover = null;
        if (had) cx.notify();
      })
      .on_scroll_wheel((e, cx) => this.wheel(e, cx));
    if (!this.vm || !this.lay || !this.tf) {
      return canvas.child(div().absolute().left(24).top(24).text_size(12).text_color(this.error ? P.red : P.dim)
        .child(this.error ? `feed error: ${this.error}` : "loading the shoal graph..."));
    }
    const sc = buildScene(this.vm, this.lay, {
      width: size.width, height: size.height, tf: this.tf, hover: this.hover, selected: this.pinned,
      policy: this.policy, packets: this.packets,
    });
    this.hits = sc.hits;
    this.discs = sc.discs;
    for (const it of sc.items) {
      const e = el(it);
      if (e) canvas.child(e);
    }
    canvas.child(this.renderHeader());
    if (this.legend) canvas.child(this.renderLegend(size));
    const cardId = this.pinned ?? this.hover;
    if (cardId && sc.nodeScreen.has(cardId)) canvas.child(this.renderCard(cardId, sc.nodeScreen.get(cardId), size, cardId === this.pinned));
    if (this.error) {
      canvas.child(div().absolute().left(16).bottom(12).px(8).py(4).rounded(6).bg(withAlpha(P.red, 0.16))
        .border(1).border_color(withAlpha(P.red, 0.5)).text_size(10.5).text_color(P.red).child(`feed error (showing last good): ${this.error}`));
    }
    return canvas;
  }

  renderHeader() {
    const h = headerText(this.vm);
    return v_flex().absolute().left(16).top(12).gap(2)
      .child(h_flex().gap(8).items_center()
        .child(div().text_size(11).font_semibold().text_color(P.ink).child("LATTICE"))
        .child(div().text_size(11).text_color(P.steel2).child(`› ${h.where}`)))
      .child(div().text_size(9.5).text_color(P.dim).child(`${h.counts}${this.vm.global ? " · click a cluster to drill in" : " · esc: back"} · l: legend · motion ${this.tier.tier}${this.tier.auto ? " (auto)" : ""}`));
  }

  renderCard(id, at, size, pinned) {
    const c = cardLines(this.vm, id);
    if (!c) return div();
    const W = 288;
    const lines = 2 + c.rows.length + c.list.length + c.links.length + (c.command ? 1 : 0);
    const H = 24 + lines * 16;
    let x = at.x + at.r + 16;
    if (x + W > size.width - 8) x = at.x - at.r - 16 - W;
    const y = clamp(at.y - 20, 8, Math.max(8, size.height - H - 8));
    const color = c.color ?? P.ink;
    const card = v_flex().id("card").absolute().left(clamp(x, 8, size.width - W - 8)).top(y).w(W).p(10).gap(3)
      .rounded(8).bg(withAlpha(P.panelSolid, 0.95)).border(1).border_color(withAlpha(color, pinned ? 0.7 : 0.35)).text_size(10.5)
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .child(h_flex().gap(6).items_center()
        .child(dot(color, 9))
        .child(div().text_size(12).font_semibold().text_color(P.ink).child(c.title))
        .child(div().flex_1().text_color(P.dim).child(c.subtitle ?? ""))
        .when(pinned, (el) => el.child(div().text_color(P.faint).child("pinned"))));
    for (const [k, v] of c.rows) {
      card.child(h_flex().gap(8).child(div().w(104).flex_none().text_color(P.dim).child(k)).child(div().flex_1().truncate().text_color(P.ink).child(v)));
    }
    for (const line of c.list) card.child(div().truncate().text_color(P.magenta).child(line));
    c.links.forEach((l, i) => {
      const color2 = l.stale ? P.red : l.kind === "active" ? P.teal : l.kind === "next" ? P.ink : P.dim;
      if (pinned && l.url) card.child(Link.new(`card-link-${i}`).href(l.url).text_color(color2).child(l.text));
      else card.child(div().truncate().text_color(color2).child(l.text));
    });
    if (c.command) {
      card.child(div().id("card-cmd").mt(2).px(6).py(2).rounded(4).bg(withAlpha(P.steel1, 0.35)).text_color(P.steel2)
        .when(pinned, (el) => el.cursor_pointer().on_click((_e, cx) => cx.write_to_clipboard(c.command)).tooltip("copy"))
        .child(`$ ${c.command}`));
    }
    return card;
  }

  renderLegend(size) {
    const L = legendEntries(this.vm);
    const glyph = { hatch: "▨", dashed: "◌", dotted: "∴", outline: "○", fill: "●" };
    const col = v_flex().gap(1);
    for (const s of L.states) {
      col.child(h_flex().gap(6).items_center()
        .child(div().w(12).text_color(s.color).child(glyph[s.style] ?? "●"))
        .child(div().w(12).text_color(s.color).child(s.badge ?? ""))
        .child(div().text_color(s.needsYou ? P.ink : P.dim).child(`${s.label}${s.pulse && this.policy.pulseMode ? " (pulses)" : ""}`)));
    }
    const right = v_flex().gap(1);
    right.child(div().text_color(P.steel2).font_semibold().child("SHAPES"));
    for (const [shape, label] of L.shapes) right.child(div().text_color(P.dim).child(`${{ circle: "●", hexagon: "⬢", diamond: "◆", square: "■", triangle: "▲", ring: "◯", dashed: "◌" }[shape]} ${label}`));
    right.child(div().mt(4).text_color(P.steel2).font_semibold().child("EDGES"));
    for (const e of L.edges) right.child(h_flex().gap(6).child(div().text_color(e.color).child(e.dash ? "┄┄" : "━━")).child(div().text_color(P.dim).child(e.cls)));
    right.child(div().mt(4).text_color(P.steel2).font_semibold().child("HOSTS"));
    for (const h of L.hosts) right.child(h_flex().gap(6).items_center().child(dot(h.color, 7)).child(div().text_color(P.dim).child(h.name)));
    for (const [label, color, g] of L.extra) right.child(h_flex().gap(6).child(div().text_color(color).child(g)).child(div().text_color(P.dim).child(label)));
    return v_flex().id("legend").absolute().left(16).bottom(16).p(10).gap(4).rounded(8).bg(withAlpha(P.panelSolid, 0.94)).border(1)
      .border_color(withAlpha(P.steel1, 0.6)).text_size(9.5).max_h(size.height - 80).overflow_hidden()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .child(div().text_color(P.steel2).font_semibold().child("LEGEND (l to close)"))
      .child(div().text_color(P.dim).child(`motion: ${this.tier.tier}${this.tier.auto ? ` (auto: ${this.tier.why})` : ""} \u00b7 m cycles`))
      .child(h_flex().gap(14).items_start().child(v_flex().gap(1).child(div().text_color(P.steel2).font_semibold().child("STATES")).child(col)).child(right));
  }

  renderRail() {
    const t = theme();
    const vm = this.vm;
    const rail = v_flex().id("rail").w(RAIL_W).flex_none().h_full().bg(P.panelSolid).border_l(1)
      .border_color(t.border ?? P.steel0).p(12).gap(12).overflow_y_scrollbar().text_size(10.5);
    const section = (title) => div().text_size(9.5).font_semibold().text_color(P.steel2).child(title);
    rail.child(v_flex().gap(2)
      .child(div().text_size(12).font_semibold().text_color(P.ink).child(vm ? (vm.global ? "all shoals" : shortShoal(vm.view)) : "shoal lattice"))
      .child(div().text_color(this.error ? P.red : P.dim).child(this.error ? "feed error" : this.doc ? `${this.doc.host ?? ""} · ${this.doc.ts ?? ""}` : "loading")));
    if (!vm) return rail;

    rail.child(section(`NEEDS YOU  ${vm.needsYou.length}`));
    const ny = v_flex().gap(2);
    for (const r of vm.needsYou.slice(0, 12)) {
      ny.child(h_flex().id(`ny-${r.id}`).gap(7).items_center().px(6).py(3).rounded(5).cursor_pointer()
        .bg(withAlpha(r.color, r.rank === 0 ? 0.16 : 0.08)).hover((s) => s.bg(withAlpha(r.color, 0.24)))
        .on_click((_e, cx) => { this.focusNode(r.id, cx); cx.notify(); })
        .child(div().w(16).h(16).flex_none().rounded(8).bg(r.color).flex().items_center().justify_center()
          .text_size(9).font_bold().text_color("#0b0b10").child(r.badge ?? "!"))
        .child(v_flex().flex_1().min_w(0)
          .child(h_flex().gap(6).child(div().font_semibold().truncate().child(r.label)).child(div().text_color(P.dim).child(vm.global && r.shoal ? shortShoal(r.shoal) : "")))
          .child(div().truncate().text_size(9.5).text_color(P.dim).child(`${r.text} · ${fmtAge(r.since_s)}`))));
    }
    if (!vm.needsYou.length) ny.child(div().text_color(P.dim).child("nothing waits on you"));
    rail.child(ny);

    rail.child(section("SHOALS"));
    for (const s of vm.shoalsInView) {
      const share = s.workShare;
      rail.child(v_flex().id(`sh-${s.id}`).gap(3).p(8).rounded(6).bg(withAlpha(s.tint, 0.06)).border(1).border_color(withAlpha(s.tint, 0.22))
        .when(vm.global, (el) => el.cursor_pointer().hover((st) => st.bg(withAlpha(s.tint, 0.12))).on_click((_e, cx) => { this.drill(s.id, cx); cx.notify(); }))
        .child(h_flex().gap(7).items_center()
          .child(this.pulseDot(s.urgentColor, 8, s.needsYou > 0))
          .child(div().font_semibold().text_color(s.tint).child(s.short))
          .child(div().flex_1().text_color(P.dim).child(s.gen))
          .when(s.needsYou > 0, (el) => el.child(div().px(5).rounded(7).bg(P.magenta).text_color("#0b0b10").font_bold().text_size(9.5).child(String(s.needsYou)))))
        .child(h_flex().gap(10).text_color(P.dim)
          .child(div().child(`done ${s.done24}/24h ${s.done7}/7d`))
          .child(div().child(`${s.msgsPerH} msg/h`)))
        .child(h_flex().gap(8).items_center()
          .when(share != null, (el) => el
            .child(div().w(80).h(4).rounded(2).bg(withAlpha(P.steel1, 0.6)).child(div().h(4).rounded(2).bg(P.teal).w(Math.round(80 * clamp(share, 0, 1)))))
            .child(div().text_color(P.dim).child(`work ${Math.round(share * 100)}%`)))
          .when(share == null, (el) => el.child(div().text_color(P.faint).child("work share n/a")))
          .child(div().flex_1())
          .child(sparkline(this.history.get(s.id) ?? [], 56, 14, withAlpha(s.tint, 0.9))))
        .when(s.grants > 0 || s.pages24 > 0 || s.asks > 0, (el) => el.child(div().text_color(P.dim)
          .child(`asks ${s.asks} · grants ${s.grants} · pages ${s.pages24}/24h`))));
    }

    rail.child(section("HOSTS"));
    for (const h of hostSummaries(vm)) {
      rail.child(v_flex().gap(1).px(6)
        .child(h_flex().gap(7).items_center().child(dot(h.color, 8)).child(div().font_semibold().text_color(h.color).child(`${h.name}${h.home ? " ⌂" : ""}`))
          .child(div().flex_1()).child(div().text_color(P.dim).child(`${h.roles} roles`)))
        .child(div().pl(15).text_color(P.dim).child(`work ${h.working} · wait ${h.waiting}${h.needsYou ? ` · you ${h.needsYou}` : ""} · ${h.msgsPerH}/h${h.jobsIn ? ` · CI in ${h.jobsIn}` : ""}`)));
    }
    rail.child(div().text_size(9).text_color(P.faint).child("drag: pan · wheel: zoom · click: pin · esc: back · f: fit · l: legend"));
    return rail;
  }

  renderPulse() {
    const pl = placement();
    const right = pl?.edge === "right";
    const doc = this.doc;
    const col = v_flex().id("pulse").py(4).gap(1).font_family(MONO).text_size(10.5).text_color(P.ink);
    if (!doc) return col.child(div().px(10).text_color(this.error ? P.red : P.dim).child(this.error ? "feed error" : "…"));
    const rows = shoalSummaries(doc);
    for (const s of rows) {
      let row = h_flex().id(`pulse-${s.id}`).gap(8).px(10).py(3).items_center().cursor_pointer()
        .hover((st) => st.bg(withAlpha(s.tint, 0.12)))
        .on_click((_e, cx) => {
          const first = buildModel(doc, s.id).needsYou[0];
          setShared(FOCUS_KEY, { shoal: s.id, node: first?.id ?? null, t: Date.now() });
          cx.notify();
        })
        .child(this.pulseDot(s.urgentColor, 9, s.needsYou > 0))
        .child(div().w(18).text_color(s.needsYou ? P.magenta : P.faint).font_bold().child(s.needsYou ? String(s.needsYou) : "·"))
        .child(div().w(70).truncate().text_color(s.tint).child(s.short))
        .child(div().w(52).text_color(P.dim).child(`${s.msgsPerH}/h`))
        .child(sparkline(this.history.get(s.id) ?? [], 40, 12, withAlpha(s.tint, 0.9)));
      if (right) row = row.flex_row_reverse();
      col.child(row);
    }
    return col;
  }
}

export { STATE_STYLE };
