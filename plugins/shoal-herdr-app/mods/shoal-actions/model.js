// model.js: the shoal actions dialog's decisions. Pure: no Herdr or GPUI imports, so it runs
// under Node (tests/herdr_app/test_shoal_actions.mjs).
//
// The dialog is a front end over one CLI contract (additive v1; unknown fields are ignored):
//   shoal ui act-plan roles --json [--shoal ID]
//     -> {"v":1,"kind":"shoal-act-roles","shoals":[{"id","tag","roles":[{"slug","host","needs_you","posture"}]}]}
//   shoal ui act-plan <write verb> --shoal ID [...] --json
//     -> {"v":1,"kind":"shoal-act-plan","verb","shoal","role","command","preview","cost","confirm","token","expires_s"}
//   shoal ui act-apply --token T --json
//     -> {"v":1,"kind":"shoal-act-result","rc","command","output"}
//   shoal ui act-plan <view verb> --shoal ID [--role SLUG] --json
//     -> {"v":1,"kind":"shoal-act-view","verb","shoal","role","text", "asks"?: [...]}
//   errors: a non-zero exit with {"v":1,"kind":"shoal-act-error","error"} on stdout.
// Every write is the CLI's apply; this module only builds argv and reads replies.

export const MOD = "shoal-actions";
export const SIZE = { width: 640, height: 540 };

export const KINDS = {
  roles: "shoal-act-roles",
  plan: "shoal-act-plan",
  result: "shoal-act-result",
  view: "shoal-act-view",
  error: "shoal-act-error",
};

/** The `shoal.pane` label shoal puts on every card terminal: `<shoal id>/<role>`. */
export const PANE_LABEL = "shoal.pane";
const NOT_ROLES = new Set(["overview", "ledger"]);

const arr = (v) => (Array.isArray(v) ? v : []);
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const str = (v) => (typeof v === "string" ? v : v == null ? "" : String(v));
const strOrNull = (v) => (typeof v === "string" && v.trim() ? v.trim() : null);

// ---- verbs and their forms

/**
 * The dialog's verbs, in button order. kind: "write" (a form, a plan, a confirm) or "view"
 * (read-only text). role: "one" (needs a role), "any" (a role or `*`), "none" (shoal-level).
 */
export const VERBS = [
  { verb: "msg", label: "Message", kind: "write", role: "any", fields: ["text", "wake"], about: "mail a role, or every role (*)" },
  { verb: "offer", label: "Offer", kind: "write", role: "one", fields: ["item", "note"], about: "offer an item to a role" },
  { verb: "withdraw", label: "Withdraw", kind: "write", role: "none", fields: ["item"], about: "withdraw an open offer" },
  { verb: "next", label: "Next", kind: "view", role: "one", about: "a role's queue, offers and mail (marks nothing read)" },
  { verb: "asks", label: "Asks", kind: "view", role: "none", about: "open asks and roles waiting on a question" },
  { verb: "offline", label: "Offline", kind: "write", role: "none", scoped: true, fields: ["state", "until", "note"], about: "user-offline mode on or off" },
  { verb: "freeze", label: "Freeze", kind: "write", role: "none", scoped: true, fields: ["state", "note", "noWake"], about: "freeze mode on or off" },
  { verb: "handoff", label: "Hand off", kind: "write", role: "one", fields: ["ecosystem", "model", "effort", "force"], about: "restart this role in place from its start doc, optionally on another provider or model" },
  { verb: "remove-role", label: "Remove", kind: "write", role: "one", fields: ["removeForce"], about: "take this role out for good: stop it, remove it from the shoal and its harness's list; the ledger keeps its history" },
  { verb: "remove-session", label: "Remove", kind: "write", role: "none", sessionOnly: true, fields: ["removeForce"], about: "take this standalone session out for good: stop it and remove it from its harness's list" },
  { verb: "status", label: "Status", kind: "view", role: "none", about: "every role's status, with health conditions" },
  { verb: "conditions", label: "Conditions", kind: "view", role: "none", about: "health conditions now" },
  { verb: "upgrade", label: "Upgrade preview", kind: "view", role: "none", about: "what an upgrade would roll out (applying stays a CLI step)" },
];

