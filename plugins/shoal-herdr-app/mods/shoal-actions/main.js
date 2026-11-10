// shoal-actions: the operator actions dialog (Herdr app mod), in place of herdr 0.9.3's popup palette.
//
// A thin View over model.js (pure, tested under Node). It runs one CLI, `shoal ui act-plan` and
// `shoal ui act-apply`, on the machine of the role's card (exec `on`), and writes nothing itself:
// a write verb is planned (the CLI's --dry-run preview, the wake cost and a one-use token), shown,
// and applied only on Confirm with that token, so the CLI keeps owning validation and the
// `ui-actions.jsonl` log. It never types into a pane. The token stays in memory; nothing from the
// environment is shown or logged.
//
// Opened by `mod.open shoal-actions` (a key toggles it), by a lattice node's right-click, or with
// openOverlay({mod: "shoal-actions", kind: "dialog", props}).
// props:
//   shoal, role, machine: the target (machine: a herdr-core machine name, null for this computer).
//   scope: "all" or "host:<name>" in place of a shoal: only the verbs that span shoals (offline, freeze).
//   session: a saved standalone session's label in place of a shoal: only its removal.
//   Add session (any target): lists every machine's candidate sessions, saves the one picked on its own
//     machine (`ui pick --save`), then opens its space here (`ui app-open --show`).
//     Without them the dialog starts on the focused card (its `shoal.pane` label and its space's
//     machine), else on the first shoal with a role that needs you.
//   verb: open straight on one verb ("msg", "next", "asks", "queue", ...); text, wake: prefill Message.
//   Queue (a role): its queue and candidates (`act-plan queue`), laid out in a draft (pick a row or candidate, then
//     click a slot; up/down; drop; mark blocked), planned as one batch (`act-plan queue-set`) and confirmed like any plan.
//   pick (fixture only): a Queue item key picked at open, so the slots show without a click.
//   fixture: true answers from the bundled fixtures/act.json instead of running shoal (every
//     screen renders without the CLI; a field value containing "fail" gets the error reply).

import { View, div } from "gpui-kit";
import { h_flex, v_flex, InputState, Input, Link } from "gpui-base";
import { exec, readTextFile, appHome, props, theme, terminals, focused, spaces, panes, machines, closeOverlay, nextCommand } from "herdr";
import {
  MOD, VERBS, verbInfo, isWrite, isView, FIELDS, newForm, visibleFields, textFields, nextField, setValue, roleFor,
  validate, rolesArgv, planArgv, applyArgv, statusArgv, planExpired, parseReply, textLines, preselect, settleSelection,
  targetMachine, backStep, keyAction, answerTarget, resultLine, fixtureReply, palette, withAlpha, shortShoal,
  verbsFor, scopeLabel, handoffMachine, lastLines, runsAcross, runLabel, sessionsArgv, openSeatArgv, mergeSessions, filterSessions,
  providerOptions, isQueue, QUEUE_SRCS, SRC_LABEL, srcColor, newDraft, draftPick, draftPlace, draftMove, draftRemove, draftBlock,
  draftChanged, draftCandidates, draftProblem, pendingTransfers, queueSetArgv, draftRefused, commandLines, handoffDocNote, verbGroups,
} from "./model.js";

const MONO = "Geist Mono";
const PROGRESS_MS = 1000;
const PROGRESS_BYTES = 8000;
// Flipped to true only in a scratch copy, to look at every screen with no props (mod.open).
const FIXTURE_DEFAULT = false;
const TEXT_KEYS = ["text", "item", "note", "until", "model", "effort", "label", "slug", "title", "doc", "host", "guidance", "nextSteps", "grants"];

const str = (v) => (typeof v === "string" ? v : v == null ? "" : String(v));

const safe = (f, d) => {
  try {
    const v = f();
    return v ?? d;
  } catch (_e) {
    return d;
  }
};

export default class Actions extends View {
  init(_props, cx) {
    const p = props() ?? {};
    this.fixture = p.fixture === true || (FIXTURE_DEFAULT && p.fixture !== false);
    this.fx = null;
    const start = preselect({ props: p, focused: safe(focused, null), terminals: safe(terminals, []), spaces: safe(spaces, []), panes: safe(panes, []) });
    this.sel = { shoal: start?.shoal ?? null, role: start?.role ?? null, scope: start?.scope ?? null, session: start?.session ?? null };
    this.origin = start?.machine ?? null; // where the roles list is read, and the fallback machine
    this.applying = false; // an apply in flight: its result must land, so the target stays put
    this.roles = null;
    this.rolesError = null;
    this.verb = null;
    this.screen = "menu";
    this.form = null;
    this.plan = null; // the plan as shown (no token)
    this.token = null; // the plan's one-use token, memory only
    this.result = null;
    this.view = null;
    this.error = null;
    this.hint = null;
    this.busyWhat = "";
    this.seq = 0;
    this.lastEnter = 0;
    this.focusKey = null;
    this.pendingVerb = verbInfo(p.verb) ? p.verb : null;
    this.sessions = null; // Add session: every machine's candidates, merged
    this.sessionsNote = null;
    this.sessionsSeq = 0;
    this.prefill = { text: typeof p.text === "string" ? p.text : undefined, wake: p.wake === true ? true : undefined };
    this.queue = null; // Queue: the role's queue as read
    this.draft = null; // and the layout proposed for it
    this.blocking = null; // the item whose blocker is being typed

    this.inputs = {};
    for (const key of TEXT_KEYS) {
      const st = InputState.new({ placeholder: FIELDS[key].placeholder ?? "" });
      st.on("change", (_e, cx) => {
        if (this.form) {
          const note = this.form.values.note;
          this.form = setValue(this.form, key, st.value());
          if (key !== "note" && this.form.values.note !== note) this.inputs.note.set_value(String(this.form.values.note ?? ""));
        }
        if (this.hint) this.hint = null;
        cx.notify();
      });
      st.on("submit", (_e, cx) => { this.enter(cx); cx.notify(); });
      st.on("focus", (_e, cx) => { this.focusKey = key; cx.notify(); });
      st.on("blur", (_e, cx) => { if (this.focusKey === key) this.focusKey = null; cx.notify(); });
      this.inputs[key] = st;
    }
    this.qFilter = InputState.new({ placeholder: "filter: item, title, role or source" });
    this.qFilter.on("change", (_e, cx) => cx.notify());
    this.qReason = InputState.new({ placeholder: "what it waits on (Enter sets it)" });
    this.qReason.on("submit", (_e, cx) => { this.setBlocked(cx); cx.notify(); });
    this.handle = cx.focus_handle();
    this.handle.focus();
    cx.timer.after(400, () => { if (!this.focusKey) this.handle.focus(); });
    this.live = "";
    this.liveFrom = null;
    this.liveBusy = false;
    cx.timer.every(PROGRESS_MS, (cx) => this.followProgress(cx));
    this.loadRoles(cx);
    cx.spawn(async (cx) => {
      for (;;) {
        const c = await nextCommand();
        if (c === "focus") this.handle.focus();
        else this.command(c, cx);
        cx.notify();
      }
    });
  }

  // ---- calls

  async fixtures() {
    if (!this.fx) {
      const text = await readTextFile(`${appHome()}/mods/${MOD}/fixtures/act.json`);
      if (text == null) throw new Error("fixture not found");
      this.fx = JSON.parse(text);
    }
    return this.fx;
  }

