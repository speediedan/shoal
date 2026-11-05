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
//     Without them the dialog starts on the focused card (its `shoal.pane` label and its space's
//     machine), else on the first shoal with a role that needs you.
//   verb: open straight on one verb ("msg", "next", "asks", ...); text, wake: prefill Message.
//   fixture: true answers from the bundled fixtures/act.json instead of running shoal (every
//     screen renders without the CLI; a field value containing "fail" gets the error reply).

import { View, div } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { exec, readTextFile, appHome, props, theme, terminals, focused, spaces, panes, machines, closeOverlay, nextCommand } from "herdr";
import {
  MOD, VERBS, verbInfo, isWrite, isView, FIELDS, newForm, visibleFields, textFields, nextField, setValue, roleFor,
  validate, rolesArgv, planArgv, applyArgv, planExpired, parseReply, textLines, preselect, settleSelection,
  targetMachine, backStep, keyAction, answerTarget, resultLine, fixtureReply, palette, withAlpha, shortShoal,
  verbsFor, scopeLabel, handoffMachine,
} from "./model.js";

const MONO = "Geist Mono";
// Flipped to true only in a scratch copy, to look at every screen with no props (mod.open).
const FIXTURE_DEFAULT = false;
const TEXT_KEYS = ["text", "item", "note", "until", "model", "effort"];

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
    this.prefill = { text: typeof p.text === "string" ? p.text : undefined, wake: p.wake === true ? true : undefined };

    this.inputs = {};
    for (const key of TEXT_KEYS) {
      const st = InputState.new({ placeholder: FIELDS[key].placeholder ?? "" });
      st.on("change", (_e, cx) => {
        if (this.form) this.form = setValue(this.form, key, st.value());
        if (this.hint) this.hint = null;
        cx.notify();
      });
      st.on("submit", (_e, cx) => { this.enter(cx); cx.notify(); });
      st.on("focus", (_e, cx) => { this.focusKey = key; cx.notify(); });
      st.on("blur", (_e, cx) => { if (this.focusKey === key) this.focusKey = null; cx.notify(); });
      this.inputs[key] = st;
    }
    this.handle = cx.focus_handle();
    this.handle.focus();
    cx.timer.after(400, () => { if (!this.focusKey) this.handle.focus(); });
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
        this.form = newForm(verb, new Date(), pre);
        this.syncInputs();
      }
      this.screen = "form";
      this.focusFirst(cx);
    } else {
      this.form = null;
      this.runView(cx);
    }
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
    this.request("planning (dry run)", planArgv(this.verb, this.sel, this.form), machine, "plan", (r, cx) => {
      if (r.kind !== "plan") {
        this.error = r.kind === "error" ? r.error : "the CLI answered with a view, not a plan";
        this.screen = "error";
        return;
      }
      const { token, ...shown } = r;
      this.token = token;
      this.plan = { ...shown, machine, plannedAt: Date.now() };
      if (!r.confirm) this.apply(cx, true); // offline: applied right after planning, still shown
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
    this.request("applying", applyArgv(token), plan.machine, "result", (r) => {
      this.applying = false;
      if (r.kind === "result") {
        this.result = { ...r, auto, plan };
        this.screen = "result";
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
    if (a === "preview") this.preview(cx);
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

  answer(ask, cx) {
    const a = answerTarget(this.sel, ask);
    this.sel = { shoal: a.target.shoal, role: a.target.role };
    this.verb = null;
    this.pick("msg", cx, { wake: true });
  }

  // ---- render

  render(_cx) {
    const c = palette(safe(theme, {}));
    const root = v_flex().id("actions").size_full().min_h(0).bg(c.bg).text_color(c.ink).text_size(12.5)
      .track_focus(this.handle)
      .on_key_down((e, cx) => this.key(e, cx));
    root.child(this.header(c));
    root.child(this.verbRow(c));
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
    const row = h_flex().px(16).pb(12).gap(6).flex_wrap().flex_none();
    for (const v of verbsFor(this.sel)) {
      const on = v.verb === this.verb;
      row.child(h_flex().id(`v-${v.verb}`).h(28).px(11).items_center().rounded(7).flex_none().cursor_pointer()
        .border_1().border_color(on ? c.accent : c.input)
        .bg(on ? c.accent : v.kind === "write" ? c.raised : c.panel)
        .text_size(12).font_medium().text_color(on ? c.accentFg : c.ink)
        .hover((s) => s.opacity(0.88))
        .tooltip(v.about)
        .on_click((_e, cx) => { this.pick(v.verb, cx); cx.notify(); })
        .child(v.label));
    }
    return row;
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
      case "error": return this.errorBody(c);
      default: return this.menuBody(c);
    }
  }

  menuBody(c) {
    const col = v_flex().gap(2);
    col.child(div().pb(6).text_size(11.5).text_color(c.muted).child("Pick an action. Writes show the CLI's dry-run preview and its wake cost before you confirm."));
    for (const v of verbsFor(this.sel)) {
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
    for (const f of visibleFields(this.form)) {
      const v = this.form.values[f.key];
      if (f.kind === "text") {
        const focusedNow = this.focusKey === f.key;
        col.child(v_flex().gap(5)
          .child(this.label(f.label + (f.required ? "" : " (optional)"), c))
          .child(Input.new(this.inputs[f.key]).h(32).px(10).rounded(7).border_1()
            .border_color(focusedNow ? c.accent : c.input).bg(c.code).text_size(12.5).text_color(c.ink)));
      } else if (f.kind === "toggle") {
        col.child(h_flex().id(`f-${f.key}`).gap(10).items_center().cursor_pointer().py(2)
          .on_click((_e, cx) => { this.form = setValue(this.form, f.key, !v); cx.notify(); })
          .child(div().w(30).h(17).flex_none().rounded(9).p(2).bg(v ? c.accent : c.input)
            .child(div().w(13).h(13).rounded(7).bg(v ? c.accentFg : c.muted).ml(v ? 13 : 0)))
          .child(v_flex().gap(1)
            .child(div().text_color(c.ink).child(f.label))
            .when(Boolean(f.hint), (el) => el.child(div().text_size(11).text_color(c.dim).child(f.hint)))));
      } else if (f.kind === "choice") {
        const row = h_flex().gap(6).items_center();
        for (const o of f.options) {
          row.child(this.chip(`f-${f.key}-${o}`, o, {
            on: v === o,
            // an optional choice clicks off again
            onClick: (_e, cx) => { this.form = setValue(this.form, f.key, v === o && !f.required ? null : o); this.hint = null; cx.notify(); },
          }, c));
        }
        col.child(v_flex().gap(5).child(this.label(f.label, c)).child(row));
      }
    }
    if (this.verb === "offline") col.child(div().text_size(11).text_color(c.dim).child("Offline applies right after its preview (no second click); the command and its result still show."));
    if (this.hint) col.child(div().text_size(11.5).text_color(c.red).child(this.hint));
    return col;
  }

  busyBody(c) {
    return h_flex().gap(8).items_center().py(8)
      .child(div().w(14).text_color(c.accent).frames(["◐", "◓", "◑", "◒"], 160))
      .child(div().text_color(c.muted).child(`${this.busyWhat}…`));
  }

  planBody(c) {
    const p = this.plan;
    const col = v_flex().gap(8);
    col.child(h_flex().gap(8).items_center()
      .child(div().text_size(13.5).font_semibold().child(`${verbInfo(this.verb).label}: preview`))
      .child(div().text_size(11.5).text_color(c.muted).child(this.target(c))));
    col.child(this.label("command", c));
    col.child(this.mono("cmd", p.command, c));
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
    if (r.auto) col.child(div().text_size(11).text_color(c.dim).child("applied right after its preview (offline asks no second click)"));
    col.child(this.label("command", c));
    col.child(this.mono("res-cmd", r.command || r.plan?.command || "", c));
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
        a.asks.forEach((t, j) => card.child(div().id(`ask-${i}-${j}`).text_size(12).text_color(c.ink).child(`ask ${j + 1}: ${t}`)));
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
    const keys = { menu: "click an action · Esc closes", form: "Enter: preview · Tab: next field · Esc: back", plan: "Enter: confirm · Esc: cancel", busy: "Esc: back", result: "Esc: back to the form", view: "Esc: back", error: "Esc: back" };
    row.child(div().flex_1().min_w(0).truncate().text_size(10.5).text_color(c.dim).child(keys[this.screen] ?? ""));
    const go = (f) => (_e, cx) => { f(cx); cx.notify(); };
    switch (this.screen) {
      case "form":
        row.child(this.button("b-cancel", "Cancel", "plain", go((cx) => this.back(cx)), c));
        row.child(this.button("b-preview", "Preview", "primary", go((cx) => this.preview(cx)), c, "plan it: the CLI's --dry-run, nothing written"));
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
        break;
      default:
        if (this.screen !== "menu") row.child(this.button("b-back", "Back", "plain", go((cx) => this.back(cx)), c));
        row.child(this.button("b-close", "Close", "primary", () => this.close(), c));
    }
    return row;
  }
}