export const verbInfo = (verb) => VERBS.find((v) => v.verb === verb) ?? null;
export const isWrite = (verb) => verbInfo(verb)?.kind === "write";
export const isView = (verb) => verbInfo(verb)?.kind === "view";

/** The verbs a target can use: a scope across shoals (`all`, `host:<name>`) takes only those that span shoals. */
export function verbsFor(target) {
  if (strOrNull(target?.session)) return VERBS.filter((v) => v.sessionOnly);
  return VERBS.filter((v) => !v.sessionOnly && (!strOrNull(target?.scope) || v.scoped));
}

/** "every shoal" or "shoals on <host>", for the dialog's header. */
export function scopeLabel(scope) {
  const s = str(scope);
  return s === "all" ? "every shoal" : s.startsWith("host:") ? `shoals on ${s.slice(5)}` : "";
}

/** Field definitions. kind: "text" (an input), "toggle" (a switch), "choice" (on/off chips). */
export const FIELDS = {
  text: { key: "text", kind: "text", label: "Message", placeholder: "what to tell the role", required: true },
  wake: { key: "wake", kind: "toggle", label: "Wake it now", hint: "otherwise the mail is read at its next boundary" },
  item: { key: "item", kind: "text", label: "Item", placeholder: "repo#n:fix or repo#n:review", required: true },
  note: { key: "note", kind: "text", label: "Note", placeholder: "optional" },
  state: { key: "state", kind: "choice", label: "Mode", options: ["on", "off"], required: true },
  until: { key: "until", kind: "text", label: "Back at", placeholder: "ISO time with offset; empty for no time" },
  noWake: { key: "noWake", kind: "toggle", label: "Record without waking every role (--no-wake)", hint: "off: every role wakes once to see the change (tokens)" },
  ecosystem: { key: "ecosystem", kind: "choice", label: "Provider (leave unpicked to keep the current one)", options: ["claude", "codex", "opencode", "muse"] },
  model: { key: "model", kind: "text", label: "Model", placeholder: "empty keeps the recorded model, or the new provider's default" },
  effort: { key: "effort", kind: "text", label: "Effort", placeholder: "empty keeps the recorded effort, or the new provider's default" },
  force: { key: "force", kind: "toggle", label: "Even if it has not said it is restart-ready", hint: "can cut off work in progress" },
  removeForce: { key: "force", kind: "toggle", label: "Even if it is running or holds an active item", hint: "stops it first and can cut off work in progress" },
};

/** The next local `hour`:00 with its UTC offset (2026-11-04T09:00-07:00): the usual offline end. */
export function nextMorning(now = new Date(), hour = 9) {
  const t = new Date(now.getTime());
  t.setHours(hour, 0, 0, 0);
  if (t.getTime() <= now.getTime()) t.setDate(t.getDate() + 1);
  const pad = (n) => String(n).padStart(2, "0");
  const off = -t.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const a = Math.abs(off);
  return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}T${pad(t.getHours())}:${pad(t.getMinutes())}${sign}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
}

/** A fresh form for a write verb, with its defaults (wake off, freeze records without a wake). */
export function newForm(verb, now = new Date(), prefill = {}) {
  const info = verbInfo(verb);
  if (!info || info.kind !== "write") return null;
  const values = { text: "", wake: false, item: "", note: "", state: null, until: "", noWake: true, ecosystem: null, model: "", effort: "", force: false };
  if (verb === "offline") values.until = nextMorning(now);
  for (const [k, v] of Object.entries(obj(prefill) ?? {})) if (k in values) values[k] = v;
  return { verb, values };
}