  /** One CLI call: an exec result, or the Error an exec rejected with (parseReply reads both). */
  async call(argv, machine) {
    try {
      if (this.fixture) return fixtureReply(await this.fixtures(), argv, this.plan);
      return await exec("shoal", argv, { on: machine ?? null });
    } catch (e) {
      return e instanceof Error ? e : new Error(String(e));
    }
  }

  /** The role's card's machine, else the shoal's cards', else where the dialog started. */
  machine() {
    // a seat is saved on the machine its session runs on
    if (this.verb === "add-session") return this.form?.values?.pick?.machine ?? null;
    if (this.sel.scope || this.sel.session) return this.origin;
    const m = targetMachine(this.sel, safe(terminals, []), safe(spaces, []), this.origin);
    if (this.verb !== "handoff" && this.verb !== "remove-role") return m;  // a session starts or stops on its own host
    const role = this.roles?.shoals?.find((s) => s.id === this.sel.shoal)?.roles?.find((r) => r.slug === this.sel.role);
    return handoffMachine(role, safe(machines, []).map((x) => x.name).filter(Boolean), m, this.roles?.host ?? null);
  }

  loadRoles(cx) {
    this.rolesError = null;
    cx.spawn(async (cx) => {
      const r = parseReply(await this.call(rolesArgv(), this.origin), "roles");
      if (r.kind === "roles") {
        this.roles = r;
        const before = this.sel.shoal;
        if (!this.sel.scope && !this.sel.session) this.sel = settleSelection(this.sel, r);
        if (this.sel.shoal !== before && this.view) this.view = null;
      } else this.rolesError = r.error;
      if (this.pendingVerb) {
        const v = this.pendingVerb;
        this.pendingVerb = null;
        this.pick(v, cx, this.prefill);
      }
      cx.notify();
    });
  }

  /** Runs a call on the target machine, ignoring a reply that a newer call has overtaken. */
  /** While an apply runs: its output so far. `exec` returns only at the end, so the CLI writes a file and this reads its tail. */
  followProgress(cx) {
    if (!this.applying || !this.liveFrom || this.liveBusy) return;
    if (this.liveFrom.run) return this.followHandoff(cx);
    const { path, machine } = this.liveFrom;
    this.liveBusy = true;
    cx.spawn(async (cx) => {
      try {
        const r = await exec("tail", ["-c", String(PROGRESS_BYTES), path], { on: machine ?? null });
        if (this.applying && r?.code === 0 && r.stdout !== this.live) {
          this.live = r.stdout;
          cx.notify();
        }
      } catch (_e) {
        // the next tick tries again; the result still comes whole
      }
      this.liveBusy = false;
    });
  }

  followHandoff(cx) {
    const from = this.liveFrom;
    if (!from?.run || this.liveBusy) return;
    this.liveBusy = true;
    cx.spawn(async (cx) => {
      try {
        const r = parseReply(await this.call(statusArgv(this.sel, from.run), from.machine), "result");
        if (this.liveFrom !== from) return;
        if (r.kind === "result") {
          this.live = r.output;
          if (!r.running) {
            this.applying = false;
            this.liveFrom = null;
            this.result = { ...r, plan: this.plan };
            this.screen = "result";
          }
          cx.notify();
        }
      } finally { this.liveBusy = false; }
    });
  }

  resumeHandoff(cx) {
    if (this.fixture) return;
    const seq = this.seq, machine = this.machine();
    cx.spawn(async (cx) => {
      const r = parseReply(await this.call(statusArgv(this.sel), machine), "result");
      if (this.verb !== "handoff" || seq !== this.seq || r.kind !== "result" || !r.run) return;
      this.live = r.output;
      if (r.running) {
        this.applying = true;
        this.liveFrom = { run: r.run, machine };
        this.busyWhat = "waiting for checkpoint and handoff";
        this.screen = "busy";
      } else {
        this.result = r;
        this.screen = "result";
      }
      cx.notify();
    });
  }

  request(what, argv, machine, expect, done, cx) {
    const seq = ++this.seq;
    this.screen = "busy";
    this.busyWhat = what;
    cx.spawn(async (cx) => {
      const reply = parseReply(await this.call(argv, machine), expect);
      if (seq !== this.seq) return;
      done(reply, cx);
      cx.notify();
    });
  }

  // ---- verbs

  pick(verb, cx, prefill = null) {
    const info = verbInfo(verb);
    if (!info || this.applying) return;
    this.seq++; // an earlier call's reply no longer lands
    this.dropPlan();
    this.result = null;
    this.error = null;
    this.hint = null;
    this.view = null;
    const same = this.verb === verb && this.form;
    this.verb = verb;
    if (info.kind === "write") {
      if (!same || prefill) {
        const pre = {};
        for (const [k, v] of Object.entries(prefill ?? {})) if (v !== undefined) pre[k] = v;
        this.form = newForm(verb, new Date(), { ...pre, standalone: Boolean(this.sel.session) });
        this.syncInputs();
      }
      this.screen = "form";
      this.focusFirst(cx);
      if (verb === "add-session") this.loadSessions(cx);
      if (verb === "handoff") this.resumeHandoff(cx);
    } else if (info.kind === "queue") {
      this.form = null;
      this.loadQueue(cx);
    } else {
      this.form = null;
      this.runView(cx);
    }
  }

  /** Every machine's candidate sessions (one shoal machine when the dialog was opened on a host), read in parallel. */
  loadSessions(cx) {
    this.sessions = null;
    this.sessionsNote = null;
    const host = this.sel.scope?.startsWith("host:") ? this.sel.scope.slice(5) : null;
    const names = host ? [host] : [null, ...safe(machines, []).map((m) => m.name).filter(Boolean)];
    const seq = ++this.sessionsSeq;
    cx.spawn(async (cx) => {
      const replies = await Promise.all(names.map(async (m) => {
        const r = parseReply(await this.call(sessionsArgv(), m), "view");
        return { machine: m, sessions: r.kind === "view" ? r.sessions ?? [] : [], error: r.kind === "error" ? r.error : null };
      }));
      if (seq !== this.sessionsSeq) return;
      const got = mergeSessions(replies);
      this.sessions = got.rows;
      this.sessionsNote = got.note;
      cx.notify();
    });
  }

  /** Picks a candidate; its label becomes the seat's name unless one was typed. */
  chooseSession(s) {
    const was = this.form.values;
    const typed = str(was.label).trim() && was.label !== was.pick?.label;
    this.form = setValue(this.form, "pick", s);
    if (!typed) {
      this.form = setValue(this.form, "label", s.label);
      this.inputs.label.set_value(s.label);
    }
    this.hint = null;
  }

  /** After a seat is saved: open its space in this app, as `ui app-open --show` does by hand. */
  openSeat(label, machine, cx) {
    if (!this.result) return;
    this.result.open = "opening its space…";
    cx.spawn(async (cx) => {
      const r = await this.call(openSeatArgv(label, machine), null); // runs here; it reads that machine's plan
      if (this.result) {
        this.result.open = r instanceof Error ? `could not open its space: ${r.message} (run \`shoal ui app-open --show ${label}${machine ? ` --machine ${machine}` : ""}\`)`
          : r.code === 0 ? "its space is open" : `app-open exit ${r.code}: ${(r.stderr || r.stdout || "").trim().split("\n").pop()}`;
      }
      cx.notify();
    });
  }

  syncInputs() {
    for (const key of TEXT_KEYS) this.inputs[key].set_value(String(this.form?.values?.[key] ?? ""));
  }

