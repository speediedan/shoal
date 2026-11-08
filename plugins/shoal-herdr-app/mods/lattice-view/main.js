// lattice-view: the shoal graph doc v1 as an animated, hoverable network (Herdr app mod).
//
// A thin View. All decisions live in pure modules that also run under Node: model.js (doc to
// view model and encodings), layout.js (deterministic layout), geom.js (curves, hit tests,
// pan/zoom) and scene.js (the display list). This file fetches the doc on a data timer, turns the
// display list into GPUI elements and handles input. Motion is native (.loop, .frames,
// .transition, .spin); the view redraws only on data ticks, hover target changes, pan and zoom,
// and at most every 100 ms while a changed layout cools. A right-click on a role node opens the
// shoal-actions dialog on that role (model.js actionsOverlay); the view itself never writes.
//
// props:
//   source: {kind: "exec", argv: [...], on} | {kind: "file", path, on} | {kind: "fixture", path?}
//   mode: "full" (default) | "pulse" (a compact floating edge drawer: one row per shoal, then up
//         to five needs-you roles; a role row click sets shared "lattice.focus", which a full copy
//         honors by drilling to that role's shoal and pinning its card)
//   shoal: a shoal id: the copy opens drilled into that shoal and treats it as its home view
//          (Escape returns there, not to the global view; `g` or the header chip toggles global).
//          Meant for a per-shoal lattice tab that reads the one shared feed file. In pulse mode it
//          narrows the drawer to that shoal.
//   view: a shoal id to open drilled into once (Escape then goes to the home view)
//   pin: a node id whose card opens pinned
//   replay: true starts the 24 h replay once the first doc is in (as `r` does)
//   depth: "flat" (default), "tilt" or "iso": the depth camera (`d` cycles it, `,` and `.` turn it)
//   motion: "auto" (default) | "full" | "balanced" | "low" | "off" (motion.js; `m` cycles)
//   budget: {max_dots, max_pulses, frame_ms, packets} overrides for the tier
//   renderer: "cpu" | "gpu" (a launcher hint for auto; else read from the app process)
//   items: false hides the satellite layer
//   poll_ms: data poll interval (default 4000, 1500 to 60000), for every source kind; with
//            `{kind: "file"}` each poll is one readTextFile of the shared feed file, no process

import { View, div, svg, PathBuilder, Background } from "gpui-kit";
import { h_flex, v_flex, Link } from "gpui-base";
import { exec, readTextFile, appHome, props, theme, shared, setShared, nextCommand, placement, openOverlay, window as appWindow } from "herdr";
import {
  PALETTE, STATE_STYLE, EDGE_COLOR, buildModel, eventsSince, demoEvents, shoalSummaries, hostSummaries,
  cardLines, legendEntries, shortShoal, fmtAge, visualSignature, urgentSignature,
  FOCUS_KEY, homeView, escapeStep, readFocus, focusKeyOf, pulseModel, pulseSignature, actionsOverlay, scopeOverlay, removeOverlay,
  SHOAL_CARD, BOARD_CARD, HIDDEN_KEY, itemOf, hiddenList, ledgerStale, boardCells, boardBar, boardSummary, offBoardText,
} from "./model.js";
import { Layout } from "./layout.js";
import { motionPolicy, resolveTier, rendererFromProcess, shouldRender, MOTION_CYCLE } from "./motion.js";
import { buildScene, fitScene, fitText, headerText } from "./scene.js";
import { hitNode, hitEdge, hitDisc, hitHole, hitTitle, panBy, zoomAt, wheelFactor, placeCard, clamp, withAlpha } from "./geom.js";
import { replayModel, replayStep, replayAdvance, replayOfDoc, binStart, binOf, clock, REPLAY_BINS, REPLAY_TICK_MS, REPLAY_SPEEDS, REPLAY_DAY_S } from "./replay.js";
import { camera, projectLayout, nextDepth, depthFor, YAW_STEP } from "./depth.js";