/** The fields a form shows now: `until` only while offline is being turned on. */
export function visibleFields(form) {
  if (!form) return [];
  const info = verbInfo(form.verb);
  return arr(info?.fields).filter((k) => k !== "until" || form.values.state === "on").map((k) => FIELDS[k]);
}

/** The text inputs of a form in Tab order (keys). */
export const textFields = (form) => visibleFields(form).filter((f) => f.kind === "text").map((f) => f.key);

/** Tab and Shift-Tab between a form's text inputs, wrapping. `at`: the focused key or null. */
export function nextField(form, at, back = false) {
  const keys = textFields(form);
  if (!keys.length) return null;
  const i = keys.indexOf(at);
  if (i < 0) return back ? keys[keys.length - 1] : keys[0];
  return keys[(i + (back ? keys.length - 1 : 1)) % keys.length];
}

/** Sets one value; returns a new form. */
export function setValue(form, key, value) {
  if (!form) return form;
  return { ...form, values: { ...form.values, [key]: value } };
}

/** The role a verb would act on, given the picked role (`*` only where the verb allows it). */
export function roleFor(verb, role) {
  const info = verbInfo(verb);
  if (!info || info.role === "none") return null;
  if (role === "*" && info.role !== "any") return null;
  return strOrNull(role);
}

/** Why a verb cannot run yet for this target and form, or null when it can. */
export function validate(verb, target, form = null) {
  const info = verbInfo(verb);
  if (!info) return `unknown action ${verb}`;
  if (info.sessionOnly) {
    if (!strOrNull(target?.session)) return "no session named";
  } else if (strOrNull(target?.scope)) {
    if (!info.scoped) return `${info.label} works on one shoal: pick one`;
  } else if (!strOrNull(target?.shoal)) return "pick a shoal first";
  if (info.role !== "none" && !roleFor(verb, target?.role)) {
    return info.role === "any" ? "pick a role (or * for everyone)" : "pick a role first (not *)";
  }
  if (info.kind !== "write") return null;
  if (!form || form.verb !== verb) return "the form is missing";
  for (const f of visibleFields(form)) {
    if (!f.required) continue;
    const v = form.values[f.key];
    if (f.kind === "choice" && !f.options.includes(v)) return `pick ${f.options.join(" or ")}`;
    if (f.kind === "text" && !str(v).trim()) return f.key === "text" ? "type a message" : `type the ${f.label.toLowerCase()}`;
  }
  return null;
}

/** argv for the roles list (run as `exec("shoal", argv, {on})`). */
export function rolesArgv(shoal = null) {
  return ["ui", "act-plan", "roles", "--json", ...(strOrNull(shoal) ? ["--shoal", shoal] : [])];
}

/**
 * argv for a verb's plan or view. Free text goes as `--opt=value`, so a value that starts with a
 * dash is never read as an option. Throws when validate() would refuse.
 */
export function planArgv(verb, target, form = null) {
  const why = validate(verb, target, form);
  if (why) throw new Error(why);
  const argv = ["ui", "act-plan", verb, ...(strOrNull(target.session) ? [`--session=${target.session}`] : strOrNull(target.scope) ? ["--scope", target.scope] : ["--shoal", target.shoal])];
  const role = roleFor(verb, target.role);
  if (role) argv.push(`--role=${role}`);
  if (isWrite(verb)) {
    const v = form.values;
    const text = (k) => str(v[k]).trim();
    if (verb === "msg") {
      argv.push(`--text=${str(v.text).trim()}`);
      if (v.wake) argv.push("--wake");
    } else if (verb === "offer") {
      argv.push(`--item=${text("item")}`);
      if (text("note")) argv.push(`--note=${text("note")}`);
    } else if (verb === "withdraw") {
      argv.push(`--item=${text("item")}`);
    } else if (verb === "offline") {
      argv.push("--state", v.state);
      if (v.state === "on" && text("until")) argv.push(`--until=${text("until")}`);
      if (text("note")) argv.push(`--note=${text("note")}`);
    } else if (verb === "freeze") {
      argv.push("--state", v.state);
      if (text("note")) argv.push(`--note=${text("note")}`);
      if (v.noWake) argv.push("--no-wake");
    } else if (verb === "handoff") {
      if (strOrNull(v.ecosystem)) argv.push(`--ecosystem=${v.ecosystem}`);
      if (text("model")) argv.push(`--model=${text("model")}`);
      if (text("effort")) argv.push(`--effort=${text("effort")}`);
      if (v.force) argv.push("--force");
    } else if ((verb === "remove-role" || verb === "remove-session") && v.force) {
      argv.push("--force");
    }
  }
  argv.push("--json");
  return argv;
}