  focusFirst(cx) {
    const keys = textFields(this.form);
    if (keys.length) {
      const k = keys[0];
      this.inputs[k].focus();
      cx.timer.after(60, () => this.inputs[k].focus());
    } else this.handle.focus();
  }

  runView(cx) {
    const why = validate(this.verb, this.sel);
    if (why) {
      this.error = why;
      this.screen = "error";
      return;
    }
    const machine = this.machine();
    const verb = this.verb;
    this.request(`reading ${verbInfo(verb).label.toLowerCase()}`, planArgv(verb, this.sel), machine, "view", (r) => {
      if (r.kind === "view") {
        this.view = { ...r, machine };
        this.screen = "view";
      } else {
        this.error = r.error;
        this.screen = "error";
      }
    }, cx);
  }

  preview(cx) {
    if (!isWrite(this.verb) || this.applying || this.screen === "busy") return;
    const why = validate(this.verb, this.sel, this.form);
    if (why) {
      this.hint = why;
      return;
    }
    const machine = this.machine();
    this.dropPlan();
    const dry = this.form?.values?.dryRun === true;
    this.request(dry ? "planning (dry run)" : "starting", planArgv(this.verb, this.sel, this.form), machine, "plan", (r, cx) => {
      if (r.kind !== "plan") {
        this.error = r.kind === "error" ? r.error : "the CLI answered with a view, not a plan";
        this.screen = "error";
        return;
      }
      const { token, ...shown } = r;
      this.token = token;
      this.plan = { ...shown, machine, plannedAt: Date.now() };
      if (!dry) this.apply(cx, true); // no preview asked for: this click was the go
      else {
        this.screen = "plan";
        this.handle.focus();
      }
    }, cx);
  }

  apply(cx, auto = false) {
    if (!this.plan || !this.token) return;
    if (planExpired(this.plan)) {
      this.dropPlan();
      this.hint = "the plan expired: preview it again";
      this.screen = "form";
      return;
    }
    const token = this.token;
    this.token = null; // one use
    const plan = this.plan;
    this.applying = true;
    this.live = "";
    this.liveFrom = plan.progress ? { path: plan.progress, machine: plan.machine } : null;
    this.request("applying", applyArgv(token, this.verb === "handoff"), plan.machine, "result", (r) => {
      if (r.kind === "result" && r.running && r.run) {
        this.liveFrom = { run: r.run, machine: plan.machine };
        this.live = r.output;
        this.busyWhat = "waiting for checkpoint and handoff";
        return;
      }
      this.applying = false;
      this.liveFrom = null;
      if (r.kind === "result") {
        this.result = { ...r, auto, plan };
        this.screen = "result";
        if (r.ok && this.verb === "add-session") this.openSeat(str(this.form?.values?.label).trim(), plan.machine, cx);
      } else {
        this.error = r.error;
        this.screen = "error";
      }
      this.handle.focus();
    }, cx);
  }

  dropPlan() {
    this.plan = null;
    this.token = null;
  }

  back(cx) {
    if (this.applying) return; // the apply's result is on its way: stay for it
    const to = backStep(this.screen, this.verb);
    this.seq++;
    this.hint = null;
    if (to === "close") return this.close();
    if (to === "queue") {
      const applied = Boolean(this.result);
      this.dropPlan();
      this.result = null;
      this.error = null;
      if (applied || !this.queue) return this.loadQueue(cx); // what was applied shows as the role's queue now
      this.screen = "queue";
      this.handle.focus();
      return;
    }
    if (to === "form" && this.form) {
      this.dropPlan();
      this.result = null;
      this.error = null;
      this.screen = "form";
      this.focusFirst(cx);
    } else {
      this.dropPlan();
      this.result = null;
      this.error = null;
      this.view = null;
      this.verb = null;
      this.screen = "menu";
      this.handle.focus();
    }
  }

  close() {
    this.dropPlan();
    closeOverlay(props()?.overlay);
  }

  enter(cx) {
    const now = Date.now();
    if (now - this.lastEnter < 250) return; // the input's submit and the key event are one press
    this.lastEnter = now;
    const a = keyAction("enter", this.screen, { canConfirm: Boolean(this.token) });
    if (a === "preview" && runsAcross(this.sel, this.form)) this.hint = `Enter does not run this across shoals: use "${runLabel(this.sel, this.verb)}"`;
    else if (a === "preview") this.preview(cx);
    else if (a === "confirm") this.apply(cx);
  }

  key(e, cx) {
    const a = keyAction(e.keystroke, this.screen, { canConfirm: Boolean(this.token) });
    if (!a) return;
    cx.stop_propagation();
    if (a === "close") this.close();
    else if (a === "back") this.back(cx);
    else if (a === "preview" || a === "confirm") this.enter(cx);
    else if (a === "next-field" || a === "prev-field") {
      const k = nextField(this.form, this.focusKey, a === "prev-field");
      if (k) this.inputs[k].focus();
    }
    cx.notify();
  }

  command(c, cx) {
    if (verbInfo(c)) this.pick(c, cx);
    else if (c === "preview") this.preview(cx);
    else if (c === "confirm") this.apply(cx);
    else if (c === "back") this.back(cx);
    else if (c === "refresh") this.loadRoles(cx);
  }

  selectShoal(id, cx) {
    if (this.sel.shoal === id || this.applying) return;
    const s = this.roles?.shoals?.find((x) => x.id === id);
    const ny = s?.roles?.find((r) => r.needsYou);
    this.sel = { shoal: id, role: ny?.slug ?? null };
    this.retarget(cx);
  }

  selectRole(slug, cx) {
    if (this.applying) return;
    this.sel = { ...this.sel, role: this.sel.role === slug ? null : slug };
    this.retarget(cx);
  }

  /** A new target drops any plan (it was for the old one) and re-reads an open view. */
  retarget(cx) {
    this.seq++;
    this.hint = null;
    if (isQueue(this.verb)) {
      this.dropPlan();
      this.result = null;
      this.error = null;
      return this.loadQueue(cx);
    }
    if (this.plan || this.screen === "plan" || this.screen === "result" || (this.screen === "busy" && isWrite(this.verb))) {
      this.dropPlan();
      this.result = null;
      this.screen = this.form ? "form" : "menu";
    }
    if (this.screen === "error") {
      this.error = null;
      this.screen = this.form ? "form" : "menu";
    }
    if (isView(this.verb)) this.runView(cx);
  }

  answer(ask, cx, n = null) {
    const a = answerTarget(this.sel, ask, new Date(), n);
    this.sel = { shoal: a.target.shoal, role: a.target.role };
    this.verb = null;
    this.pick("msg", cx, { wake: true, ...(Number.isInteger(n) ? { text: `re ask ${n}: ` } : {}) });
  }

  // ---- the queue manager

  loadQueue(cx) {
    const why = validate("queue", this.sel);
    if (why) {
      this.error = why;
      this.screen = "error";
      return;
    }
    const machine = this.machine();
    this.blocking = null;
    this.request("reading the queue", planArgv("queue", this.sel), machine, "view", (r) => {
      if (r.kind === "view" && r.queue) {
        this.queue = r.queue;
        this.draft = newDraft(r.queue);
        // fixture only: open with an item picked, to look at the slots
        if (this.fixture && typeof props()?.pick === "string") this.draft = draftPick(this.draft, props().pick);
        this.screen = "queue";
        this.handle.focus();
      } else {
        this.error = r.kind === "error" ? r.error : "the CLI answered without a queue";
        this.screen = "error";
      }
    }, cx);
  }