const MOD = "lattice-view";
const RAIL_W = 272;
const BOARD_BAR_W = 226;
const TIER_MS = 2000;
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
    this.homeShoal = typeof p.shoal === "string" && p.shoal && p.shoal !== "global" ? p.shoal : null;
    this.home = { home: this.homeShoal ?? "global", missing: false };
    this.view = typeof p.view === "string" ? p.view : this.homeShoal ?? "global";
    this.pm = null;
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
    this.legendAll = false;
    this.history = new Map();
    this.replay = null; // {rm, t, playing, timer, note}: the last day played back as packets (r)
    this.hits = [];
    this.satHits = [];
    this.edgeHits = [];
    this.discs = [];
    this.cooling = null;
    this.batch = 0;
    // a focus request persisted from an earlier run is not replayed
    try { this.focusSeen = focusKeyOf(shared(FOCUS_KEY)); } catch (_e) { this.focusSeen = null; }
    this.pinned = typeof p.pin === "string" ? p.pin : null;
    this.replayAtStart = p.replay === true;
    this.depth = ["tilt", "iso"].includes(p.depth) ? p.depth : "flat";
    this.yawTurn = 0;
    this.handle = cx.focus_handle();
    if (!this.renderer) this.probeRenderer(cx);
    this.load(cx);
    cx.timer.every(this.pollMs, (cx) => this.load(cx));
    cx.timer.every(TIER_MS, (cx) => {  // a render decides the motion, and a window gaining focus draws nothing by itself
      const was = this.tier?.tier;
      this.resolveMotion();
      if (this.tier.tier !== was) cx.notify();
    });
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
        // the environment first (a forced or selected driver), then the loaded libraries and
        // device nodes, where a hardware driver outranks the lavapipe ICD the loader always maps
        const env = (await readTextFile("/proc/self/environ")) ?? "";
        let hint = rendererFromProcess(env);
        if (!hint) hint = rendererFromProcess(env, (await readTextFile("/proc/self/maps")) ?? "");
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
        if (doc && this.replayAtStart) {
          this.replayAtStart = false;
          this.startReplay(cx);
        }
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
    this.home = homeView(doc, this.homeShoal);
    if (this.mode === "pulse") {
      // the drawer needs no layout, scene or packets: only its rows, redrawn when they change
      this.pm = pulseModel(doc, { shoal: this.homeShoal, maxNeeds: 5 });
      const sig = pulseSignature(this.pm);
      if (sig === this.sig) return false;
      this.sig = sig;
      return true;
    }
    for (const s of shoalSummaries(doc)) {
      const h = this.history.get(s.id) ?? [];
      h.push(s.msgsPerH);
      if (h.length > 30) h.shift();
      this.history.set(s.id, h);
    }
    this.rebuild(cx);
    const { fresh, lastSeq } = eventsSince(this.lastSeq, doc);
    this.lastSeq = lastSeq;
    const sig = visualSignature(this.vm, this.pinned ?? this.hover);
    const urgentSig = urgentSignature(this.vm);
    const draw = shouldRender({ sig, urgentSig, lastSig: this.sig, lastUrgentSig: this.urgentSig, sinceMs: Date.now() - (this.lastRender ?? 0), renderMs: this.policy.renderMs });
    if (draw) {
      this.sig = sig;
      this.urgentSig = urgentSig;
    }
    // a replay owns the packets: the live ones would read as part of the past
    return this.spawnPackets(this.replay ? [] : fresh, cx) || draw;
  }

  // ---- replay: the last 24 h by `ts`, as packets on the live picture

  /** `r`: fetch the day's events (the feed's `--replay`, on the machine the feed reads) and start playing them. */
  startReplay(cx) {
    if (this.replay) return this.stopReplay();
    this.replay = { rm: null, t: 0, playing: false, timer: null, note: "loading the last 24 h...", dayS: this.replayDayS ?? REPLAY_DAY_S };
    cx.spawn(async (cx) => {
      let doc = null;
      try {
        const s = this.source;
        if (s.kind === "fixture") doc = { replay: replayOfDoc(this.fixture ?? this.doc) };
        else {
          const argv = s.kind === "exec" ? [...s.argv] : ["shoal", "ui", "graph", "--json"];
          const r = await exec(argv[0], [...argv.slice(1), "--replay", "--events", "0"], { on: s.on });
          if (r.code !== 0) throw new Error(`exit ${r.code}: ${(r.stderr || r.stdout || "").trim().slice(-160)}`);
          doc = JSON.parse(r.stdout);
        }
      } catch (e) {
        if (this.replay) this.replay.note = `replay unavailable: ${String(e?.message ?? e).slice(0, 120)}`;
        cx.notify();
        return;
      }
      const rm = replayModel(doc);
      if (!this.replay) return;
      if (!rm) {
        this.replay.note = "the feed gave no replay";
      } else {
        Object.assign(this.replay, { rm, t: rm.from, note: rm.events.length ? null : "nothing happened in the last 24 h" });
        this.playReplay(cx, rm.events.length > 0);
      }
      cx.notify();
    });
  }

  playReplay(cx, on) {
    const rp = this.replay;
    if (!rp?.rm) return;
    rp.timer?.cancel();
    rp.timer = null;
    rp.playing = on;
    if (!on) return;
    if (rp.t >= rp.rm.to) rp.t = rp.rm.from;
    rp.timer = cx.timer.every(REPLAY_TICK_MS, (cx) => {
      const r = this.replay;
      if (!r?.rm) return;
      const t1 = Math.min(r.rm.to, r.t + replayAdvance(r.rm, r.dayS));
      const step = replayStep(r.rm, r.t, t1);
      r.t = t1;
      if (step.events.length) this.spawnPackets(step.events, cx, true);
      if (r.t >= r.rm.to) this.playReplay(cx, false);
      cx.notify();
    });
  }

  seekReplay(cx, i) {
    if (!this.replay?.rm) return;
    this.replay.t = binStart(this.replay.rm, i);
    if (!this.replay.playing) this.playReplay(cx, true);
  }

  /** A replay's speed, kept for the next one too. */
  replaySpeed(dayS) {
    this.replayDayS = dayS;
    if (this.replay) this.replay.dayS = dayS;
  }

  stepReplaySpeed(d) {
    const cur = this.replay?.dayS ?? this.replayDayS ?? REPLAY_DAY_S;
    const i = REPLAY_SPEEDS.findIndex((s) => Math.abs(s.day_s - cur) < 0.01);
    const j = Math.max(0, Math.min(REPLAY_SPEEDS.length - 1, (i < 0 ? 1 : i) + d));
    this.replaySpeed(REPLAY_SPEEDS[j].day_s);
  }

  stopReplay() {
    this.replay?.timer?.cancel();
    this.replay = null;
    this.packets = [];
  }

  rebuild(cx) {
    if (!this.doc) return;
    this.vm = buildModel(this.doc, this.view, { items: this.items, hidden: hiddenList(shared(HIDDEN_KEY)) });
    this.view = this.vm.view;
    let lay = this.layouts.get(this.view);
    if (!lay) {
      lay = new Layout();
      this.layouts.set(this.view, lay);
    }
    const cold = lay.pos.size === 0;
    lay.update(this.vm);
    // positions are kept by node id: a poll with the same nodes moves nothing (and so does not
    // refit); a changed node set glides its new and retargeted nodes into place at 10 Hz
    if (!cold && lay.iters > 0 && cx && this.policy.any) this.cool(cx);
    else lay.run();
    this.lay = this.seen(lay.result());
    if (!this.tf || this.needFit || (this.autoFit && lay.moved)) this.fit();
  }

  /** The layout as the depth camera sees it (flat: as it is). Low and off motion on a CPU renderer stay flat. */
  seen(lay) {
    this.depthNow = depthFor(this.depth, this.tier?.tier, this.renderer);
    return projectLayout(this.vm, lay, camera(this.depthNow, this.yawTurn));
  }

  setDepth(name, cx) {
    this.depth = name;
    if (name === "flat") this.yawTurn = 0;
    const lay = this.layouts.get(this.view);
    if (lay) this.lay = this.seen(lay.result());
    this.autoFit = true;
    this.fit();
  }

  /** `f`: a fresh full layout of this view, fitted to the canvas. */
  relayout(cx) {
    this.autoFit = true;
    const lay = this.layouts.get(this.view);
    if (lay && this.vm) {
      this.cooling?.cancel();
      this.cooling = null;
      lay.update(this.vm, { relayout: true });
      lay.run();
      this.lay = this.seen(lay.result());
    }
    this.fit();
  }

  cool(cx) {
    if (this.cooling) return;
    this.cooling = cx.timer.every(100, (cx) => {
      const lay = this.layouts.get(this.view);
      if (lay) {
        lay.step(10);
        this.lay = this.seen(lay.result());
        if (this.autoFit) this.fit(!lay.settled);
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

  /** Fits every node, label and cluster ring inside the canvas (quick: fewer passes, while cooling). */
  fit(quick = false) {
    if (!this.lay || !this.vm) return;
    this.needFit = false;
    this.tf = fitScene(this.vm, this.lay, this.canvasSize(), { pad: 24, top: 48, kMax: 1.6, iterations: quick ? 2 : 5 });
  }

  spawnPackets(fresh, cx, force = false) {
    // a replay asked for is drawn whatever the motion tier: it is the point of the view while it runs
    if ((!this.policy.packets && !force) || !fresh.length || !this.vm) return false;
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
    this.needFit = true;
    this.rebuild(cx);
  }

  focusNode(id, cx) {
    const n = this.vm?.byId.get(id) ?? buildModel(this.doc, "global").byId.get(id);
    if (n?.shoal && this.view !== n.shoal) this.drill(n.shoal, cx);
    this.pinned = id;
  }

  command(c, cx) {
    if (c === "back") this.key({ keystroke: "escape" }, cx);
    else if (c === "global") this.toggleGlobal(cx);
    else if (c === "legend") this.legend = !this.legend;
    else if (c === "motion") this.motionReq = MOTION_CYCLE[(MOTION_CYCLE.indexOf(this.motionReq) + 1) % MOTION_CYCLE.length];
    else if (c === "fit") this.relayout(cx);
  }

  key(e, cx) {
    const k = e.keystroke;
    const center = { x: this.canvasSize().width / 2, y: this.canvasSize().height / 2 };
    if (k === "escape" && this.replay) this.stopReplay();
    else if (k === "escape") {
      const step = escapeStep({ pinned: this.pinned, legend: this.legend, view: this.vm?.view ?? this.view, home: this.home.home });
      if (!step) return;
      if ("pinned" in step) this.pinned = null;
      else if ("legend" in step) this.legend = false;
      else this.drill(step.view, cx);
    } else if (k === "g") this.toggleGlobal(cx);
    else if (k === "l") this.legend = !this.legend;
    else if (k === "m") this.motionReq = MOTION_CYCLE[(MOTION_CYCLE.indexOf(this.motionReq) + 1) % MOTION_CYCLE.length];
    else if (k === "f" || k === "0") this.relayout(cx);
    else if (k === "i") { this.items = !this.items; this.needFit = this.autoFit; this.rebuild(cx); }
    else if (k === "r") this.startReplay(cx);
    else if (k === "d") this.setDepth(nextDepth(this.depth), cx);
    else if ((k === "," || k === ".") && this.depth !== "flat") { this.yawTurn += k === "," ? -YAW_STEP : YAW_STEP; this.setDepth(this.depth, cx); }
    else if (k === "space" && this.replay?.rm) this.playReplay(cx, !this.replay.playing);
    else if ((k === "[" || k === "]") && this.replay) this.stepReplaySpeed(k === "]" ? 1 : -1);
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
    const hole = hitHole(this.discs, p);
    const named = this.boardUrl(hitTitle(this.discs, p)) ? hitTitle(this.discs, p) : null;
    // the pointer on the card shown keeps it: its links (each queued or done item) can then be clicked
    const b = this.cardBox;
    if (b && this.hover && p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h) return;
    const id = hitNode(this.hits, p, 4) ?? hitNode(this.satHits, p, 5) ?? (named ? BOARD_CARD + named : null) ?? hitEdge(this.edgeHits, p)
      ?? (hole ? SHOAL_CARD + hole : null);
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
    const sat = id ? null : hitNode(this.satHits, p, 5);
    const board = id || sat ? null : this.boardUrl(hitTitle(this.discs, p));
    if (id) this.pinned = this.pinned === id ? null : id;
    else if (sat) {
      // an item opens its issue; one with no link pins its card
      const url = itemOf(this.vm, sat)?.it.url;
      if (url) cx.open_url(url);
      else this.pinned = this.pinned === sat ? null : sat;
    }
    else if (board) cx.open_url(board); // a shoal's name opens its board; the rest of its disc drills in
    else if (this.vm?.global && hitDisc(this.discs, p)) this.drill(hitDisc(this.discs, p), cx);
    else {
      // an edge pins only outside a cluster disc: a click inside one still drills in, however close an edge runs
      const hole = hitHole(this.discs, p);
      const what = hitEdge(this.edgeHits, p) ?? (hole ? SHOAL_CARD + hole : null);
      this.pinned = what && this.pinned !== what ? what : null;
    }
    cx.notify();
  }

  /** A shoal's first priority board, or null. */
  boardUrl(sid) {
    return sid ? this.vm?.shoals.find((s) => s.id === sid)?.boards.items[0]?.url ?? null : null;
  }

  /** Right-click: a role opens the shoal-actions dialog on it; an outside writer pins its card, which offers to hide
   * it; a shoal's circle or title acts on that shoal, and anywhere else (the hub too) on every shoal. */
  actions(e, cx) {
    const p = e.local_position;
    if (!p || !this.vm) return;
    const id = hitNode(this.hits, p, 4);
    const n = id ? this.vm.byId.get(id) : null;
    cx.stop_propagation();
    this.press = null;
    this.drag = false;
    if (n && n.kind !== "role" && n.kind !== "user") {
      this.pinned = id;
      cx.notify();
      return;
    }
    const spec = n?.kind === "role" ? actionsOverlay(this.vm, id, this.source) : scopeOverlay(this.vm, n ? null : hitDisc(this.discs, p), this.source);
    if (spec) openOverlay(spec);
  }

  /** Take a node off every copy of the picture, and bring them back; the list persists. */
  setHidden(ids, cx) {
    setShared(HIDDEN_KEY, ids);
    this.pinned = null;
    this.hover = null;
    this.rebuild(cx);
    cx.notify();
  }

  wheel(e, cx) {
    if (!this.tf) return;
    const anchor = e.local_position ?? { x: this.canvasSize().width / 2, y: this.canvasSize().height / 2 };
    this.tf = zoomAt(this.tf, anchor, wheelFactor(e.delta?.y ?? 0), 0.25, 4);
    this.autoFit = false;
    cx.notify();
  }

  /** `g` and the header chip: the global view, or back to this copy's home shoal. */
  toggleGlobal(cx) {
    if (!this.vm) return;
    const home = this.home.home;
    if (this.vm.global) { if (home !== "global") this.drill(home, cx); } else this.drill("global", cx);
  }

  // A pulse row click (or any mod) asks the full view to focus a role: shared "lattice.focus".
  applySharedFocus() {
    if (!this.doc) return;
    const f = readFocus(shared(FOCUS_KEY), this.focusSeen);
    if (!f) return;
    this.focusSeen = f.key;
    if (f.stale) return;
    if (f.node && this.vm && (this.vm.byId.has(f.node) || buildModel(this.doc, "global").byId.has(f.node))) this.focusNode(f.node, null);
    else if (f.shoal) this.drill(f.shoal, null);
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
      .on_mouse_down("right", (e, cx) => this.actions(e, cx))
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
    this.satHits = sc.satHits ?? [];
    this.edgeHits = sc.edgeHits;
    this.discs = sc.discs;
    for (const it of sc.items) {
      const e = el(it);
      if (e) canvas.child(e);
    }
    canvas.child(this.renderHeader());
    if (this.replay) canvas.child(this.renderReplay(size));
    if (this.legend) canvas.child(this.renderLegend(size));
    const screenOf = (id) => {
      if (!id) return null;
      if (id.startsWith(SHOAL_CARD)) {
        const d = sc.discs.find((x) => x.id === id.slice(SHOAL_CARD.length));
        return d ? { x: d.x, y: d.y, r: 0 } : null;
      }
      if (id.startsWith(BOARD_CARD)) {
        const t = sc.discs.find((x) => x.id === id.slice(BOARD_CARD.length))?.title;
        return t ? { x: t.x + t.w / 2, y: t.y + t.h, r: 0 } : null;
      }
      return sc.nodeScreen.get(id) ?? sc.satScreen?.get(id) ?? sc.edgeScreen.get(id) ?? null;
    };
    // an edge with nothing left in the day leaves the feed: a pin on it must not block the hover's card
    const cardId = screenOf(this.pinned) ? this.pinned : this.hover;
    const cardAt = screenOf(cardId);
    this.cardBox = null;
    if (cardAt) canvas.child(this.renderCard(cardId, cardAt, size, cardId === this.pinned));
    if (this.error) {
      canvas.child(div().absolute().left(16).bottom(12).px(8).py(4).rounded(6).bg(withAlpha(P.red, 0.16))
        .border(1).border_color(withAlpha(P.red, 0.5)).text_size(10.5).text_color(P.red).child(`feed error (showing last good): ${this.error}`));
    }
    return canvas;
  }

  /** The replay bar along the canvas's foot: a quarter hour a column, its height the events in it; a click plays from there. */
  renderReplay(size) {
    const rp = this.replay;
    const W = Math.max(240, size.width - 32);
    const bar = v_flex().id("replay").absolute().left(16).bottom(12).w(W).gap(4).px(8).py(6).rounded(6)
      .bg(P.panel).border(1).border_color(withAlpha(P.steel1, 0.6)).text_size(10).text_color(P.dim)
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation());
    const btn = (id, text, tip, f) => div().id(id).px(6).rounded(4).cursor_pointer().text_color(P.ink).border(1)
      .border_color(withAlpha(P.steel1, 0.7)).hover((st) => st.bg(withAlpha(P.steel1, 0.4))).tooltip(tip)
      .on_click((_e, cx) => { f(cx); cx.notify(); }).child(text);
    const rm = rp.rm;
    const head = h_flex().gap(8).items_center()
      .child(div().font_semibold().text_color(P.ink).child("REPLAY"))
      .child(div().text_color(P.steel2).child(rm ? `${clock(rp.t)} · last 24 h · ${rm.events.length} events${rm.dropped ? ` (+${rm.dropped} older not drawn)` : ""}` : rp.note ?? ""));
    if (rm) head.child(btn("replay-play", rp.playing ? "❚❚" : "▶", "play or pause (space)", (cx) => this.playReplay(cx, !rp.playing)));
    if (rm) {
      // the speed: a whole day in 96, 32 (the default), 11 or 3.2 seconds; [ and ] step it
      for (const [i, sp] of REPLAY_SPEEDS.entries()) {
        const on = Math.abs((rp.dayS ?? REPLAY_DAY_S) - sp.day_s) < 0.01;
        head.child(div().id(`replay-speed-${i}`).px(5).rounded(4).cursor_pointer().text_color(on ? P.void : P.ink).bg(on ? P.teal : withAlpha(P.steel1, 0.25))
          .tooltip(`a day in ${sp.day_s} s`).on_click((_e, cx) => { this.replaySpeed(sp.day_s); cx.notify(); }).child(sp.label));
      }
    }
    head.child(btn("replay-live", "live ✕", "back to live (r or esc)", () => this.stopReplay()));
    if (rm && rp.note) head.child(div().text_color(P.amber).child(rp.note));
    bar.child(head);
    if (rm) {
      const cw = (W - 16) / REPLAY_BINS;
      const now = binOf(rm, rp.t);
      const cols = h_flex().h(26).items_end();
      rm.bins.forEach((n, i) => {
        const h = n ? 3 + Math.round((n / rm.max) * 23) : 1;
        const tint = i === now ? P.teal : i < now ? withAlpha(P.cyan, 0.75) : withAlpha(P.steel2, 0.55);
        cols.child(div().id(`rb-${i}`).w(cw).h(26).flex_none().items_end().cursor_pointer()
          .tooltip(`${clock(binStart(rm, i))}: ${n} event${n === 1 ? "" : "s"}`)
          .on_click((_e, cx) => { this.seekReplay(cx, i); cx.notify(); })
          .child(div().w(Math.max(1, cw - 1)).h(h).mt(26 - h).bg(tint)));
      });
      bar.child(cols);
    }
    return bar;
  }

  renderHeader() {
    const h = headerText(this.vm);
    const home = this.home.home;
    const atHome = this.vm.view === home;
    // a copy with a home shoal gets a chip to the global view and back (also `g`)
    const chip = this.homeShoal && !this.home.missing
      ? div().id("home-chip").px(6).rounded(4).cursor_pointer().text_size(9.5).text_color(P.steel2).border(1).border_color(withAlpha(P.steel1, 0.7))
        .hover((st) => st.text_color(P.ink).bg(withAlpha(P.steel1, 0.4)))
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
        .on_click((_e, cx) => { this.toggleGlobal(cx); cx.notify(); })
        .child(this.vm.global ? `‹ ${shortShoal(home)}` : "all shoals ›")
      : null;
    const board = this.vm.global ? null : this.vm.shoalsInView[0]?.boards.items[0] ?? null;
    const back = this.vm.global ? (home !== "global" ? ` · esc: back to ${shortShoal(home)}` : " · click a cluster to drill in, its name for its board") : atHome ? " · g: all shoals" : " · esc: back";
    return v_flex().absolute().left(16).top(12).gap(2)
      .child(h_flex().gap(8).items_center()
        .child(div().text_size(11).font_semibold().text_color(P.ink).child("LATTICE"))
        .child(div().text_size(11).text_color(P.steel2).child(`› ${h.where}`))
        .when(Boolean(board), (el) => el.child(div().id("header-board").cursor_pointer().text_size(10.5).text_color(P.ink)
          .hover((st) => st.text_color(P.teal))
          .tooltip(board.counts ? boardSummary(board.counts) : "open this shoal's board")
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
          .on_click((_e, cx) => cx.open_url(board.url))
          .child(`${board.title} ↗`)))
        .when(chip !== null, (el) => el.child(chip))
        .when(this.home.missing, (el) => el.child(div().text_size(9.5).text_color(P.amber).child(`${shortShoal(this.homeShoal)} is not in the feed`))))
      .child(div().text_size(9.5).text_color(P.dim).child(`${h.counts}${back} · l: legend · r: replay 24 h · d: depth ${this.depthNow !== this.depth ? `flat (${this.depth} off: CPU, low motion)` : this.depthNow ?? "flat"} · motion ${this.tier.tier}${this.tier.auto ? " (auto)" : ""}`));
  }

  renderCard(id, at, size, pinned) {
    const c = cardLines(this.vm, id);
    if (!c) return div();
    const W = 300;
    // every line is one line (a long item or blocker is cut, its full text on the line's tooltip),
    // so the height estimate holds; a card taller than the canvas drops its last item lines
    const textW = W - 24;
    const fit = (t) => fitText(t, textW, 10.5);
    // measured in Herdr app 0.1.12: a 10.5px line plus the 3px gap is about 20px; padding,
    // border and the command chip add about 28
    const LINE = 20, EXTRA = 28;
    const hideBtn = pinned && c.hideable === true;
    const removeSpec = hideBtn ? removeOverlay(this.vm, id, this.source) : null;
    const boards = c.boards ?? [];
    const longTitle = fitText(c.title, textW - 96, 12) !== c.title; // it wraps onto a second line
    const fixed = 1 + (longTitle ? 1 : 0) + boards.length * 3 + c.rows.length + c.list.length + (c.command ? 1 : 0) + (hideBtn ? 1 : 0) + (removeSpec ? 1 : 0);
    const room = Math.floor((size.height - 16 - EXTRA) / LINE) - fixed;
    const links = c.links.length > room ? c.links.slice(0, Math.max(0, room - 1)) : c.links;
    const more = c.links.length - links.length;
    const box = placeCard(at, size, W, EXTRA + (fixed + links.length + (more ? 1 : 0)) * LINE);
    this.cardBox = box; // where the card is, so a pointer reaching it keeps it
    const color = c.color ?? P.ink;
    const line = (key, text, color2) => {
      const short = fit(text);
      return div().id(key).truncate().text_color(color2).when(short !== text, (el) => el.tooltip(text)).child(short);
    };
    const card = v_flex().id("card").absolute().left(box.x).top(box.y).w(W).max_h(box.maxH).overflow_hidden().p(10).gap(3)
      .rounded(8).bg(withAlpha(P.panelSolid, 0.95)).border(1).border_color(withAlpha(color, pinned ? 0.7 : 0.35)).text_size(10.5)
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .child(h_flex().gap(6).items_center()
        .child(dot(color, 9))
        .child(div().flex_1().min_w(0).text_size(12).font_semibold().text_color(P.ink).child(c.title))
        .when(!longTitle, (el) => el.child(div().flex_none().text_color(P.dim).child(c.subtitle ?? ""))
          .when(pinned, (el2) => el2.child(div().text_color(P.faint).child("pinned")))));
    // a long name has the whole first line, so it reads in full
    if (longTitle) {
      card.child(h_flex().gap(6).child(div().flex_1().min_w(0).truncate().text_color(P.dim).child(c.subtitle ?? ""))
        .when(pinned, (el) => el.child(div().text_color(P.faint).child("pinned"))));
    }
    for (const [k, v] of c.rows) {
      card.child(h_flex().gap(8).child(div().w(104).flex_none().text_color(P.dim).child(k)).child(div().flex_1().min_w(0).truncate().text_color(P.ink).child(v)));
    }
    boards.forEach((b, i) => card.child(this.boardTable(b.counts, `card-bd-${i}`, textW)));
    c.list.forEach((text, i) => card.child(line(`card-ask-${i}`, text, P.magenta)));
    links.forEach((l, i) => {
      const color2 = l.stale ? P.red : l.kind === "active" ? P.teal : l.kind === "next" ? P.ink : P.dim;
      if (l.url) { // a link on a hovered card too: the card stays while the pointer is on it
        const short = fit(l.text);
        card.child(div().id(`card-item-${i}`).when(short !== l.text, (el) => el.tooltip(l.text))
          .child(Link.new(`card-link-${i}`).href(l.url).text_color(color2).child(short)));
      } else card.child(line(`card-item-${i}`, l.text, color2));
    });
    if (more) card.child(div().text_color(P.faint).child(`+${more} more items: shoal view`));
    if (c.command) {
      const cmd = `$ ${c.command}`;
      const short = fitText(cmd, textW - 12, 10.5);
      card.child(div().id("card-cmd").mt(2).px(6).py(2).rounded(4).bg(withAlpha(P.steel1, 0.35)).text_color(P.steel2)
        .when(pinned, (el) => el.cursor_pointer().on_click((_e, cx) => cx.write_to_clipboard(c.command)).tooltip(short === cmd ? "copy" : `copy: ${c.command}`))
        .child(short));
    }
    if (removeSpec) {
      card.child(div().id("card-remove").mt(2).px(6).py(2).rounded(4).cursor_pointer().bg(withAlpha(P.red, 0.18)).text_color(P.ink)
        .hover((st) => st.bg(withAlpha(P.red, 0.32)))
        .tooltip("for good: stops it and removes it from its harness's list, with a preview first; the ledger keeps its history")
        .on_click(() => openOverlay(removeSpec))
        .child("Remove..."));
    }
    if (hideBtn) {
      card.child(div().id("card-hide").mt(2).px(6).py(2).rounded(4).cursor_pointer().bg(withAlpha(P.steel1, 0.35)).text_color(P.ink)
        .hover((st) => st.bg(withAlpha(P.steel1, 0.6)))
        .tooltip(`takes it off this picture only, and the rail's footer brings it back${removeSpec ? "" : ". It has no saved session on this host to remove"}`)
        .on_click((_e, cx) => this.setHidden([...hiddenList(shared(HIDDEN_KEY)), id], cx))
        .child("Hide from the lattice"));
    }
    return card;
  }

  renderLegend(size) {
    const L = legendEntries(this.vm, this.legendAll);
    const glyph = { hatch: "▨", dashed: "◌", dotted: "∴", outline: "○", fill: "●" };
    const col = v_flex().gap(1);
    for (const g of L.groups) {
      col.child(div().mt(3).text_color(P.steel2).font_semibold().child(g.label));
      for (const s of g.states) {
        col.child(h_flex().gap(6).items_center()
          .child(div().w(12).text_color(s.color).child(glyph[s.style] ?? "●"))
          .child(div().w(12).text_color(s.color).child(s.badge ?? ""))
          .child(div().text_color(s.needsYou ? P.ink : P.dim).child(`${s.label}${s.pulse && this.policy.pulseMode ? " (pulses)" : ""}`)));
      }
    }
    if (L.hiddenStates > 0 || this.legendAll) {
      col.child(div().id("legend-all").mt(4).cursor_pointer().text_color(P.dim).hover((st) => st.text_color(P.ink))
        .on_click((_e, cx) => { this.legendAll = !this.legendAll; cx.notify(); })
        .child(this.legendAll ? "only what is on screen" : `${L.hiddenStates} more: the full key`));
    }
    const right = v_flex().gap(1);
    right.child(div().text_color(P.steel2).font_semibold().child("NODES"));
    for (const [shape, label] of L.shapes) right.child(div().text_color(P.dim).child(`${{ circle: "●", hexagon: "⬢", diamond: "◆", square: "■", triangle: "▲", ring: "◯", dashed: "◌" }[shape]} ${label}`));
    right.child(div().mt(4).text_color(P.steel2).font_semibold().child("EDGES"));
    for (const e of L.edges) right.child(h_flex().gap(6).child(div().text_color(e.color).child(e.dash ? "┄┄" : "━━")).child(div().text_color(P.dim).child(e.cls)));
    if (L.hosts.length) right.child(div().mt(4).text_color(P.steel2).font_semibold().child("HOSTS"));
    for (const h of L.hosts) right.child(h_flex().gap(6).items_center().child(dot(h.color, 7)).child(div().text_color(P.dim).child(h.name)));
    for (const [label, color, g] of L.extra) right.child(h_flex().gap(6).child(div().text_color(color).child(g)).child(div().text_color(P.dim).child(label)));
    return v_flex().id("legend").absolute().left(16).bottom(16).p(10).gap(4).rounded(8).bg(withAlpha(P.panelSolid, 0.94)).border(1)
      .border_color(withAlpha(P.steel1, 0.6)).text_size(9.5).max_h(size.height - 80).overflow_hidden()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .child(div().text_color(P.steel2).font_semibold().child("LEGEND (l to close)"))
      .child(div().text_color(P.dim).child(`motion: ${this.tier.tier}${this.tier.auto ? ` (auto: ${this.tier.why})` : ""} \u00b7 m cycles`))
      // the one exception to the tier: a replay asked for with `r` draws its packets at any tier, `off` included
      .child(div().text_color(P.dim).child("r: replay 24 h (its packets move at any motion tier)"))
      .child(div().text_color(P.dim).child(`d: depth (flat, tilt, iso; now ${this.depthNow ?? "flat"}) \u00b7 , and .: turn it`))
      .child(h_flex().gap(14).items_start().child(col).child(right));
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
    const stale = vm.shoalsInView.filter(ledgerStale);
    if (stale.length) {
      const worst = Math.max(...stale.map((s) => s.ledgerAge));
      rail.child(div().px(6).py(4).rounded(5).bg(withAlpha(P.amber, 0.14)).text_color(P.amber)
        .child(`${vm.host ?? "this host"}'s ledger clone is ${fmtAge(worst)} old: this picture may be out of date`));
    }

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

    const boarded = vm.shoalsInView.filter((s) => s.boards.items.length);
    if (boarded.length) rail.child(section("BOARDS"));
    for (const s of boarded) rail.child(this.renderBoards(s, vm.global));

    rail.child(section("SHOALS"));
    for (const s of vm.shoalsInView) {
      const share = s.workShare;
      rail.child(v_flex().id(`sh-${s.id}`).gap(3).p(8).rounded(6).bg(withAlpha(s.tint, 0.06)).border(1).border_color(withAlpha(s.tint, 0.22))
        .when(vm.global, (el) => el.cursor_pointer().hover((st) => st.bg(withAlpha(s.tint, 0.12))).on_click((_e, cx) => { this.drill(s.id, cx); cx.notify(); }))
        .child(h_flex().gap(7).items_center()
          .child(this.pulseDot(s.urgentColor, 8, s.needsYou > 0))
          .child(div().font_semibold().text_color(s.tint).child(s.short))
          .child(div().flex_1())
          .when(s.offline, (el) => el.child(div().text_color(P.steel2).child("offline")))
          .when(s.needsYou > 0, (el) => el.child(div().px(5).rounded(7).bg(P.magenta).text_color("#0b0b10").font_bold().text_size(9.5).child(String(s.needsYou)))))
        .child(h_flex().gap(10).text_color(P.dim)
          .child(div().child(`done ${s.done24}/24h ${s.done7}/7d`))
          .child(div().child(`${s.msgs24} msg/24h`)))
        .child(h_flex().gap(8).items_center()
          .when(share != null, (el) => el
            .child(div().w(80).h(4).rounded(2).bg(withAlpha(P.steel1, 0.6)).child(div().h(4).rounded(2).bg(P.teal).w(Math.round(80 * clamp(share, 0, 1)))))
            .child(div().text_color(P.dim).child(`work ${Math.round(share * 100)}%`)))
          .when(share == null, (el) => el.child(div().text_color(P.faint).child("work share n/a")))
          .child(div().flex_1())
          .child(sparkline(this.history.get(s.id) ?? [], 56, 14, withAlpha(s.tint, 0.9))))
        .when(s.starved > 0, (el) => el.child(div().text_color(P.dim).child(`${s.starved} with no work assigned`)))
        .when(s.grants > 0 || s.pages24 > 0 || s.asks > 0, (el) => el.child(div().text_color(P.dim)
          .child(`asks ${s.asks} · grants ${s.grants} · pages ${s.pages24}/24h`))));
    }

    rail.child(section("HOSTS"));
    for (const h of hostSummaries(vm)) {
      rail.child(v_flex().gap(1).px(6)
        .child(h_flex().gap(7).items_center().child(dot(h.color, 8)).child(div().font_semibold().text_color(h.color).child(`${h.name}${h.home ? " ⌂" : ""}`))
          .child(div().flex_1()).child(div().text_color(P.dim).child(`${h.roles} roles`)))
        .child(div().pl(15).text_color(P.dim).child(`work ${h.working} · wait ${h.waiting}${h.needsYou ? ` · you ${h.needsYou}` : ""} · ${h.msgs24}/24h${h.jobsIn ? ` · CI in ${h.jobsIn}` : ""}`)));
    }
    if (vm.hiddenN > 0) {
      rail.child(div().id("show-hidden").px(6).cursor_pointer().text_color(P.dim).hover((st) => st.text_color(P.ink))
        .on_click((_e, cx) => this.setHidden([], cx)).child(`${vm.hiddenN} hidden: show them`));
    }
    rail.child(div().text_size(9).text_color(P.faint).child("drag: pan · wheel: zoom · click: pin · right-click: actions · esc: back · f: fit · l: legend"));
    return rail;
  }

  /** A board's numbers: the table, with a bar under it. */
  boardTable(counts, key, width) {
    const table = h_flex().justify_between();
    boardCells(counts).forEach((c) => table.child(v_flex().id(`${key}-${c.label}`).items_center().tooltip(c.about)
      .child(div().font_semibold().text_color(c.color).child(String(c.n)))
      .child(div().text_size(9).text_color(P.dim).child(c.label))));
    const bar = h_flex().h(4).rounded(2).overflow_hidden();
    boardBar(counts, width).forEach((seg) => bar.child(div().h(4).flex_none().w(seg.w).bg(seg.color)));
    return v_flex().gap(4).child(table).child(bar);
  }

  /** One shoal's priority boards: a link and how old the counts are, then the table with a bar under it. */
  renderBoards(s, named) {
    const B = s.boards;
    const box = v_flex().id(`bd-${s.id}`).gap(4).p(8).rounded(6).bg(withAlpha(s.tint, 0.06)).border(1).border_color(withAlpha(s.tint, 0.22));
    B.items.forEach((b, i) => {
      const tagged = named && b.title !== s.short;  // a board named for its shoal says so once
      box.child(h_flex().gap(7).items_center()
        .when(tagged, (el) => el.child(div().flex_none().font_semibold().text_color(s.tint).child(s.short)))
        .child(div().id(`bd-${s.id}-${i}`).min_w(0).truncate().tooltip(b.url)
          .child(Link.new(`bd-link-${s.id}-${i}`).href(b.url).text_color(tagged ? P.ink : s.tint).child(`${b.title} ↗`)))
        .child(div().flex_1())
        .when(B.age != null, (el) => el.child(div().flex_none().text_size(9.5).text_color(B.stale ? P.amber : P.faint).child(fmtAge(B.age)))));
      if (!b.counts) {
        box.child(div().text_color(P.faint).child(B.note));
        return;
      }
      box.child(this.boardTable(b.counts, `bd-${s.id}-${i}`, BOARD_BAR_W));
    });
    const off = offBoardText(B.offBoard);
    if (off) box.child(div().id(`bd-off-${s.id}`).text_size(9.5).text_color(P.dim).tooltip("work roles hold that is not open on this board").child(off));
    return box;
  }

  /**
   * The pulse drawer: one row per shoal (its loudest state's dot, the needs-you count, its tag,
   * msg/h), then up to five needs-you roles (badge, role, wait event, host). Dots and counts sit on
   * the edge side of each row, so they show while the drawer peeks; rows mirror on the right edge
   * and run side by side on a top or bottom edge. Still unless the motion tier is full.
   */
  renderPulse() {
    const pl = placement();
    const vertical = pl ? pl.vertical : true;
    const right = pl?.edge === "right";
    const pm = this.pm;
    const animate = this.policy.tier === "full";
    // the drawer sits in Herdr's frame on the theme background, so its text follows the theme
    const t = theme();
    const ink = t.foreground ?? P.ink, dim = t.muted ?? P.dim, faint = t.dim ?? P.faint, rule = t.border ?? P.steel1;
    const col = (vertical ? v_flex() : h_flex()).id("pulse").py(4).gap(1).font_family(MONO).text_size(10.5).text_color(ink);
    if (!pm) return col.child(div().px(10).text_color(this.error ? P.red : dim).child(this.error ? "feed error" : "…"));
    const mirror = (row) => (right ? row.flex_row_reverse() : row);
    const lead = (el) => div().w(14).h(14).flex_none().flex().items_center().justify_center().child(el);
    for (const s of pm.shoals) {
      const state = (STATE_STYLE[s.urgent] ?? STATE_STYLE.idle).label;
      col.child(mirror(h_flex().id(`pulse-${s.id}`).gap(7).px(10).py(3).items_center().cursor_pointer()
        .hover((st) => st.bg(withAlpha(s.tint, 0.12)))
        .tooltip(`${s.short}: ${s.needsYou ? `${s.needsYou} need you` : "nothing waits on you"} · loudest: ${state}`)
        .on_click((_e, cx) => {
          const first = pm.needs.find((r) => r.shoal === s.id);
          setShared(FOCUS_KEY, { shoal: s.id, node: first?.id ?? null, t: Date.now() });
          cx.notify();
        })
        .child(lead(this.pulseDot(s.urgentColor, 9, animate && s.needsYou > 0)))
        .child(div().w(18).flex_none().text_color(s.needsYou ? P.magenta : faint).font_bold().child(s.needsYou ? String(s.needsYou) : "·"))
        .child(div().w(84).flex_none().truncate().text_color(s.tint).child(s.short))
        .child(div().w(52).flex_none().text_color(dim).child(`${s.msgs24}/24h`))));
    }
    if (pm.needs.length) {
      col.child(vertical ? div().mx(10).my(3).h(1).bg(rule) : div().my(4).mx(2).w(1).bg(rule));
      for (const r of pm.needs) {
        col.child(mirror(h_flex().id(`pulse-ny-${r.id}`).gap(7).px(10).py(2).items_center().cursor_pointer()
          .hover((st) => st.bg(withAlpha(r.color, 0.16)))
          .tooltip(`${r.label} @${r.short}: ${r.event}${r.host ? ` on ${r.host}` : ""} · ${fmtAge(r.since_s)} · click: focus it in the lattice tab`)
          .on_click((_e, cx) => {
            setShared(FOCUS_KEY, { node: r.id, shoal: r.shoal, t: Date.now() });
            cx.notify();
          })
          .child(lead(div().w(14).h(14).rounded(7).bg(r.color).flex().items_center().justify_center()
            .text_size(8.5).font_bold().text_color("#0b0b10").child(r.badge)))
          .child(div().w(18).flex_none())
          .child(div().w(84).flex_none().truncate().font_semibold().text_color(ink).child(r.label))
          .child(div().w(76).flex_none().truncate().text_color(r.color).child(r.event))
          .child(div().w(56).flex_none().truncate().text_color(dim).child(r.host ?? ""))));
      }
      if (pm.more) col.child(mirror(h_flex().px(10).py(1).child(div().w(39).flex_none()).child(div().text_size(9.5).text_color(faint).child(`+${pm.more} more`))));
    }
    return col;
  }
}

export { STATE_STYLE };