/** argv to apply a plan. The token stays in memory: it is never shown or logged. */
export function applyArgv(token) {
  if (!strOrNull(token)) throw new Error("no plan token");
  return ["ui", "act-apply", "--token", token, "--json"];
}

/** Has a plan's token run out? `plannedAt` in ms; the CLI checks again on apply. */
export function planExpired(plan, now = Date.now()) {
  if (!plan || typeof plan.expiresS !== "number" || typeof plan.plannedAt !== "number") return false;
  return now >= plan.plannedAt + plan.expiresS * 1000;
}

// ---- replies

const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;
/** Text as the dialog shows it: no terminal escapes, no carriage returns, tabs as spaces. */
export function plainText(s) {
  return str(s).replace(ANSI, "").replace(/\r\n?/g, "\n").replace(/\t/g, "    ").replace(/[\x00-\x08\x0b-\x1f\x7f]/g, "");
}

/** Text as lines for a monospace block, capped (a huge preview stays responsive). */
export function textLines(s, cap = 1500) {
  const lines = plainText(s).replace(/\n+$/, "").split("\n");
  if (lines.length <= cap) return lines;
  return [...lines.slice(0, cap), `... ${lines.length - cap} more lines`];
}

const lastLine = (s) => str(s).split(/\r?\n/).map((l) => plainText(l).trim()).filter(Boolean).pop() ?? "";
const clip = (s, n = 400) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

function parseJson(text) {
  const t = str(text).trim();
  if (!t) return null;
  try {
    return JSON.parse(t);
  } catch (_e) {
    // a stray line before the document (a warning on stdout): try the last line alone
    const last = t.split(/\r?\n/).pop();
    try {
      return JSON.parse(last);
    } catch (_e2) {
      return null;
    }
  }
}