  edit(f) {
    this.draft = f(this.draft);
    this.hint = null;
    if (this.draft.refused && Object.keys(this.draft.refused).length) this.draft = { ...this.draft, refused: {} };
  }

  /** A slot was clicked: the picked row or candidate goes there. */
  place(pos) {
    if (this.draft?.picked) this.edit((d) => draftPlace(d, this.queue, d.picked, pos));
  }

  startBlocked(item) {
    this.blocking = this.blocking === item ? null : item;
    this.qReason.set_value(this.draft.blocked[item] ?? "");
    if (this.blocking) this.qReason.focus();
  }

  setBlocked(_cx) {
    if (!this.blocking || !this.draft) return;
    const item = this.blocking;
    this.edit((d) => draftBlock(d, item, this.qReason.value()));
    this.blocking = null;
    this.handle.focus();
  }

  previewQueue(cx) {
    if (!this.draft || this.applying || this.screen === "busy") return;
    const why = draftProblem(this.draft, this.queue);
    if (why) {
      this.hint = why;
      return;
    }
    const machine = this.machine();
    this.dropPlan();
    this.request("planning the queue (dry run)", queueSetArgv(this.sel, this.draft), machine, "plan", (r) => {
      if (r.kind === "plan") {
        const { token, ...shown } = r;
        this.token = token;
        this.plan = { ...shown, machine, plannedAt: Date.now() };
        this.screen = "plan";
      } else if (r.kind === "error" && r.refused) {
        // a dry run refused some items: they are marked on their rows, and the layout stays to fix
        this.draft = draftRefused(this.draft, r.refused);
        this.hint = r.error;
        this.screen = "queue";
      } else {
        this.error = r.kind === "error" ? r.error : "the CLI answered with a view, not a plan";
        this.screen = "error";
      }
      this.handle.focus();
    }, cx);
  }

  tiny(id, label, onClick, c, { tip = null, color = null, on = false } = {}) {
    return h_flex().id(id).h(20).px(6).items_center().rounded(4).flex_none().cursor_pointer()
      .border_1().border_color(on ? c.accent : c.input).bg(on ? withAlpha(c.accent, 0.2) : c.raised)
      .text_size(10.5).text_color(color ?? c.ink).hover((s) => s.bg(c.hover))
      .when(Boolean(tip), (el) => el.tooltip(tip)).on_click(onClick).child(label);
  }

  itemCell(id, r, c, color) {
    const cell = h_flex().gap(6).items_center().min_w(0).flex_1()
      .child(div().w(7).h(7).rounded(4).flex_none().bg(color));
    cell.child(r.url ? Link.new(`${id}-link`).href(r.url).flex_none().text_size(11.5).font_family(MONO).text_color(c.ink).child(r.item)
      : div().flex_none().text_size(11.5).font_family(MONO).text_color(c.ink).child(r.item));
    if (r.title) cell.child(div().flex_1().min_w(0).truncate().text_size(11).text_color(c.dim).child(r.title));
    return cell;
  }

  slot(pos, c) {
    return h_flex().id(`slot-${pos}`).h(18).mx(4).px(8).items_center().rounded(4).cursor_pointer()
      .border_1().border_color(withAlpha(c.accent, 0.6)).bg(withAlpha(c.accent, 0.08))
      .text_size(10.5).text_color(c.accent).hover((s) => s.bg(withAlpha(c.accent, 0.2)))
      .on_click((_e, cx) => { this.place(pos); cx.notify(); }).child(`place here (${pos + 1})`);
  }