function normRoles(d) {
  const shoals = [];
  const seen = new Set();
  for (const raw of arr(d.shoals)) {
    const s = obj(raw);
    const id = strOrNull(s?.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const roles = [];
    const slugs = new Set();
    for (const r of arr(s.roles)) {
      const o = obj(r);
      const slug = strOrNull(o?.slug);
      if (!slug || slugs.has(slug) || slug === "*") continue;
      slugs.add(slug);
      roles.push({ slug, host: strOrNull(o.host), needsYou: o.needs_you === true, posture: strOrNull(o.posture) });
    }
    shoals.push({ id, tag: strOrNull(s.tag) ?? shortShoal(id), roles, needsYou: roles.filter((r) => r.needsYou).length });
  }
  return { kind: "roles", shoals };
}

function normAsks(list) {
  return arr(list).map(obj).filter((a) => a && strOrNull(a.role)).map((a) => ({
    role: strOrNull(a.role),
    host: strOrNull(a.host),
    asks: arr(a.asks).map(str).filter((x) => x.trim()),
    question: a.question === true,
    saysWaiting: a.says_waiting === true,
  }));
}

/**
 * One CLI reply, from an exec result {code, stdout, stderr} or the Error an exec rejected with,
 * as {kind: "roles" | "plan" | "result" | "view" | "error", ...}. `expect`: the kind the call
 * wants ("roles", "plan", "result", "view"; a plan call may also get a view for a read-only verb);
 * anything else is an error. Unparsable output is an error showing the last stderr line.
 */
export function parseReply(r, expect = null) {
  if (r instanceof Error || !r || typeof r !== "object" || typeof r.code !== "number") {
    return { kind: "error", error: clip(`could not run shoal: ${str(r?.message ?? r) || "no reply"}`) };
  }
  const d = obj(parseJson(r.stdout));
  if (d && d.kind === KINDS.error) {
    return { kind: "error", error: clip(plainText(str(d.error)).trim() || `exit ${r.code}`), code: r.code };
  }
  if (!d) {
    const why = lastLine(r.stderr) || lastLine(r.stdout) || "no output";
    return { kind: "error", error: clip(r.code === 0 ? `unreadable reply: ${why}` : `exit ${r.code}: ${why}`), code: r.code };
  }
  if (typeof d.v === "number" && d.v !== 1) return { kind: "error", error: `reply v${d.v} is not supported (v1)` };
  let out = null;
  if (d.kind === KINDS.roles) out = normRoles(d);
  else if (d.kind === KINDS.plan) {
    out = {
      kind: "plan",
      verb: strOrNull(d.verb),
      shoal: strOrNull(d.shoal),
      role: strOrNull(d.role),
      command: str(d.command),
      preview: str(d.preview),
      cost: plainText(str(d.cost)).trim(),
      confirm: d.confirm !== false,
      token: strOrNull(d.token),
      expiresS: typeof d.expires_s === "number" && d.expires_s > 0 ? d.expires_s : null,
    };
  } else if (d.kind === KINDS.result) {
    const rc = typeof d.rc === "number" ? d.rc : r.code;
    out = { kind: "result", rc, ok: rc === 0, command: str(d.command), output: str(d.output) };
  } else if (d.kind === KINDS.view) {
    out = { kind: "view", verb: strOrNull(d.verb), shoal: strOrNull(d.shoal), role: strOrNull(d.role), text: str(d.text) };
    if (Array.isArray(d.asks)) out.asks = normAsks(d.asks);
  }
  if (!out) return { kind: "error", error: clip(`unexpected reply${d.kind ? ` (${d.kind})` : ""}`), code: r.code };
  if (out.kind !== "result" && r.code !== 0) {
    return { kind: "error", error: clip(`exit ${r.code}: ${lastLine(r.stderr) || "no error text"}`), code: r.code };
  }
  if (expect) {
    const ok = expect === "plan" ? out.kind === "plan" || out.kind === "view" : out.kind === expect;
    if (!ok) return { kind: "error", error: `unexpected reply (${out.kind}, wanted ${expect})`, code: r.code };
  }
  if (out.kind === "plan" && !out.token) return { kind: "error", error: "the plan came without a token: nothing to apply" };
  return out;
}

// ---- where the dialog was opened

/** "alpha-20261101-000000" -> "alpha" (a shoal id's tag when the CLI gives none). */
export const shortShoal = (id) => str(id).replace(/-\d{8}-\d{6}$/, "");

/** A `shoal.pane` label as {shoal, role}: role null for overview and ledger; null for standalone. */
export function parsePaneLabel(label) {
  const s = strOrNull(label);
  if (!s) return null;
  const i = s.indexOf("/");
  if (i <= 0) return null;
  const shoal = s.slice(0, i);
  const role = s.slice(i + 1);
  if (shoal === "standalone") return null;
  return { shoal, role: role && !NOT_ROLES.has(role) ? role : null };
}

/** A terminal's labels object (terminals() rows; tolerant of a missing one). */
const labelsOf = (t) => obj(t?.labels) ?? {};

/**
 * The space a core terminal shows in. Remote terminals' addresses carry a `<machine>/` prefix and
 * their `herdr.space` label holds that machine's local id, which spaces() prefixes the same way.
 */
export function terminalSpace(t, spaces) {
  const local = strOrNull(labelsOf(t)["herdr.space"]);
  if (!local) return null;
  const addr = str(t?.address);
  const slash = addr.lastIndexOf("/");
  const prefix = slash > 0 ? addr.slice(0, slash) : null;
  const list = arr(spaces);
  const ids = prefix ? [`${prefix}/${local}`, local] : [local];
  for (const id of ids) {
    const sp = list.find((s) => s && s.id === id);
    if (sp) return sp;
  }
  return null;
}

const machineOfSpace = (sp) => (sp ? (typeof sp.machine === "string" && sp.machine ? sp.machine : null) : undefined);

/**
 * The machine a shoal's role card lives on: the role's own card, else any card of that shoal
 * (overview, ledger, another role). undefined when no card is open (null is this computer).
 */
export function machineFor(shoal, role, terminals, spaces) {
  if (!strOrNull(shoal)) return undefined;
  const rows = arr(terminals).filter((t) => {
    const p = parsePaneLabel(labelsOf(t)[PANE_LABEL]);
    return p && p.shoal === shoal;
  });
  const own = role ? rows.find((t) => parsePaneLabel(labelsOf(t)[PANE_LABEL]).role === role) : null;
  for (const t of own ? [own, ...rows] : rows) {
    const sp = terminalSpace(t, spaces);
    if (sp) return machineOfSpace(sp);
  }
  return undefined;
}

/**
 * The shoal and role of the focused card: focused().terminal, else the focused pane's terminal
 * (panes()), else the agent an agent block shows. The machine is the focused space's.
 */
export function focusContext(focused, terminals, spaces, panes = []) {
  const f = obj(focused);
  if (!f) return null;
  const paneTerm = f.pane ? obj(arr(panes).find((p) => p && p.pane === f.pane))?.terminal ?? null : null;
  const addr = strOrNull(f.terminal) ?? strOrNull(f.shown) ?? strOrNull(paneTerm) ?? strOrNull(f.agent);
  if (!addr) return null;
  const t = arr(terminals).find((x) => x && x.address === addr);
  const p = parsePaneLabel(labelsOf(t)[PANE_LABEL]);
  if (!p) return null;
  const sp = arr(spaces).find((s) => s && s.id === f.space) ?? terminalSpace(t, spaces);
  return { shoal: p.shoal, role: p.role, machine: machineOfSpace(sp) ?? null, from: "focus" };
}

/** {shoal, role, machine} the dialog was opened with (props), or null. */
export function propsContext(p) {
  const o = obj(p);
  const shoal = strOrNull(o?.shoal);
  const scope = strOrNull(o?.scope);
  const session = strOrNull(o?.session);
  if (!shoal && !scope && !session) return null;
  const machine = typeof o.machine === "string" && o.machine ? o.machine : null;
  return { shoal, role: strOrNull(o.role), scope, session, machine, from: "props", machineGiven: "machine" in o };
}

/**
 * Where a handoff runs: `restart` launches a session where it runs and refuses a role on another host, so it goes to the
 * role's own host when that is a machine here, or to this computer when the role is here; else the usual machine.
 */
export function handoffMachine(role, machineNames, fallback, thisHost = null) {
  const host = strOrNull(role?.host);
  if (!host) return fallback;
  if (thisHost && host === thisHost) return null;
  return arr(machineNames).includes(host) ? host : fallback;
}

/** The first shoal with a needs-you role, and that role; else the first shoal. */
export function firstNeedsYou(roles) {
  const shoals = arr(roles?.shoals);
  for (const s of shoals) {
    const r = s.roles.find((x) => x.needsYou);
    if (r) return { shoal: s.id, role: r.slug, from: "needs-you" };
  }
  return shoals.length ? { shoal: shoals[0].id, role: null, from: "first" } : null;
}

/**
 * The starting selection: props, else the focused card, else (once the roles list is in) the
 * first needs-you role. Pure; `env` = {props, focused, terminals, spaces, panes}.
 */
export function preselect(env) {
  return propsContext(env?.props) ?? focusContext(env?.focused, env?.terminals, env?.spaces, env?.panes) ?? null;
}

/**
 * The selection checked against the roles list: an unknown shoal falls back to the first
 * needs-you role (or the first shoal); an unknown role is dropped. `*` is kept (msg uses it).
 */
export function settleSelection(sel, roles) {
  const shoals = arr(roles?.shoals);
  if (!shoals.length) return sel ? { ...sel } : { shoal: null, role: null };
  const s = sel?.shoal ? shoals.find((x) => x.id === sel.shoal) : null;
  if (!s) {
    const f = firstNeedsYou(roles);
    return { ...(sel ?? {}), shoal: f.shoal, role: f.role, from: f.from };
  }
  const role = sel.role === "*" || s.roles.some((r) => r.slug === sel.role) ? sel.role : null;
  return { ...sel, role };
}

/** The machine to run a verb on: the role's card, else the shoal's cards, else where the dialog started. */
export function targetMachine(target, terminals, spaces, fallback = null) {
  const role = target?.role && target.role !== "*" ? target.role : null;
  const m = machineFor(target?.shoal, role, terminals, spaces);
  return m === undefined ? fallback ?? null : m;
}

// ---- screens and keys

/**
 * Screens: "menu" (verb buttons only), "form" (a write verb's fields), "busy" (a call in flight),
 * "plan" (preview, cost, Confirm), "result" (rc and output), "view" (read-only text), "error".
 * Esc goes back one step, and closes from the menu.
 */
export function backStep(screen, verb) {
  switch (screen) {
    case "menu": return "close";
    case "plan": return "form";
    case "error": return isWrite(verb) ? "form" : "menu";
    case "result": return isWrite(verb) ? "form" : "menu";
    case "busy": return isWrite(verb) ? "form" : "menu";
    default: return "menu";
  }
}

/** What a key does on a screen: "close", "back", "preview", "confirm", "next-field", "prev-field" or null. */
export function keyAction(keystroke, screen, opts = {}) {
  const k = str(keystroke);
  if (k === "escape") return screen === "menu" ? "close" : "back";
  if (k === "enter") {
    if (screen === "form") return "preview";
    if (screen === "plan" && opts.canConfirm) return "confirm";
    return null;
  }
  if (screen === "form" && (k === "tab" || k === "shift-tab")) return k === "tab" ? "next-field" : "prev-field";
  return null;
}

/** What the asks view's Answer does: Message to that role, wake on, the text box empty. */
export function answerTarget(target, ask, now = new Date()) {
  return {
    target: { ...target, role: ask.role },
    form: newForm("msg", now, { wake: true }),
  };
}

/** Short words for the result line. */
export function resultLine(res) {
  if (!res) return "";
  return res.ok ? "done (rc 0)" : `failed (rc ${res.rc})`;
}

// ---- fixture mode

/**
 * An answer from the bundled fixtures for one CLI argv, in the CLI's own reply shape
 * ({code, stdout, stderr}), so fixture mode exercises parseReply too. A plan's command is rebuilt
 * from the argv (the fixture keeps its preview); a field value containing "fail" gets the error
 * fixture. `last`: the last plan shown (its command goes on the result).
 */
export function fixtureReply(fx, argv, last = null) {
  const a = arr(argv).map(str);
  const reply = (doc, code = 0, stderr = "") => ({ code, stdout: JSON.stringify(doc), stderr });
  const err = (doc) => reply(doc ?? { v: 1, kind: KINDS.error, error: "fixture: error" }, 2, "shoal: error");
  if (a[0] !== "ui") return err();
  if (a[1] === "act-apply") {
    const doc = { ...(fx?.result ?? {}) };
    if (last?.command) doc.command = last.command;
    return reply(doc);
  }
  if (a[1] !== "act-plan") return err();
  const verb = a[2];
  if (verb === "roles") return reply(fx?.roles ?? { v: 1, kind: KINDS.roles, shoals: [] });
  const opt = (name) => {
    const eq = a.find((x) => x.startsWith(`--${name}=`));
    if (eq) return eq.slice(name.length + 3);
    const i = a.indexOf(`--${name}`);
    return i >= 0 && i + 1 < a.length && !a[i + 1].startsWith("--") ? a[i + 1] : null;
  };
  if (a.some((x) => /^--(text|item|note|until)=.*fail/i.test(x))) return err(fx?.error);
  const shoal = opt("shoal");
  const role = opt("role");
  if (fx?.views?.[verb]) {
    return reply({ ...fx.views[verb], shoal, role });
  }
  const plan = fx?.plans?.[verb];
  if (!plan) return err({ v: 1, kind: KINDS.error, error: `fixture: no ${verb} plan` });
  const shell = (s) => (/^[\w@%+=:,./*#-]+$/.test(s) ? s : `'${s.replace(/'/g, "'\\''")}'`);
  const rest = a.slice(3).filter((x) => x !== "--json");
  const words = [];
  for (let i = 0; i < rest.length; i++) {
    const x = rest[i];
    if (x === "--role" || x.startsWith("--role=")) {
      words.push("--to", x.includes("=") ? x.slice(7) : rest[++i]);
    } else if (x === "--state") {
      words.unshift(rest[++i]);
    } else words.push(x);
  }
  const command = ["shoal", verb, ...words].map(shell).join(" ");
  let cost = str(plan.cost);
  if (verb === "msg" && !a.includes("--wake")) cost = "";
  if (verb === "freeze" && a.includes("--no-wake")) cost = "";
  return reply({ ...plan, verb, shoal, role, command, cost });
}

// ---- look

/** The dialog's colors from theme(), with fallbacks so a partial theme still reads. */
export function palette(t = {}) {
  const c = obj(t) ?? {};
  const pick = (...names) => names.map((n) => c[n]).find((v) => typeof v === "string" && v) ?? null;
  return {
    bg: pick("popover", "surface", "background") ?? "#101418",
    panel: pick("surface", "raised", "popover") ?? "#161b21",
    raised: pick("raised", "surface") ?? "#1c232b",
    hover: pick("hover", "raised") ?? "#232c35",
    selected: pick("selected", "hover") ?? "#26323d",
    code: pick("background", "canvas") ?? "#0b0e12",
    border: pick("border") ?? "#2a333c",
    input: pick("input", "border") ?? "#3a4652",
    ink: pick("foreground") ?? "#d4d4d4",
    muted: pick("muted", "foreground") ?? "#a0a0a0",
    dim: pick("dim", "muted") ?? "#707070",
    accent: pick("accent") ?? "#4ea3ff",
    accentFg: pick("accent_foreground", "background") ?? "#0b0e12",
    solid: pick("solid", "accent") ?? "#d4d4d4",
    solidFg: pick("solid_foreground", "background") ?? "#0b0e12",
    blocked: pick("blocked", "ansi9", "ansi1") ?? "#ff4f9a",
    working: pick("working", "ansi3") ?? "#ffb547",
    amber: pick("ansi11", "ansi3", "working") ?? "#ffb547",
    red: pick("ansi9", "ansi1", "blocked") ?? "#ff4d5e",
    green: pick("done", "ansi10", "ansi2") ?? "#50d090",
  };
}

/** "#rrggbb" with an alpha (0..1) as "#rrggbbaa". */
export function withAlpha(hex, a) {
  let h = str(hex).replace("#", "");
  if (h.length === 3) h = h.split("").map((x) => x + x).join("");
  const n = Math.round(Math.min(1, Math.max(0, Number(a) || 0)) * 255).toString(16).padStart(2, "0");
  return `#${h.slice(0, 6)}${n}`;
}