  queueBody(c) {
    const q = this.queue;
    const d = this.draft;
    const go = (f) => (_e, cx) => { f(cx); cx.notify(); };
    const col = v_flex().gap(8);
    col.child(h_flex().gap(8).items_center()
      .child(div().text_size(13.5).font_semibold().child("Queue"))
      .child(div().text_size(11.5).text_color(c.muted).child(this.target(c)))
      .child(div().text_size(11).text_color(c.dim).child(`fill=${q.fill} · at most ${q.nextMax} next`))
      .child(div().flex_1())
      .child(this.tiny("q-again", "read again", go((cx) => this.loadQueue(cx)), c, { tip: "read the queue again (drops this layout)" })));
    if (!q.verbs.queueOrder || !q.verbs.transfer) {
      col.child(div().text_size(11).text_color(c.amber).child("This host's shoal does not have the queue verbs yet (queue-order, transfer): "
        + "you can lay a queue out, and Preview says what is missing."));
    }
    if (q.order?.status === "pending") {
      col.child(div().text_size(11).text_color(c.amber).child(`An order is pending (not acked yet): ${q.order.order.join(", ") || "(none)"}. A new one replaces it.`));
    }
    const pending = pendingTransfers(q);
    for (const t of pending.values()) {
      col.child(div().text_size(11).text_color(c.violet).child(`transfer pending: ${t.item} ${t.from ?? "?"} → ${t.to ?? "?"} (waits for the holder's hand-off)`));
    }

    // its queue: the active item pinned, then the next items with slots while something is picked
    col.child(this.label(d.picked ? "its queue: click a slot to place the picked item" : "its queue", c));
    const list = v_flex().id("q-list").gap(3);
    const active = q.queue.find((r) => r.active);
    if (active) {
      list.child(h_flex().id("q-active").gap(8).px(8).py(4).rounded(6).bg(c.panel).items_center()
        .child(div().w(42).flex_none().text_size(10.5).font_medium().text_color(c.working).child("active"))
        .child(this.itemCell("q-active", active, c, srcColor(active.src, c)))
        .child(div().text_size(10.5).text_color(c.dim).child("pinned")));
    }
    if (!d.rows.length && !d.picked) list.child(div().px(8).text_size(11.5).text_color(c.dim).child("no next items: pick a candidate below, then a slot"));
    d.rows.forEach((r, i) => {
      if (d.picked && d.picked !== r.key) list.child(this.slot(i, c));
      const color = srcColor(r.src, c);
      const refused = d.refused?.[r.key];
      const blocked = d.blocked[r.item] ?? null;
      const moving = r.src === "other";
      const row = v_flex().id(`q-${i}`).gap(2).px(8).py(4).rounded(6)
        .bg(d.picked === r.key ? withAlpha(c.accent, 0.18) : moving ? withAlpha(c.violet, 0.1) : c.panel)
        .border_1().border_color(refused ? withAlpha(c.red, 0.7) : moving ? withAlpha(c.violet, 0.55) : c.border);
      const line = h_flex().gap(6).items_center()
        .child(div().w(18).flex_none().text_size(10.5).text_color(c.dim).child(String(i + 1)))
        .child(this.itemCell(`q-${i}`, r, c, color))
        .child(this.tiny(`q-up-${i}`, "↑", go(() => this.edit((x) => draftMove(x, r.key, -1))), c, { tip: "up one" }))
        .child(this.tiny(`q-dn-${i}`, "↓", go(() => this.edit((x) => draftMove(x, r.key, 1))), c, { tip: "down one" }))
        .child(this.tiny(`q-mv-${i}`, "Move", go(() => this.edit((x) => draftPick(x, r.key))), c, { tip: "pick it, then click a slot", on: d.picked === r.key }))
        .when(!moving, (el) => el.child(this.tiny(`q-bl-${i}`, blocked ? "Blocked ✓" : "Blocked", go(() => this.startBlocked(r.item)), c,
          { tip: "tell the role what it waits on", color: blocked ? c.amber : null })))
        .child(this.tiny(`q-rm-${i}`, q.queue.some((x) => x.key === r.key) ? "Drop" : "Remove", go(() => this.edit((x) => draftRemove(x, q, r.key))), c,
          { tip: q.queue.some((x) => x.key === r.key) ? "take it out of the queue" : "back to the candidates" }));
      row.child(line);
      const notes = [];
      if (moving) notes.push([c.violet, `moves from ${r.owner ?? "another role"} (${r.how ?? "held"}): a transfer; it joins once handed off`]);
      else if (!q.queue.some((x) => x.key === r.key)) notes.push([c.muted, r.src === "board" && q.fill !== "self" || r.src === "released" ? "new: offered to it first" : "new"]);
      if (r.blocked) notes.push([c.dim, `recorded blocker: ${r.blocked}`]);
      if (blocked) notes.push([c.amber, `marked blocked: ${blocked}`]);
      if (refused) notes.push([c.red, `refused: ${refused}`]);
      notes.forEach(([col2, t], j) => row.child(div().id(`q-${i}-n${j}`).pl(24).text_size(10.5).text_color(col2).child(t)));
      if (this.blocking === r.item) {
        row.child(h_flex().gap(6).pl(24).items_center()
          .child(Input.new(this.qReason).h(24).flex_1().px(8).rounded(5).border_1().border_color(c.accent).bg(c.code).text_size(11.5))
          .child(this.tiny(`q-bl-set-${i}`, "Set", go((cx) => this.setBlocked(cx)), c))
          .child(this.tiny(`q-bl-clr-${i}`, "Clear", go(() => { this.edit((x) => draftBlock(x, r.item, "")); this.blocking = null; }), c)));
      }
      list.child(row);
    });
    if (d.picked) list.child(this.slot(d.rows.filter((r) => r.key !== d.picked).length, c));
    col.child(list);
    if (d.dropped.length) {
      const dr = h_flex().gap(6).flex_wrap().items_center().child(div().text_size(11).text_color(c.dim).child("dropped:"));
      d.dropped.forEach((r, i) => dr.child(this.tiny(`q-undo-${i}`, `${r.item} ↺`, go(() => this.edit((x) => draftPlace(x, q, r.key, x.rows.length))), c, { tip: "undo the drop" })));
      col.child(dr);
    }
    if (this.hint) col.child(div().text_size(11.5).text_color(c.red).child(this.hint));

    // candidates: by source, another role's in violet
    const cands = draftCandidates(d, q, this.qFilter.value());
    col.child(h_flex().gap(8).items_center().pt(4)
      .child(this.label(`candidates (${cands.length})`, c))
      .child(div().flex_1())
      .children(QUEUE_SRCS.map((s) => h_flex().gap(4).items_center().flex_none()
        .child(div().w(7).h(7).rounded(4).bg(srcColor(s, c)))
        .child(div().text_size(10.5).text_color(c.dim).child(SRC_LABEL[s])))));
    col.child(Input.new(this.qFilter).h(28).px(8).rounded(6).border_1().border_color(c.input).bg(c.code).text_size(12));
    const cl = v_flex().id("q-cands").gap(3);
    if (!cands.length) cl.child(div().px(8).text_size(11.5).text_color(c.dim).child(q.candidates.length ? "none match the filter" : "no candidates: no open offers, board items, releases or other roles' items"));
    cands.forEach((r, i) => {
      const moving = r.src === "other";
      const who = moving ? `${r.owner ?? "?"} · ${r.how ?? ""}` : r.src === "released" ? `released by ${r.by ?? "?"}${r.kind ? ` (${r.kind})` : ""}`
        : r.src === "offer" ? `offered${r.by ? ` by ${r.by}` : ""}${r.prio != null ? ` · prio ${r.prio}` : ""}` : SRC_LABEL[r.src];
      cl.child(h_flex().id(`c-${i}`).gap(6).px(8).py(4).rounded(6).items_center()
        .bg(d.picked === r.key ? withAlpha(c.accent, 0.18) : moving ? withAlpha(c.violet, 0.1) : c.panel)
        .border_1().border_color(moving ? withAlpha(c.violet, 0.55) : c.border)
        .child(this.itemCell(`c-${i}`, r, c, srcColor(r.src, c)))
        .child(div().flex_none().max_w(150).truncate().text_size(10.5).text_color(moving ? c.violet : c.dim).child(who))
        .child(this.tiny(`c-pick-${i}`, d.picked === r.key ? "Picked" : "Pick", go(() => this.edit((x) => draftPick(x, r.key))), c,
          { tip: "pick it, then click a slot in its queue", on: d.picked === r.key }))
        .child(this.tiny(`c-end-${i}`, "+ end", go(() => this.edit((x) => draftPlace(x, q, r.key, x.rows.length))), c,
          { tip: moving ? `move it from ${r.owner}: a transfer` : "add it after the last next item" })));
    });
    col.child(cl);
    return col;
  }

  // ---- render

  render(_cx) {
    const c = palette(safe(theme, {}));
    const root = v_flex().id("actions").size_full().min_h(0).bg(c.bg).text_color(c.ink).text_size(12.5)
      .track_focus(this.handle)
      .on_key_down((e, cx) => this.key(e, cx));
    root.child(this.header(c));
    if (verbsFor(this.sel).length > 1) root.child(this.verbRow(c));  // one verb is already chosen: a button for it does nothing
    root.child(div().h(1).flex_none().bg(c.border));
    root.child(v_flex().id("body").flex_1().min_h(0).overflow_y_scrollbar().px(16).py(12).gap(10).child(this.body(c)));
    root.child(this.footer(c));
    return root;
  }

  chip(id, label, { on = false, mark = null, dimmed = false, tip = null, onClick } , c) {
    return h_flex().id(id).h(24).px(9).gap(6).items_center().rounded(12).flex_none().cursor_pointer()
      .border_1().border_color(on ? c.accent : mark ? withAlpha(mark, 0.6) : c.input)
      .bg(on ? withAlpha(c.accent, 0.18) : c.raised)
      .text_size(11.5).text_color(on ? c.ink : dimmed ? c.dim : c.muted)
      .hover((s) => s.bg(on ? withAlpha(c.accent, 0.26) : c.hover))
      .when(Boolean(tip), (el) => el.tooltip(tip))
      .on_click(onClick)
      .when(Boolean(mark), (el) => el.child(div().w(7).h(7).rounded(4).flex_none().bg(mark)))
      .child(label);
  }

  header(c) {
    const m = this.machine();
    const shoals = this.roles?.shoals ?? [];
    const cur = shoals.find((s) => s.id === this.sel.shoal) ?? null;
    const info = verbInfo(this.verb);
    const roleUse = info ? info.role : "any";
    const head = h_flex().items_center().gap(8)
      .child(div().text_size(15).font_semibold().child("Shoal actions"))
      .child(div().text_size(11).text_color(c.dim).child(`on ${m ?? "this computer"}${this.fixture ? " · fixtures" : ""}`))
      .when(Boolean(this.sel.scope), (el) => el.child(div().text_size(11.5).font_medium().text_color(c.amber).child(scopeLabel(this.sel.scope))))
      .when(Boolean(this.sel.session), (el) => el.child(div().text_size(11.5).font_medium().text_color(c.amber).child(this.sel.session)))
      .child(div().flex_1())
      .child(h_flex().id("refresh").px(6).rounded(5).cursor_pointer().text_size(11).text_color(c.dim)
        .hover((s) => s.text_color(c.ink).bg(c.hover)).tooltip("read the roles again")
        .on_click((_e, cx) => { this.loadRoles(cx); cx.notify(); }).child("↻"))
      .child(div().text_size(11).text_color(c.dim).child("Esc"));
    const col = v_flex().px(16).pt(14).pb(10).gap(8).flex_none().child(head);
    if (this.sel.scope || this.sel.session) return col; // no shoal or role to pick

    const shoalRow = h_flex().gap(6).flex_wrap().items_center().child(div().w(44).flex_none().text_size(11).text_color(c.dim).child("shoal"));
    if (!this.roles) {
      shoalRow.child(div().text_size(11.5).text_color(this.rolesError ? c.red : c.dim)
        .child(this.rolesError ? `roles: ${this.rolesError}` : this.sel.shoal ? `${shortShoal(this.sel.shoal)} · reading the roles…` : "reading the roles…"));
    } else if (!shoals.length) shoalRow.child(div().text_size(11.5).text_color(c.dim).child("no shoals here"));
    for (const s of shoals) {
      shoalRow.child(this.chip(`sh-${s.id}`, s.needsYou ? `${s.tag}  ${s.needsYou}` : s.tag, {
        on: s.id === this.sel.shoal, mark: s.needsYou ? c.blocked : null, tip: `${s.id}${s.needsYou ? ` · ${s.needsYou} need you` : ""}`,
        onClick: (_e, cx) => { this.selectShoal(s.id, cx); cx.notify(); },
      }, c));
    }
    col.child(shoalRow);

    const roleRow = h_flex().gap(6).flex_wrap().items_center().child(div().w(44).flex_none().text_size(11).text_color(c.dim).child("role"));
    if (cur) {
      const unused = roleUse === "none";
      for (const r of cur.roles) {
        roleRow.child(this.chip(`ro-${r.slug}`, r.slug, {
          on: r.slug === this.sel.role && !unused, mark: r.needsYou ? c.blocked : null, dimmed: unused,
          tip: `${r.slug}${r.host ? ` on ${r.host}` : ""}${r.posture ? ` · ${r.posture}` : ""}${r.needsYou ? " · needs you" : ""}${unused ? ` · ${info.label} is shoal-wide` : ""}`,
          onClick: (_e, cx) => { this.selectRole(r.slug, cx); cx.notify(); },
        }, c));
      }
      if (roleUse === "any") {
        roleRow.child(this.chip("ro-all", "* everyone", {
          on: this.sel.role === "*", tip: "message every role",
          onClick: (_e, cx) => { this.selectRole("*", cx); cx.notify(); },
        }, c));
      }
      if (!cur.roles.length) roleRow.child(div().text_size(11.5).text_color(c.dim).child("no roles with a session"));
    } else roleRow.child(div().text_size(11.5).text_color(c.dim).child("pick a shoal"));
    col.child(roleRow);
    return col;
  }

  verbRow(c) {
    // one labeled row per group (Talk, Work, Seat, Shoal, Inspect): every verb one click away, in an order that says what it is for
    const col = v_flex().px(16).pb(12).gap(5).flex_none();
    for (const g of verbGroups(this.sel)) {
      const row = h_flex().id(`g-${g.key}`).gap(6).items_start();
      row.child(div().w(54).flex_none().pt(6).text_size(10.5).text_color(c.dim).child(g.label));
      const chips = h_flex().flex_1().min_w(0).gap(6).flex_wrap();
      for (const v of g.verbs) {
        const on = v.verb === this.verb;
        chips.child(h_flex().id(`v-${v.verb}`).h(26).px(10).items_center().rounded(7).flex_none().cursor_pointer()
          .border_1().border_color(on ? c.accent : c.input)
          .bg(on ? c.accent : v.kind === "write" ? c.raised : c.panel)
          .text_size(12).font_medium().text_color(on ? c.accentFg : c.ink)
          .hover((s) => s.opacity(0.88))
          .tooltip(v.about)
          .on_click((_e, cx) => { this.pick(v.verb, cx); cx.notify(); })
          .child(v.label));
      }
      col.child(row.child(chips));
    }
    return col;
  }

  label(text, c) {
    return div().text_size(11).text_color(c.dim).child(text);
  }

  mono(id, text, c, { color = null, maxLines = 1500 } = {}) {
    const box = v_flex().id(id).p(10).rounded(7).bg(c.code).border_1().border_color(c.border)
      .font_family(MONO).text_size(11.5).text_color(color ?? c.ink).gap(1);
    const lines = textLines(text, maxLines);
    lines.forEach((l, i) => box.child(div().id(`${id}-${i}`).child(l.length ? l : " ")));
    if (!lines.length || (lines.length === 1 && !lines[0])) box.child(div().text_color(c.dim).child("(no output)"));
    return box;
  }

  target(c) {
    if (verbInfo(this.verb)?.hostLevel) return this.form?.values?.pick ? `on ${this.form.values.pick.machine ?? "this computer"}` : "any machine";
    if (this.sel.session) return this.sel.session;
    if (this.sel.scope) return scopeLabel(this.sel.scope);
    const role = roleFor(this.verb, this.sel.role);
    const where = this.sel.shoal ? shortShoal(this.sel.shoal) : "no shoal";
    return `${where}${role ? ` / ${role}` : ""}`;
  }

  body(c) {
    switch (this.screen) {
      case "form": return this.formBody(c);
      case "busy": return this.busyBody(c);
      case "plan": return this.planBody(c);
      case "result": return this.resultBody(c);
      case "view": return this.viewBody(c);
      case "queue": return this.queueBody(c);
      case "error": return this.errorBody(c);
      default: return this.menuBody(c);
    }
  }

  menuBody(c) {
    const col = v_flex().gap(2);
    col.child(div().pb(6).text_size(11.5).text_color(c.muted).child("Pick an action. Permanent or wide ones preview first; on the rest, tick Preview first to see a dry run."));
    for (const v of verbGroups(this.sel).flatMap((g) => g.verbs)) {
      col.child(h_flex().id(`m-${v.verb}`).gap(10).px(8).py(5).rounded(6).cursor_pointer().items_center()
        .hover((s) => s.bg(c.hover))
        .on_click((_e, cx) => { this.pick(v.verb, cx); cx.notify(); })
        .child(div().w(110).flex_none().font_medium().text_color(c.ink).child(v.label))
        .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.dim).child(v.about))
        .child(div().text_size(10.5).text_color(v.kind === "write" ? c.amber : c.dim).child(v.kind === "write" ? "write" : "view")));
    }
    return col;
  }

  formBody(c) {
    const info = verbInfo(this.verb);
    const col = v_flex().gap(10);
    col.child(h_flex().gap(8).items_center()
      .child(div().text_size(13.5).font_semibold().child(info.label))
      .child(div().text_size(11.5).text_color(c.muted).child(this.target(c))));
    const roleDoc = this.roles?.shoals?.find((s) => s.id === this.sel.shoal)?.roles?.find((r) => r.slug === this.sel.role)?.doc ?? null;
    const docNote = handoffDocNote(this.form, roleDoc);
    if (docNote) col.child(div().text_size(11.5).text_color(c.muted).child(docNote));
    for (const f of visibleFields(this.form)) {
      const v = this.form.values[f.key];
      if (f.kind === "text") {
        const focusedNow = this.focusKey === f.key;
        col.child(v_flex().gap(5)
          .child(this.label(f.label + (f.required ? "" : " (optional)"), c))
          .child(Input.new(this.inputs[f.key]).h(32).px(10).rounded(7).border_1()
            .border_color(focusedNow ? c.accent : c.input).bg(c.code).text_size(12.5).text_color(c.ink))
          .when(Boolean(f.hint), (el) => el.child(div().text_size(11).text_color(c.dim).child(f.hint))));
      } else if (f.kind === "toggle") {
        col.child(h_flex().id(`f-${f.key}`).gap(10).items_center().cursor_pointer().py(2)
          .on_click((_e, cx) => { this.form = setValue(this.form, f.key, !v); cx.notify(); })
          .child(div().w(30).h(17).flex_none().rounded(9).p(2).bg(v ? c.accent : c.input)
            .child(div().w(13).h(13).rounded(7).bg(v ? c.accentFg : c.muted).ml(v ? 13 : 0)))
          .child(v_flex().gap(1)
            .child(div().text_color(c.ink).child(f.label))
            .when(Boolean(f.hint), (el) => el.child(div().text_size(11).text_color(c.dim).child(f.hint)))));
      } else if (f.kind === "sessions") {
        const list = v_flex().id("f-sessions").gap(2).max_h(196).overflow_y_scrollbar();
        // the seat name filters the list as it is typed; the picked one stays in it
        const shown = filterSessions(this.sessions, this.form.values.label, v);
        if (!this.sessions) list.child(div().px(8).text_size(11.5).text_color(c.dim).child("reading the sessions on every machine…"));
        else if (!this.sessions.length) list.child(div().px(8).text_size(11.5).text_color(c.dim).child("no standalone sessions found (roles are left out)"));
        else if (!shown.length) list.child(div().px(8).text_size(11.5).text_color(c.dim).child(`no session matches "${str(this.form.values.label).trim()}": clear the name to see all ${this.sessions.length}`));
        for (const s of shown) {
          const on = v?.key === s.key;
          list.child(h_flex().id(`s-${s.key}`).gap(8).px(8).py(4).rounded(6).cursor_pointer().items_center().flex_none()
            .bg(on ? withAlpha(c.accent, 0.2) : c.panel).hover((st) => st.bg(on ? withAlpha(c.accent, 0.26) : c.hover))
            .on_click((_e, cx) => { this.chooseSession(s); cx.notify(); })
            .child(div().w(62).flex_none().text_size(11).text_color(c.muted).child(s.harness))
            .child(div().w(168).flex_none().truncate().text_color(c.ink).child(s.label))
            .child(div().w(72).flex_none().truncate().text_size(11).text_color(c.dim).child(s.state || "?"))
            .child(div().w(92).flex_none().truncate().text_size(11).text_color(c.dim).child(s.machine ?? "this computer"))
            .child(div().flex_1().min_w(0).truncate().text_size(11).text_color(c.dim).child(s.cwd)));
        }
        const count = this.sessions && shown.length !== this.sessions.length ? ` · ${shown.length} of ${this.sessions.length} match the name` : "";
        col.child(v_flex().gap(5).child(this.label(f.label + count, c)).child(list));
        if (this.sessionsNote) col.child(div().text_size(11).text_color(c.amber).child(this.sessionsNote));
      } else if (f.kind === "choice") {
        const row = h_flex().gap(6).items_center();
        // a provider set aside on this host (`shoal ui set ecosystems`) is not offered
        for (const o of f.key === "ecosystem" ? providerOptions(f.options, this.roles) : f.options) {
          row.child(this.chip(`f-${f.key}-${o}`, o, {
            on: v === o,
            // an optional choice clicks off again
            onClick: (_e, cx) => {
              this.form = setValue(this.form, f.key, v === o && !f.required ? null : o);
              this.inputs.note.set_value(String(this.form.values.note ?? ""));
              this.hint = null;
              cx.notify();
            },
          }, c));
        }
        col.child(v_flex().gap(5).child(this.label(f.label, c)).child(row));
      }
    }
    if (this.hint) col.child(div().text_size(11.5).text_color(c.red).child(this.hint));
    return col;
  }

  busyBody(c) {
    const head = h_flex().gap(8).items_center().py(8)
      .child(div().w(14).text_color(c.accent).frames(["◐", "◓", "◑", "◒"], 160))
      .child(div().text_color(c.muted).child(`${this.busyWhat}…`));
    if (!this.applying || !this.live) return head;
    if (this.verb === "handoff") return v_flex().gap(6).child(head)
      .child(this.label("You can close this dialog. Handoff continues; reopen Handoff to follow its log.", c))
      .child(this.mono("live", lastLines(this.live), c));
    return v_flex().gap(6).child(head).child(this.mono("live", lastLines(this.live), c));
  }

  planBody(c) {
    const p = this.plan;
    const col = v_flex().gap(8);
    col.child(h_flex().gap(8).items_center()
      .child(div().text_size(13.5).font_semibold().child(`${verbInfo(this.verb).label}: preview`))
      .child(div().text_size(11.5).text_color(c.muted).child(this.target(c))));
    if (p.summary?.length) {
      col.child(this.label("what it does", c));
      const list = v_flex().gap(2);
      p.summary.forEach((x, i) => list.child(div().id(`sum-${i}`).text_size(12).text_color(c.ink).child(`• ${x}`)));
      col.child(list);
    }
    col.child(this.label("command", c));
    col.child(this.mono("cmd", commandLines(p.command), c));
    if (p.cost) {
      col.child(h_flex().gap(8).items_start().px(10).py(7).rounded(7).bg(withAlpha(c.amber, 0.12)).border_1().border_color(withAlpha(c.amber, 0.5))
        .child(div().text_color(c.amber).child("⚠"))
        .child(div().flex_1().min_w(0).text_color(c.amber).child(p.cost)));
    }
    col.child(this.label("dry run", c));
    col.child(this.mono("preview", p.preview, c, { color: c.muted }));
    if (this.hint) col.child(div().text_size(11.5).text_color(c.red).child(this.hint));
    return col;
  }

  resultBody(c) {
    const r = this.result;
    const col = v_flex().gap(8);
    col.child(h_flex().gap(8).items_center()
      .child(div().text_size(14).font_bold().text_color(r.ok ? c.green : c.red).child(r.ok ? "✓" : "✕"))
      .child(div().text_size(13.5).font_semibold().child(`${verbInfo(this.verb)?.label ?? "Action"}: ${resultLine(r)}`))
      .child(div().text_size(11.5).text_color(c.muted).child(this.target(c))));
    if (r.open) col.child(div().text_size(12).text_color(r.open === "its space is open" ? c.green : c.muted).child(r.open));
    col.child(this.label("command", c));
    col.child(this.mono("res-cmd", commandLines(r.command || r.plan?.command || ""), c));
    if (r.plan?.cost) col.child(div().text_size(11.5).text_color(c.amber).child(`⚠ ${r.plan.cost}`));
    col.child(this.label("output", c));
    col.child(this.mono("res-out", r.output, c, { color: r.ok ? c.ink : c.red }));
    if (r.auto && r.plan?.preview) {
      col.child(this.label("dry run", c));
      col.child(this.mono("res-preview", r.plan.preview, c, { color: c.muted }));
    }
    return col;
  }

  viewBody(c) {
    const v = this.view;
    const col = v_flex().gap(8);
    col.child(h_flex().gap(8).items_center()
      .child(div().text_size(13.5).font_semibold().child(verbInfo(this.verb).label))
      .child(div().text_size(11.5).text_color(c.muted).child(this.target(c)))
      .child(div().flex_1())
      .child(h_flex().id("view-again").px(8).h(22).items_center().rounded(5).cursor_pointer().text_size(11).text_color(c.muted)
        .border_1().border_color(c.input).hover((s) => s.bg(c.hover)).on_click((_e, cx) => { this.runView(cx); cx.notify(); }).child("read again")));
    if (Array.isArray(v.asks)) {
      if (!v.asks.length) col.child(div().text_color(c.dim).child("no open asks and no role waiting on a question"));
      v.asks.forEach((a, i) => {
        const why = a.question ? "blocked on a question in its session: attach to its card to answer there, or answer by mail" : a.saysWaiting ? "says it waits on you, with no prompt: answer by mail with a wake" : "";
        const card = v_flex().id(`ask-${i}`).gap(4).p(10).rounded(8).bg(c.panel).border_1().border_color(withAlpha(c.blocked, 0.45))
          .child(h_flex().gap(8).items_center()
            .child(div().w(8).h(8).rounded(4).flex_none().bg(c.blocked))
            .child(div().font_semibold().child(a.role))
            .child(div().text_size(11).text_color(c.dim).child(a.host ? `on ${a.host}` : ""))
            .child(div().flex_1())
            .child(h_flex().id(`answer-${i}`).h(24).px(10).items_center().rounded(6).cursor_pointer().bg(c.solid).text_color(c.solidFg)
              .text_size(11.5).font_medium().hover((s) => s.opacity(0.88)).tooltip(`Message ${a.role}, wake on`)
              .on_click((_e, cx) => { this.answer(a, cx); cx.notify(); }).child("Answer")));
        if (why) card.child(div().text_size(11).text_color(c.dim).child(why));
        a.asks.forEach((t, j) => {
          // each ask: its text in full, a reply naming it, and the issues or PRs it names
          const row = h_flex().id(`ask-${i}-${j}`).gap(8).items_start()
            .child(div().flex_1().min_w(0).text_size(12).text_color(c.ink).child(`ask ${j + 1}: ${t}`))
            .child(h_flex().id(`reply-${i}-${j}`).flex_none().h(22).px(8).items_center().rounded(5).cursor_pointer().border_1().border_color(c.input)
              .text_size(11).text_color(c.ink).hover((s) => s.bg(c.hover)).tooltip(`Message ${a.role} "re ask ${j + 1}:", wake on`)
              .on_click((_e, cx) => { this.answer(a, cx, j + 1); cx.notify(); }).child("Reply"));
          for (const [k, l] of (a.links?.[j] ?? []).entries()) {
            row.child(Link.new(`open-${i}-${j}-${k}`).href(l.url).flex_none().text_size(11).text_color(c.accent).child(`${l.text} ↗`));
          }
          card.child(row);
        });
        col.child(card);
      });
      if (v.text && v.text.trim()) col.child(this.mono("view-text", v.text, c));
    } else col.child(this.mono("view-text", v.text, c));
    return col;
  }

  errorBody(c) {
    return v_flex().gap(8)
      .child(h_flex().gap(8).items_center()
        .child(div().text_size(14).font_bold().text_color(c.red).child("✕"))
        .child(div().text_size(13.5).font_semibold().child(`${verbInfo(this.verb)?.label ?? "Action"}: not done`))
        .child(div().text_size(11.5).text_color(c.muted).child(this.target(c))))
      .child(div().px(10).py(8).rounded(7).bg(withAlpha(c.red, 0.1)).border_1().border_color(withAlpha(c.red, 0.45)).text_color(c.red).child(this.error ?? "unknown error"))
      .child(div().text_size(11).text_color(c.dim).child("Nothing was written."));
  }

  button(id, label, kind, onClick, c, tip = null) {
    const primary = kind === "primary";
    const warn = kind === "warn";
    const bg = primary ? c.solid : warn ? c.amber : c.raised;
    const fg = primary ? c.solidFg : warn ? "#1a1200" : c.ink;
    return h_flex().id(id).h(30).px(14).items_center().rounded(8).flex_none().cursor_pointer()
      .border_1().border_color(primary || warn ? bg : c.input).bg(bg).text_color(fg).text_size(12.5).font_medium()
      .hover((s) => s.opacity(0.88)).when(Boolean(tip), (el) => el.tooltip(tip)).on_click(onClick).child(label);
  }

  footer(c) {
    const row = h_flex().flex_none().px(16).py(10).gap(8).items_center().border_t_1().border_color(c.border);
    const keys = { queue: "pick a row or candidate, then a slot · Esc: back", menu: "click an action · Esc closes", form: `${runsAcross(this.sel, this.form) ? "" : `Enter: ${this.form?.values?.dryRun ? "preview" : "run"} · `}Tab: next field · Esc: back`, plan: "Enter: confirm · Esc: cancel", busy: "Esc: back", result: "Esc: back to the form", view: "Esc: back", error: "Esc: back" };
    row.child(div().flex_1().min_w(0).truncate().text_size(10.5).text_color(c.dim).child(keys[this.screen] ?? ""));
    const go = (f) => (_e, cx) => { f(cx); cx.notify(); };
    switch (this.screen) {
      case "form":
        row.child(this.button("b-cancel", "Cancel", "plain", go((cx) => this.back(cx)), c));
        if (this.form?.values?.dryRun) row.child(this.button("b-preview", "Preview", "primary", go((cx) => this.preview(cx)), c, "plan it: the CLI's --dry-run, nothing written"));
        else row.child(this.button("b-run", runLabel(this.sel, this.verb), "primary", go((cx) => this.preview(cx)), c, "run it now; tick Preview first to see a dry run"));
        break;
      case "plan":
        row.child(this.button("b-cancel", "Cancel", "plain", go((cx) => this.back(cx)), c, "discard the plan; nothing written"));
        row.child(this.button("b-confirm", "Confirm", this.plan?.cost ? "warn" : "primary", go((cx) => this.apply(cx)), c, this.plan?.cost || "apply it"));
        break;
      case "result":
        row.child(this.button("b-again", "Another", "plain", go((cx) => this.back(cx)), c, "back to the form"));
        row.child(this.button("b-close", "Close", "primary", () => this.close(), c));
        break;
      case "busy":
        if (!this.applying) row.child(this.button("b-back", "Back", "plain", go((cx) => this.back(cx)), c));
        else if (this.verb === "handoff" && this.liveFrom?.run) row.child(this.button("b-background", "Continue in background", "plain", () => this.close(), c));
        break;
      case "queue": {
        const changed = this.draft && this.queue && draftChanged(this.draft, this.queue);
        row.child(this.button("b-back", "Back", "plain", go((cx) => this.back(cx)), c));
        if (changed) row.child(this.button("b-reset", "Reset", "plain", go(() => { this.draft = newDraft(this.queue); this.hint = null; }), c, "back to the queue as read"));
        row.child(this.button("b-preview", "Preview", "primary", go((cx) => this.previewQueue(cx)), c, "plan the whole change as one batch: each verb's --dry-run, nothing written"));
        break;
      }
      default:
        if (this.screen !== "menu") row.child(this.button("b-back", "Back", "plain", go((cx) => this.back(cx)), c));
        row.child(this.button("b-close", "Close", "primary", () => this.close(), c));
    }
    return row;
  }
}
