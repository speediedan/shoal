// test_shoal_actions.mjs: the shoal-actions Herdr app mod's pure module against its bundled fixtures.
// Node only, no dependencies: node --test tests/herdr_app/*.mjs (or node this file)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "..", "plugins", "shoal-herdr-app", "mods", "shoal-actions");
const M = await import(join(MOD, "model.js"));
const fx = JSON.parse(readFileSync(join(MOD, "fixtures", "act.json"), "utf8"));
const modJson = JSON.parse(readFileSync(join(MOD, "mod.json"), "utf8"));

const A = "alpha-20261101-000000";
const B = "beta-20261101-000000";
const ok = (doc, code = 0, stderr = "") => ({ code, stdout: JSON.stringify(doc), stderr });
const NOW = new Date(2026, 10, 3, 14, 30, 0); // local time

// terminals(), spaces(), focused() and panes() as the app gives them (synthetic ids)
const spaces = [
  { id: "sp-home", machine: null, name: "alpha" },
  { id: "lane-a/sp-remote", machine: "lane-a", name: "beta" },
  { id: "sp-solo", machine: null, name: "solo" },
];
const terminals = [
  { address: "t1", labels: { "herdr.space": "sp-home", "herdr.tab": "tb1", "shoal.pane": `${A}/overview` } },
  { address: "t2", labels: { "herdr.space": "sp-home", "herdr.tab": "tb2", "shoal.pane": `${A}/core` } },
  { address: "t3", labels: { "herdr.space": "sp-home", "herdr.tab": "tb2", "shoal.pane": `${A}/lane-a` } },
  { address: "lane-a/t7", labels: { "herdr.space": "sp-remote", "herdr.tab": "tb9", "shoal.pane": `${B}/docs` } },
  { address: "lane-a/t8", labels: { "herdr.space": "sp-remote", "herdr.tab": "tb9", "shoal.pane": `${B}/ledger` } },
  { address: "t9", labels: { "herdr.space": "sp-solo", "herdr.tab": "tb5", "shoal.pane": "standalone/<label>" } },
  { address: "t10", labels: { "herdr.space": "sp-home" } },
  { address: "t11" },
];
const panes = [{ pane: "p2", terminal: "t2" }, { pane: "p7", terminal: "lane-a/t7" }, { pane: "pm", terminal: null, mod: "lattice-view" }];

test("mod.json: a focusable dialog overlay that docks nowhere", () => {
  assert.equal(modJson.place, "none");
  assert.equal(modJson.focusable, true);
  assert.equal(modJson.overlay.kind, "dialog");
  assert.equal(modJson.overlay.width, M.SIZE.width);
  assert.equal(modJson.overlay.height, M.SIZE.height);
});

test("verbs: the seventeen buttons in order, each with a kind and a role rule", () => {
  assert.deepEqual(M.verbsFor({ shoal: A }).map((v) => v.label), ["Message", "Offer", "Withdraw", "Next", "Queue", "Asks", "Offline", "Freeze", "Handoff", "Fill", "Dismiss", "Remove", "New role", "Add session", "Status", "Conditions", "Upgrade preview"]);
  assert.deepEqual(M.VERBS.filter((v) => v.kind === "write").map((v) => v.verb), ["msg", "offer", "withdraw", "offline", "freeze", "handoff", "fill", "dismiss", "remove-role", "remove-session", "add-role", "add-session"]);
  assert.deepEqual(M.VERBS.filter((v) => v.role !== "none").map((v) => [v.verb, v.role]), [["msg", "any"], ["offer", "one"], ["next", "one"], ["queue", "one"], ["asks", "opt"], ["handoff", "one"], ["fill", "one"], ["dismiss", "one"], ["remove-role", "one"]]);
  for (const v of M.VERBS) {
    if (v.kind === "write") assert.ok(v.fields.length && v.fields.every((k) => M.FIELDS[k]), v.verb);
    else assert.ok(fx.views[v.verb], `a view fixture for ${v.verb}`);
  }
  for (const v of M.VERBS.filter((x) => x.kind === "write")) assert.ok(fx.plans[v.verb], `a plan fixture for ${v.verb}`);
  assert.equal(M.verbInfo("nope"), null);
  assert.ok(M.isWrite("msg") && !M.isWrite("next") && M.isView("asks") && !M.isView("freeze"));
});

test("forms: defaults, visible fields, Tab order", () => {
  const msg = M.newForm("msg", NOW);
  assert.deepEqual(msg.values, { text: "", wake: false, item: "", note: "", state: null, until: "", noWake: true, ecosystem: null, model: "", effort: "", force: false, pick: null, label: "", slug: "", title: "", doc: "", host: "", fill: null, starter: false, guidance: "", nextSteps: "", grants: "", dryRun: false, undo: false });
  assert.deepEqual(M.visibleFields(msg).map((f) => f.key), ["text", "wake", "dryRun"]);
  assert.equal(M.newForm("next", NOW), null, "a view has no form");
  assert.equal(M.newForm("msg", NOW, { wake: true, bogus: 1 }).values.wake, true);
  assert.ok(!("bogus" in M.newForm("msg", NOW, { bogus: 1 }).values));
  // offline: `until` only while turning it on, prefilled with the next local 09:00
  let off = M.newForm("offline", NOW);
  assert.equal(off.values.until, M.nextMorning(NOW));
  assert.match(off.values.until, /^2026-11-04T09:00[+-]\d\d:\d\d$/);
  assert.deepEqual(M.visibleFields(off).map((f) => f.key), ["state", "note", "dryRun"]);
  off = M.setValue(off, "state", "on");
  assert.deepEqual(M.visibleFields(off).map((f) => f.key), ["state", "until", "note", "dryRun"]);
  assert.match(off.values.note, /^Back \w{3} Nov 4 09:00$/, "turning it on fills the note from the back time");
  assert.equal(M.setValue(off, "note", "my own").values.note, "my own");
  assert.equal(M.setValue(M.setValue(off, "note", "my own"), "until", "2026-11-09T10:00-08:00").values.note, "my own", "a written note stays");
  assert.notEqual(M.setValue(off, "until", "2026-11-09T10:00-08:00").values.note, off.values.note, "the default follows the time");
  assert.equal(M.setValue(off, "until", "").values.note, "", "no time, no default");
  assert.equal(M.setValue(off, "state", "off").values.note, "", "coming back, the default says nothing");
  assert.equal(M.setValue(M.setValue(off, "note", "kept"), "state", "off").values.note, "kept");
  assert.equal(M.newForm("offline", NOW, { state: "on" }).values.note, off.values.note, "a prefilled state fills it too");
  assert.equal(M.backNote("not a time"), "Back not a time");
  for (const v of ["msg", "offer", "withdraw", "offline"]) assert.equal(M.newForm(v, NOW).values.dryRun, false, `${v} runs at once by default`);
  for (const v of ["freeze", "handoff", "remove-role", "remove-session"]) assert.equal(M.newForm(v, NOW).values.dryRun, true, `${v} previews first`);
  assert.equal(M.lastLines("a\nb\nc\n", 2), "b\nc");
  // across shoals with no preview, only the button that names its reach runs it
  const run = M.newForm("offline", NOW);
  assert.equal(M.runsAcross({ scope: "all" }, run), true);
  assert.equal(M.runsAcross({ scope: "all" }, M.setValue(run, "dryRun", true)), false, "Enter gives the preview");
  assert.equal(M.runsAcross({ shoal: A }, run), false);
  assert.equal(M.runLabel({ scope: "all" }), "Run on every shoal");
  assert.equal(M.runLabel({ scope: "host:lane-a" }), "Run on shoals on lane-a");
  assert.equal(M.runLabel({ shoal: A }), "Run");
  assert.equal(M.lastLines("", 2), "");
  assert.deepEqual(M.textFields(off), ["until", "note"]);
  assert.equal(M.nextField(off, null), "until");
  assert.equal(M.nextField(off, "until"), "note");
  assert.equal(M.nextField(off, "note"), "until", "Tab wraps");
  assert.equal(M.nextField(off, "until", true), "note", "Shift-Tab goes back");
  assert.equal(M.nextField(off, null, true), "note");
  assert.equal(M.nextField(M.newForm("freeze", NOW), null), "note");
  assert.equal(M.nextField(null, null), null);
  // freeze records without a wake by default (0.9.3 asked [Y/n])
  assert.equal(M.newForm("freeze", NOW).values.noWake, true);
  // the next morning rolls to tomorrow at or after 09:00, and stays today before it
  assert.match(M.nextMorning(new Date(2026, 10, 3, 8, 59)), /^2026-11-03T09:00/);
  assert.match(M.nextMorning(new Date(2026, 10, 3, 9, 0)), /^2026-11-04T09:00/);
  assert.match(M.nextMorning(new Date(2026, 11, 31, 23, 0)), /^2027-01-01T09:00/);
});

test("validation and argv for every verb", () => {
  const t = { shoal: A, role: "lane-a" };
  const f = (verb, values) => ({ ...M.newForm(verb, NOW), values: { ...M.newForm(verb, NOW).values, dryRun: true, ...values } });
  assert.ok(M.planArgv("msg", t, f("msg", { text: "hi", dryRun: false })).includes("--no-preview"), "no preview asked for");
  assert.ok(!M.planArgv("msg", t, f("msg", { text: "hi" })).includes("--no-preview"));
  // Dismiss runs at once, on one role, and can be undone
  assert.equal(M.newForm("dismiss", NOW).values.dryRun, false);
  assert.ok(M.planArgv("dismiss", t, f("dismiss", { undo: true })).includes("--undo"));
  assert.ok(!M.planArgv("dismiss", t, f("dismiss", {})).includes("--undo"));
  assert.match(M.validate("dismiss", { shoal: A, role: "*" }, f("dismiss", {})), /pick a role/);
  assert.equal(M.validate("msg", t, f("msg", {})), "type a message");
  assert.equal(M.validate("msg", { shoal: A }, f("msg", { text: "hi" })), "pick a role (or * for everyone)");
  assert.equal(M.validate("msg", { role: "core" }, f("msg", { text: "hi" })), "pick a shoal first");
  assert.equal(M.validate("offer", { shoal: A, role: "*" }, f("offer", { item: "r#1:fix" })), "pick a role first (not *)");
  assert.equal(M.validate("offer", t, f("offer", { item: "  " })), "type the item");
  assert.equal(M.validate("offline", { shoal: A }, f("offline", {})), "pick on or off");
  assert.equal(M.validate("next", { shoal: A }), "pick a role first (not *)");
  assert.equal(M.validate("status", { shoal: A }), null);
  assert.equal(M.validate("withdraw", { shoal: A }, M.newForm("offer", NOW)), "the form is missing");
  assert.throws(() => M.planArgv("msg", t, f("msg", {})), /type a message/);

  assert.deepEqual(M.planArgv("msg", t, f("msg", { text: "  -rebase first  " })),
    ["ui", "act-plan", "msg", "--shoal", A, "--role=lane-a", "--text=-rebase first", "--json"], "a leading dash stays a value");
  assert.deepEqual(M.planArgv("msg", { shoal: A, role: "*" }, f("msg", { text: "hi all", wake: true })),
    ["ui", "act-plan", "msg", "--shoal", A, "--role=*", "--text=hi all", "--wake", "--json"]);
  assert.deepEqual(M.planArgv("offer", t, f("offer", { item: "repo-one#104:review", note: "after CI" })),
    ["ui", "act-plan", "offer", "--shoal", A, "--role=lane-a", "--item=repo-one#104:review", "--note=after CI", "--json"]);
  assert.deepEqual(M.planArgv("offer", t, f("offer", { item: "repo-one#104:review" })),
    ["ui", "act-plan", "offer", "--shoal", A, "--role=lane-a", "--item=repo-one#104:review", "--json"], "an empty note is left out");
  assert.deepEqual(M.planArgv("withdraw", t, f("withdraw", { item: "repo-one#104" })),
    ["ui", "act-plan", "withdraw", "--shoal", A, "--item=repo-one#104", "--json"], "withdraw is shoal-wide: no role");
  assert.deepEqual(M.planArgv("offline", t, f("offline", { state: "on", until: "2026-11-04T09:00+00:00", note: "away" })),
    ["ui", "act-plan", "offline", "--shoal", A, "--state", "on", "--until=2026-11-04T09:00+00:00", "--note=away", "--json"]);
  assert.deepEqual(M.planArgv("offline", t, f("offline", { state: "on", until: "" })),
    ["ui", "act-plan", "offline", "--shoal", A, "--state", "on", "--json"], "a cleared until is no time");
  assert.deepEqual(M.planArgv("offline", t, f("offline", { state: "off", until: "2026-11-04T09:00+00:00" })),
    ["ui", "act-plan", "offline", "--shoal", A, "--state", "off", "--json"], "until only with on");
  assert.deepEqual(M.planArgv("freeze", t, f("freeze", { state: "on" })),
    ["ui", "act-plan", "freeze", "--shoal", A, "--state", "on", "--no-wake", "--json"]);
  assert.deepEqual(M.planArgv("freeze", t, f("freeze", { state: "off", noWake: false, note: "resume" })),
    ["ui", "act-plan", "freeze", "--shoal", A, "--state", "off", "--note=resume", "--json"]);
  assert.deepEqual(M.planArgv("next", t), ["ui", "act-plan", "next", "--shoal", A, "--role=lane-a", "--json"]);
  for (const v of ["status", "conditions", "upgrade"]) {
    assert.deepEqual(M.planArgv(v, t), ["ui", "act-plan", v, "--shoal", A, "--json"], `${v} is shoal-wide`);
  }
  assert.deepEqual(M.planArgv("asks", t), ["ui", "act-plan", "asks", "--shoal", A, "--role=lane-a", "--json"], "asks: the picked role's");
  assert.deepEqual(M.planArgv("asks", { ...t, role: null }), ["ui", "act-plan", "asks", "--shoal", A, "--json"], "no role: every role's");
  assert.deepEqual(M.planArgv("asks", { ...t, role: "*" }), ["ui", "act-plan", "asks", "--shoal", A, "--json"]);
  assert.deepEqual(M.rolesArgv(), ["ui", "act-plan", "roles", "--json"]);
  assert.deepEqual(M.rolesArgv(A), ["ui", "act-plan", "roles", "--json", "--shoal", A]);
  assert.deepEqual(M.applyArgv("abc"), ["ui", "act-apply", "--token", "abc", "--json"]);
  assert.throws(() => M.applyArgv(""), /no plan token/);
  assert.equal(M.roleFor("withdraw", "core"), null);
  assert.equal(M.roleFor("msg", "*"), "*");
  assert.equal(M.roleFor("next", "*"), null);
});

test("replies: roles", () => {
  const r = M.parseReply(ok(fx.roles), "roles");
  assert.equal(r.kind, "roles");
  assert.deepEqual(r.shoals.map((s) => [s.id, s.tag, s.needsYou]), [[A, "alpha", 1], [B, "beta", 1]]);
  assert.deepEqual(r.shoals[0].roles[1], { slug: "lane-a", host: "host-a", needsYou: true, status: "blocked:user-ask", doc: null });
  // unknown fields ignored; bad rows, repeats and a `*` slug dropped; a missing tag from the id
  const doc = JSON.parse(JSON.stringify(fx.roles));
  doc.future = 1;
  doc.shoals[0].future = [1];
  doc.shoals[0].roles.push({ slug: "core" }, { slug: "*" }, { nope: 1 }, null);
  doc.shoals.push({ id: A }, { tag: "x" }, { id: "zeta-20261101-000000" });
  const r2 = M.parseReply(ok(doc), "roles");
  assert.deepEqual(r2.shoals.slice(0, 2), r.shoals);
  assert.equal(r2.shoals.length, 3);
  assert.deepEqual(r2.shoals[2], { id: "zeta-20261101-000000", tag: "zeta", roles: [], needsYou: 0 });
});

test("replies: plan, result, view, asks", () => {
  const p = M.parseReply(ok(fx.plans.msg), "plan");
  assert.equal(p.kind, "plan");
  assert.equal(p.verb, "msg");
  assert.equal(p.confirm, true);
  assert.equal(p.token, "0f3c9a7d2b8e4f61");
  assert.equal(p.expiresS, 600);
  assert.match(p.cost, /tokens/);
  assert.match(p.preview, /dry run/);
  const off = M.parseReply(ok(fx.plans.offline), "plan");
  assert.equal(off.confirm, false, "offline applies right after planning");
  assert.equal(M.parseReply(ok({ ...fx.plans.msg, confirm: undefined }), "plan").confirm, true, "confirm defaults to asking");
  assert.equal(M.parseReply(ok({ ...fx.plans.msg, token: "" }), "plan").kind, "error", "a plan without a token is not appliable");
  assert.equal(M.parseReply(ok({ ...fx.plans.msg, expires_s: -1 }), "plan").expiresS, null);
  const res = M.parseReply(ok(fx.result), "result");
  assert.deepEqual([res.kind, res.rc, res.ok], ["result", 0, true]);
  assert.match(res.output, /appended/);
  const bad = M.parseReply(ok({ ...fx.result, rc: 3, output: "refused" }, 3), "result");
  assert.deepEqual([bad.kind, bad.rc, bad.ok], ["result", 3, false], "a failed apply still shows its result");
  assert.equal(M.resultLine(res), "done (rc 0)");
  assert.equal(M.resultLine(bad), "failed (rc 3)");
  assert.equal(M.parseReply(ok({ ...fx.result, rc: undefined }, 4), "result").rc, 4, "rc falls back to the exit code");
  for (const v of ["next", "status", "conditions", "upgrade"]) {
    const r = M.parseReply(ok(fx.views[v]), "view");
    assert.equal(r.kind, "view", v);
    assert.equal(r.verb, v);
    assert.ok(r.text.length > 0);
    assert.ok(!("asks" in r), `${v} has no asks list`);
  }
  const asks = M.parseReply(ok(fx.views.asks), "view");
  assert.deepEqual(asks.asks.map((a) => [a.role, a.host, a.asks.length, a.question, a.saysWaiting]),
    [["lane-a", "host-a", 1, true, false], ["lane-b", "host-b", 2, false, true]]);
  assert.deepEqual(M.parseReply(ok({ ...fx.views.asks, asks: [{ role: "x", asks: ["", "a", 3] }, { asks: ["y"] }, null] }), "view").asks,
    [{ role: "x", host: null, asks: ["a", "3"], question: false, saysWaiting: false, links: [] }]);
  const linked = M.parseReply(ok({ ...fx.views.asks, asks: [{ role: "x", asks: ["see #945"],
    links: [[{ text: "repo#945", url: "https://github.com/acme/repo/issues/945" }, { text: "bad", url: "file:///etc/x" }]] }] }), "view").asks[0];
  assert.deepEqual(linked.links, [[{ text: "repo#945", url: "https://github.com/acme/repo/issues/945" }]], "web links only");
  // a plan call may get a view (a read-only verb); a roles call may not
  assert.equal(M.parseReply(ok(fx.views.status), "plan").kind, "view");
  assert.match(M.parseReply(ok(fx.views.status), "roles").error, /unexpected reply \(view, wanted roles\)/);
  assert.match(M.parseReply(ok(fx.plans.msg), "result").error, /wanted result/);
});

test("replies: errors, junk and transport failures", () => {
  const e = M.parseReply(ok(fx.error, 2, "shoal: error"), "plan");
  assert.deepEqual([e.kind, e.error, e.code], ["error", "no open offer of repo-one#999 in alpha-20261101-000000", 2]);
  assert.equal(M.parseReply(ok(fx.error, 0)).kind, "error", "an error document is an error even at exit 0");
  const junk = M.parseReply({ code: 1, stdout: "Traceback ...", stderr: "line one\nValueError: bad shoal\n\n" }, "plan");
  assert.deepEqual([junk.kind, junk.error], ["error", "exit 1: ValueError: bad shoal"], "the last stderr line");
  assert.equal(M.parseReply({ code: 0, stdout: "", stderr: "" }).error, "unreadable reply: no output");
  assert.equal(M.parseReply({ code: 127, stdout: "", stderr: "\x1b[31mshoal: command not found\x1b[0m" }).error, "exit 127: shoal: command not found");
  assert.match(M.parseReply(new Error("machine unreachable")).error, /could not run shoal: machine unreachable/);
  assert.match(M.parseReply(null).error, /could not run shoal/);
  assert.match(M.parseReply(ok({ v: 2, kind: "shoal-act-plan" })).error, /v2/);
  assert.match(M.parseReply(ok({ v: 1, kind: "shoal-act-future" })).error, /unexpected reply \(shoal-act-future\)/);
  assert.match(M.parseReply(ok(fx.views.next, 1, "boom")).error, /exit 1: boom/, "a view at a non-zero exit is an error");
  // a warning line before the document still parses
  assert.equal(M.parseReply({ code: 0, stdout: `warning: slow ledger\n${JSON.stringify(fx.roles)}`, stderr: "" }, "roles").kind, "roles");
  // a long error is clipped
  assert.ok(M.parseReply(ok({ v: 1, kind: "shoal-act-error", error: "x".repeat(2000) }, 2)).error.length <= 400);
});

test("text: escapes stripped, lines capped", () => {
  assert.equal(M.plainText("\x1b[1mbold\x1b[0m\r\nnext\tcol\x07"), "bold\nnext    col");
  assert.equal(M.plainText("\x1b]8;;https://example.org\x1b\\link\x1b]8;;\x1b\\"), "link");
  assert.deepEqual(M.textLines("a\n  b\n\n"), ["a", "  b"], "leading spaces kept, trailing newlines dropped");
  const many = M.textLines(Array.from({ length: 10 }, (_, i) => `l${i}`).join("\n"), 4);
  assert.deepEqual(many, ["l0", "l1", "l2", "l3", "... 6 more lines"]);
  assert.deepEqual(M.textLines(""), [""]);
});

test("plan expiry", () => {
  const plan = { expiresS: 600, plannedAt: 1_000_000 };
  assert.equal(M.planExpired(plan, 1_000_000 + 599_999), false);
  assert.equal(M.planExpired(plan, 1_000_000 + 600_000), true);
  assert.equal(M.planExpired({ expiresS: null, plannedAt: 0 }, 1e12), false, "no expiry: the CLI decides");
  assert.equal(M.planExpired(null), false);
});

test("pane labels and the machine of a card", () => {
  assert.deepEqual(M.parsePaneLabel(`${A}/core`), { shoal: A, role: "core" });
  assert.deepEqual(M.parsePaneLabel(`${A}/overview`), { shoal: A, role: null });
  assert.deepEqual(M.parsePaneLabel(`${A}/ledger`), { shoal: A, role: null });
  for (const bad of ["standalone/<label>", "", null, "noslash", "/core", 7]) assert.equal(M.parsePaneLabel(bad), null, String(bad));
  assert.equal(M.terminalSpace(terminals[1], spaces).id, "sp-home");
  assert.equal(M.terminalSpace(terminals[3], spaces).id, "lane-a/sp-remote", "a remote terminal's space carries the machine prefix");
  assert.equal(M.terminalSpace(terminals[7], spaces), null);
  assert.equal(M.machineFor(A, "core", terminals, spaces), null, "this computer");
  assert.equal(M.machineFor(B, "docs", terminals, spaces), "lane-a");
  assert.equal(M.machineFor(B, "pool", terminals, spaces), "lane-a", "no card of its own: the shoal's other cards");
  assert.equal(M.machineFor(B, null, terminals, spaces), "lane-a");
  assert.equal(M.machineFor("gamma-20261101-000000", "core", terminals, spaces), undefined, "no card at all");
  assert.equal(M.machineFor(null, "core", terminals, spaces), undefined);
  assert.equal(M.targetMachine({ shoal: B, role: "docs" }, terminals, spaces, "fallback"), "lane-a");
  assert.equal(M.targetMachine({ shoal: B, role: "*" }, terminals, spaces, null), "lane-a", "* uses the shoal's cards");
  assert.equal(M.targetMachine({ shoal: "gamma-20261101-000000" }, terminals, spaces, "lane-b"), "lane-b");
  assert.equal(M.targetMachine({ shoal: "gamma-20261101-000000" }, terminals, spaces), null);
});

test("preselection: props, then the focused card, then the first needs-you role", () => {
  const roles = M.parseReply(ok(fx.roles), "roles");
  // props win, with their machine
  assert.deepEqual(M.preselect({ props: { shoal: B, role: "docs", machine: "lane-a", overlay: "o1" }, focused: { space: "sp-home", terminal: "t2" }, terminals, spaces }),
    { shoal: B, role: "docs", scope: null, session: null, machine: "lane-a", from: "props", machineGiven: true });
  assert.equal(M.preselect({ props: { shoal: A, role: "core", machine: null } }).machine, null);
  // a key opens it with only {overlay}: the focused card
  const env = (f) => ({ props: { overlay: "o1" }, focused: f, terminals, spaces, panes });
  assert.deepEqual(M.preselect(env({ space: "sp-home", tab: "tb2", pane: "p2", terminal: "t2" })), { shoal: A, role: "core", machine: null, from: "focus" });
  assert.deepEqual(M.preselect(env({ space: "lane-a/sp-remote", pane: "p7", terminal: "lane-a/t7" })), { shoal: B, role: "docs", machine: "lane-a", from: "focus" });
  assert.deepEqual(M.preselect(env({ space: "lane-a/sp-remote", pane: null, terminal: "lane-a/t8" })), { shoal: B, role: null, machine: "lane-a", from: "focus" }, "the ledger card: shoal only");
  assert.deepEqual(M.preselect(env({ space: "sp-home", pane: "p2", terminal: null })).role, "core", "terminal null: the focused pane's terminal");
  assert.deepEqual(M.preselect(env({ space: "sp-home", pane: "pm", terminal: null, agent: "t3" })).role, "lane-a", "a mod pane: the agent block's terminal");
  assert.deepEqual(M.preselect(env({ space: "sp-solo", pane: null, terminal: "t9" })),
    { shoal: null, role: null, session: "<label>", machine: null, from: "focus" }, "the focused standalone card selects its saved seat");
  assert.equal(M.preselect(env({ space: "sp-home", terminal: "t10" })), null, "an unlabelled terminal");
  assert.equal(M.preselect(env({ space: "sp-home", terminal: "gone" })), null);
  assert.equal(M.preselect(env(null)), null);
  assert.equal(M.preselect({}), null);
  // then the roles list settles it: the first needs-you role
  assert.deepEqual(M.firstNeedsYou(roles), { shoal: A, role: "lane-a", from: "needs-you" });
  assert.deepEqual(M.settleSelection({ shoal: null, role: null }, roles), { shoal: A, role: "lane-a", from: "needs-you" });
  assert.deepEqual(M.settleSelection(null, roles), { shoal: A, role: "lane-a", from: "needs-you" });
  assert.deepEqual(M.settleSelection({ shoal: B, role: "docs" }, roles), { shoal: B, role: "docs" });
  assert.deepEqual(M.settleSelection({ shoal: B, role: "gone" }, roles), { shoal: B, role: null }, "an unknown role is dropped");
  assert.deepEqual(M.settleSelection({ shoal: B, role: "*" }, roles), { shoal: B, role: "*" });
  assert.equal(M.settleSelection({ shoal: "retired-20250101-000000", role: "core" }, roles).shoal, A, "an unknown shoal falls back");
  const calm = { kind: "roles", shoals: [{ id: B, tag: "beta", roles: [{ slug: "core", needsYou: false }], needsYou: 0 }] };
  assert.deepEqual(M.firstNeedsYou(calm), { shoal: B, role: null, from: "first" });
  assert.equal(M.firstNeedsYou({ shoals: [] }), null);
  assert.deepEqual(M.settleSelection({ shoal: A, role: "core" }, { shoals: [] }), { shoal: A, role: "core" }, "no list yet: kept");
});

test("screens and keys: Esc steps back, Enter previews then confirms, Tab moves", () => {
  assert.equal(M.keyAction("escape", "menu"), "close");
  for (const s of ["form", "plan", "result", "view", "error", "busy"]) assert.equal(M.keyAction("escape", s), "back", s);
  assert.equal(M.keyAction("enter", "form"), "preview");
  assert.equal(M.keyAction("enter", "plan", { canConfirm: true }), "confirm");
  assert.equal(M.keyAction("enter", "plan", { canConfirm: false }), null, "no token, no confirm");
  assert.equal(M.keyAction("enter", "menu"), null);
  assert.equal(M.keyAction("enter", "result"), null, "a result never re-applies");
  assert.equal(M.keyAction("tab", "form"), "next-field");
  assert.equal(M.keyAction("shift-tab", "form"), "prev-field");
  assert.equal(M.keyAction("tab", "menu"), null);
  assert.equal(M.keyAction("x", "form"), null);
  assert.equal(M.backStep("menu", null), "close");
  assert.equal(M.backStep("plan", "msg"), "form");
  assert.equal(M.backStep("result", "msg"), "form");
  assert.equal(M.backStep("error", "msg"), "form");
  assert.equal(M.backStep("error", "next"), "menu");
  assert.equal(M.backStep("view", "asks"), "menu");
  assert.equal(M.backStep("form", "msg"), "menu");
});

test("Answer from the asks view: Message to that role with wake on", () => {
  const a = M.answerTarget({ shoal: A, role: "core" }, { role: "lane-b", asks: ["?"] }, NOW);
  assert.deepEqual(a.target, { shoal: A, role: "lane-b" });
  assert.equal(a.form.verb, "msg");
  assert.equal(a.form.values.wake, true);
  assert.equal(a.form.values.text, "");
});

test("fixture mode answers every call in the CLI's shape", () => {
  const roles = M.parseReply(M.fixtureReply(fx, M.rolesArgv()), "roles");
  assert.equal(roles.shoals.length, 2);
  const t = { shoal: A, role: "lane-a" };
  const msg = { ...M.newForm("msg", NOW), values: { ...M.newForm("msg", NOW).values, text: "use main", wake: true } };
  const p = M.parseReply(M.fixtureReply(fx, M.planArgv("msg", t, msg)), "plan");
  assert.equal(p.kind, "plan");
  assert.equal(p.command, `shoal msg --shoal ${A} --to lane-a '--text=use main' --wake`, "the command follows the form");
  assert.match(p.cost, /wake/);
  assert.equal(p.shoal, A);
  const quiet = { ...msg, values: { ...msg.values, wake: false } };
  assert.equal(M.parseReply(M.fixtureReply(fx, M.planArgv("msg", t, quiet)), "plan").cost, "", "no wake, no cost");
  const fr = { ...M.newForm("freeze", NOW), values: { ...M.newForm("freeze", NOW).values, state: "on" } };
  const fp = M.parseReply(M.fixtureReply(fx, M.planArgv("freeze", t, fr)), "plan");
  assert.equal(fp.command, `shoal freeze on --shoal ${A} --no-wake`);
  assert.equal(fp.cost, "", "--no-wake: no cost");
  const of = { ...M.newForm("offline", NOW), values: { ...M.newForm("offline", NOW).values, state: "on", until: "2026-11-04T09:00+00:00" } };
  const op = M.parseReply(M.fixtureReply(fx, M.planArgv("offline", t, of)), "plan");
  assert.equal(op.confirm, false);
  assert.equal(op.command, `shoal offline on --shoal ${A} --until=2026-11-04T09:00+00:00`);
  const res = M.parseReply(M.fixtureReply(fx, M.applyArgv(p.token ?? "x"), p), "result");
  assert.equal(res.command, p.command, "the result names the planned command");
  for (const v of ["next", "asks", "status", "conditions", "upgrade"]) {
    const r = M.parseReply(M.fixtureReply(fx, M.planArgv(v, t)), "plan");
    assert.equal(r.kind, "view", v);
    assert.equal(r.shoal, A);
  }
  const wd = { ...M.newForm("withdraw", NOW), values: { ...M.newForm("withdraw", NOW).values, item: "fail#1" } };
  const err = M.parseReply(M.fixtureReply(fx, M.planArgv("withdraw", t, wd)), "plan");
  assert.equal(err.kind, "error");
  assert.match(err.error, /no open offer/);
  assert.equal(M.parseReply(M.fixtureReply(fx, ["status"])).kind, "error");
});

test("palette follows theme() and falls back", () => {
  const t = { popover: "#101820", foreground: "#e0e8f0", blocked: "#ff3366", ansi11: "#ffcc00", accent: "#33aaff" };
  const c = M.palette(t);
  assert.equal(c.bg, "#101820");
  assert.equal(c.ink, "#e0e8f0");
  assert.equal(c.blocked, "#ff3366");
  assert.equal(c.amber, "#ffcc00");
  const bare = M.palette(null);
  for (const [k, v] of Object.entries(bare)) assert.match(v, /^#[0-9a-f]{6}$/, k);
  assert.equal(M.withAlpha("#abc", 0.5), "#aabbcc80");
});

test("public gate: fixtures and module hold no local paths or host names", () => {
  const files = ["model.js", "main.js", "mod.json", join("fixtures", "act.json")].map((f) => readFileSync(join(MOD, f), "utf8"));
  for (const text of files) {
    assert.ok(!/\/home\/|\/Users\/|\/mnt\//.test(text), "no absolute home paths");
    assert.ok(!/\u2014/.test(text), "no em dashes");
  }
});

test("scopes: across shoals only the verbs that span them, and no shoal to pick", () => {
  const all = { shoal: null, role: null, scope: "all" };
  assert.deepEqual(M.verbsFor(all).map((v) => v.verb), ["offline", "freeze", "add-session"], "and Add session, a host's");
  assert.deepEqual(M.verbsFor({ session: "some-seat" }).map((v) => v.verb), ["handoff", "dismiss", "remove-session"]);
  assert.equal(M.verbsFor({ shoal: A, role: null }).length, M.VERBS.length - 1, "all but the standalone session's removal");
  const form = M.setValue(M.setValue(M.newForm("offline", NOW), "state", "off"), "dryRun", true);
  assert.equal(M.validate("offline", all, form), null);
  assert.deepEqual(M.planArgv("offline", all, form).slice(0, 7), ["ui", "act-plan", "offline", "--scope", "all", "--state", "off"]);
  assert.deepEqual(M.planArgv("offline", { scope: "host:lane-a" }, form).slice(3, 5), ["--scope", "host:lane-a"]);
  assert.match(M.validate("msg", all, M.newForm("msg", NOW)), /one shoal/);
  assert.equal(M.scopeLabel("all"), "every shoal");
  assert.equal(M.scopeLabel("host:lane-a"), "shoals on lane-a");
  assert.equal(M.scopeLabel(null), "");
  assert.deepEqual(M.propsContext({ scope: "host:lane-a", machine: "lane-a" }), { shoal: null, role: null, scope: "host:lane-a", session: null, machine: "lane-a", from: "props", machineGiven: true });
});

test("remove: a role of a shoal, or a saved session by name, and nothing else on offer", () => {
  const t = { shoal: A, role: "core" };
  const f = M.newForm("remove-role", NOW);
  assert.deepEqual(M.planArgv("remove-role", t, f), ["ui", "act-plan", "remove-role", "--shoal", A, "--role=core", "--json"]);
  assert.deepEqual(M.planArgv("remove-role", t, M.setValue(f, "force", true)), ["ui", "act-plan", "remove-role", "--shoal", A, "--role=core", "--force", "--json"]);
  assert.equal(M.validate("remove-role", { shoal: A, role: "*" }, f), "pick a role first (not *)");
  const seat = { session: "some-seat" };
  assert.deepEqual(M.verbsFor(seat).map((v) => v.verb), ["handoff", "dismiss", "remove-session"]);
  const g = M.newForm("remove-session", NOW);
  assert.deepEqual(M.planArgv("remove-session", seat, g), ["ui", "act-plan", "remove-session", "--session=some-seat", "--json"]);
  assert.equal(M.validate("remove-session", { shoal: A, role: "core" }, g), "no session named");
  assert.ok(!M.verbsFor({ scope: "all" }).some((v) => v.verb.startsWith("remove")), "never across shoals");
  assert.equal(M.propsContext({ session: "some-seat" }).session, "some-seat");
});

test("handoff: a role, an optional provider and model, on the role's own host", () => {
  const t = { shoal: A, role: "core" };
  const f = M.newForm("handoff", NOW);
  assert.equal(M.validate("handoff", { shoal: A, role: "*" }, f), "pick a role first (not *)");
  assert.deepEqual(M.planArgv("handoff", t, f), ["ui", "act-plan", "handoff", "--shoal", A, "--role=core", "--json"]);
  const g = M.setValue(M.setValue(M.setValue(M.setValue(M.setValue(f, "ecosystem", "codex"), "model", " gpt-x "), "effort", "high"), "force", true), "prepare", false);
  assert.deepEqual(M.planArgv("handoff", t, g), ["ui", "act-plan", "handoff", "--shoal", A, "--role=core", "--ecosystem=codex", "--model=gpt-x", "--effort=high", "--no-prepare", "--force", "--json"]);
  assert.deepEqual(M.visibleFields(f).map((x) => x.key), ["ecosystem", "model", "effort", "note", "prepare", "dryRun"]);
  // a restart note rides on the plan; a role's form says which start doc it resumes from
  assert.ok(M.planArgv("handoff", t, M.setValue(f, "note", " look at #12 first ")).includes("--note=look at #12 first"));
  assert.equal(M.handoffDocNote(f, "/example/core_plan_state.md"), "Resumes from its start doc: /example/core_plan_state.md");
  assert.match(M.handoffDocNote(f), /its saved start doc \(its plan_state\.md\)/);
  assert.equal(M.handoffDocNote(M.newForm("offer", NOW)), null);
  assert.ok(!M.visibleFields(f).some((x) => x.key === "throughQuestion"), "a role answers its question first");
  assert.equal(M.handoffMachine({ host: "lane-b" }, ["lane-a", "lane-b"], "lane-a"), "lane-b", "restart refuses another host's role");
  assert.equal(M.handoffMachine({ host: "here" }, ["lane-a"], "lane-a", "here"), null);
  assert.equal(M.handoffMachine({ host: "elsewhere" }, ["lane-a"], "lane-a"), "lane-a", "the CLI's refusal then says where to run it");
  assert.equal(M.handoffMachine(null, ["lane-a"], "lane-a"), "lane-a");
});

test("standalone Handoff: checkpoint by default, a saved start doc, and a resumable background log", () => {
  const target = { session: "example-seat" };
  const f = M.newForm("handoff", NOW, { standalone: true, doc: "/example/plan.md" });
  assert.equal(M.validate("handoff", target, f), null);
  assert.deepEqual(M.planArgv("handoff", target, f), ["ui", "act-plan", "handoff", "--session=example-seat", "--doc=/example/plan.md", "--json"]);
  assert.ok(!M.visibleFields(f).some((x) => x.key === "force"));
  assert.ok(M.visibleFields(f).some((x) => x.key === "prepare"));
  assert.ok(M.planArgv("handoff", target, M.setValue(f, "prepare", false)).includes("--no-prepare"));
  // through an open question: offered only with checkpoint-first, and sent only when chosen
  assert.ok(M.visibleFields(f).some((x) => x.key === "throughQuestion"));
  assert.ok(!M.visibleFields(M.setValue(f, "prepare", false)).some((x) => x.key === "throughQuestion"));
  assert.ok(!M.planArgv("handoff", target, f).includes("--through-question"));
  assert.ok(M.planArgv("handoff", target, M.setValue(f, "throughQuestion", true)).includes("--through-question"));
  assert.ok(!M.planArgv("handoff", target, M.setValue(M.setValue(f, "throughQuestion", true), "prepare", false)).includes("--through-question"));
  assert.match(M.handoffDocNote(f), /^Resumes from \/example\/plan\.md\.$/);
  assert.match(M.handoffDocNote(M.setValue(f, "doc", "")), /seat's saved start doc/);
  assert.deepEqual(M.applyArgv("token", true), ["ui", "act-apply", "--token", "token", "--json", "--background"]);
  assert.deepEqual(M.statusArgv(target), ["ui", "act-status", "--session=example-seat", "--json"]);
  assert.deepEqual(M.statusArgv(target, "run"), ["ui", "act-status", "--run", "run", "--json"]);
  const p = M.parseReply(ok({ ...fx.plans.handoff, progress: "/example/run.log" }), "plan");
  assert.equal(p.progress, "/example/run.log", "the parser retains the progress path");
  const r = M.parseReply(ok({ v: 1, kind: "shoal-act-result", rc: 0, running: true, run: "run", progress: "/example/run.log", output: "waiting for checkpoint" }), "result");
  assert.equal(r.running, true);
  assert.equal(r.run, "run");
  assert.equal(r.progress, "/example/run.log");
});

test("Add session: on any target, a list fed from every machine, saved on the session's own machine", () => {
  const addInfo = M.verbInfo("add-session");
  assert.ok(addInfo && addInfo.hostLevel && addInfo.kind === "write");
  assert.ok(M.verbsFor({ shoal: "a" }).some((v) => v.verb === "add-session"));
  assert.ok(M.verbsFor({ scope: "host:lane-b" }).some((v) => v.verb === "add-session"), "a host's right-click offers it");
  assert.ok(!M.verbsFor({ session: "seat" }).some((v) => v.verb === "add-session"));
  const f = M.newForm("add-session");
  assert.equal(M.validate("add-session", {}, f), "type the seat name (filters the list as you type)");
  assert.equal(M.validate("add-session", {}, M.setValue(f, "label", "x")), "pick a session");
  const merged = M.mergeSessions([
    { machine: null, sessions: [{ harness: "claude", session: "s1", label: "notes", state: "alive", cwd: "/x", job: "j1" }] },
    { machine: "lane-b", sessions: [{ harness: "claude", session: "s1", label: "dup" }, { harness: "opencode", session: "o1", label: "" }, { label: "no id" }] },
    { machine: "lane-c", sessions: [], error: "unreachable" },
  ]);
  assert.deepEqual(merged.rows.map((r) => [r.machine, r.session, r.label]), [[null, "s1", "notes"], ["lane-b", "o1", "o1"]], "first machine wins; no id, no row");
  assert.equal(merged.note, "not read: lane-c: unreachable");
  let g = M.setValue(f, "pick", merged.rows[0]);
  assert.equal(M.validate("add-session", {}, g), "type the seat name (filters the list as you type)");
  g = M.setValue(g, "label", "notes");
  assert.equal(M.validate("add-session", {}, g), null);
  assert.deepEqual(M.planArgv("add-session", { shoal: "ignored" }, g),
    ["ui", "act-plan", "add-session", "--no-preview", "--harness=claude", "--id=s1", "--label=notes", "--cwd=/x", "--job=j1", "--json"]);
  assert.deepEqual(M.sessionsArgv(), ["ui", "act-plan", "sessions", "--json"]);
  assert.deepEqual(M.openSeatArgv("notes"), ["ui", "app-open", "--show", "notes"]);
  assert.deepEqual(M.openSeatArgv("notes", "lane-b"), ["ui", "app-open", "--show", "notes", "--machine", "lane-b"], "a seat saved on lane-b opens from lane-b's plan");
  const view = M.parseReply({ code: 0, stdout: JSON.stringify({ v: 1, kind: "shoal-act-view", verb: "sessions", sessions: [{ harness: "claude", session: "s1" }, 3] }) }, "view");
  assert.equal(view.sessions.length, 1);
  assert.equal(M.fixtureReply({}, M.openSeatArgv("x")).code, 0, "the fixture opens a space without the CLI");
});

test("Add session: the name filters the list as it is typed, the picked one stays", () => {
  const rows = [
    { key: "a", label: "Implementing Zellij", cwd: "/repos/x", harness: "opencode", machine: null },
    { key: "b", label: "shoal-role-lifecycle", cwd: "/repos/shoal", harness: "claude", machine: "lane-b" },
    { key: "c", label: "notes on shoal", cwd: "/tmp", harness: "codex", machine: null },
    { key: "d", label: "triage", cwd: "/srv/shoal-ledgers", harness: "muse", machine: null },
  ];
  assert.equal(M.filterSessions(rows, "").length, 4, "no text: all of them");
  assert.deepEqual(M.filterSessions(rows, "SHOAL").map((r) => r.key), ["b", "c", "d"], "case aside; a label that starts with it first");
  assert.deepEqual(M.filterSessions(rows, "lane-b").map((r) => r.key), ["b"], "the machine too");
  assert.deepEqual(M.filterSessions(rows, "zzz", rows[0]).map((r) => r.key), ["a"], "the picked one stays in view");
  assert.deepEqual(M.verbInfo("add-session").fields, ["label", "pick"], "the name first: it is the filter");
});

test("New role: a new session as a role of the shoal, previewed first", () => {
  const info = M.verbInfo("add-role");
  assert.ok(info && !info.hostLevel && info.role === "none");
  assert.ok(M.verbsFor({ shoal: A }).some((v) => v.verb === "add-role"));
  assert.ok(!M.verbsFor({ scope: "all" }).some((v) => v.verb === "add-role"), "one shoal's: not across shoals");
  let f = M.newForm("add-role");
  assert.equal(f.values.dryRun, true, "it starts a session: preview first");
  assert.match(M.validate("add-role", { shoal: A }, f), /role name/i);
  for (const [k, v] of [["slug", "lifecycle"], ["title", "role lifecycle"], ["doc", "/docs/lifecycle/plan_state.md"]]) f = M.setValue(f, k, v);
  assert.equal(M.validate("add-role", { shoal: A }, f), "pick claude or codex or opencode or muse");
  f = M.setValue(M.setValue(M.setValue(f, "ecosystem", "claude"), "model", "claude-opus-5-5"), "effort", "high");
  f = M.setValue(M.setValue(f, "host", "lane-b"), "fill", "assigned");
  assert.equal(M.validate("add-role", { shoal: A }, f), null);
  assert.deepEqual(M.planArgv("add-role", { shoal: A, role: "core" }, f),
    ["ui", "act-plan", "add-role", "--shoal", A, "--slug=lifecycle", "--title=role lifecycle", "--doc=/docs/lifecycle/plan_state.md",
      "--ecosystem=claude", "--model=claude-opus-5-5", "--effort=high", "--host=lane-b", "--fill=assigned", "--json"]);
  assert.equal(M.validate("handoff", { shoal: A, role: "core" }, M.newForm("handoff")), null, "a handoff's provider stays optional");
});

test("New role with a starter doc: guidance asked only then, and sent with the plan", () => {
  let f = M.newForm("add-role");
  assert.ok(!M.visibleFields(f).some((x) => x.key === "guidance"), "no guidance while the starter doc is off");
  f = M.setValue(f, "starter", true);
  assert.deepEqual(M.visibleFields(f).map((x) => x.key).slice(3, 7), ["starter", "guidance", "nextSteps", "grants"]);
  for (const [k, v] of [["slug", "lifecycle"], ["title", "role lifecycle"], ["doc", "/docs/lifecycle/plan_state.md"], ["ecosystem", "claude"]]) f = M.setValue(f, k, v);
  assert.equal(M.validate("add-role", { shoal: A }, f), "type the guidance (its mission)");
  f = M.setValue(M.setValue(M.setValue(f, "guidance", "own the lifecycle verbs"), "nextSteps", "read the docs; list issues"), "grants", "push (maintainer)");
  const argv = M.planArgv("add-role", { shoal: A }, f);
  assert.deepEqual(argv.slice(-5), ["--starter", "--guidance=own the lifecycle verbs", "--next=read the docs; list issues", "--grants=push (maintainer)", "--json"]);
});

test("Add session's button: one seat on one machine, whatever scope the dialog was opened with", () => {
  const f = M.newForm("add-session");
  assert.equal(M.runsAcross({ scope: "all" }, f), false, "not run across shoals");
  assert.equal(M.runLabel({ scope: "all" }, "add-session"), "Add standalone seat");
  assert.equal(M.runLabel({ scope: "all" }, "offline"), "Run on every shoal", "a verb that spans shoals still says so");
  assert.ok(!M.planArgv("add-session", { scope: "all" }, M.setValue(M.setValue(f, "label", "x"), "pick", { harness: "claude", session: "s1" })).includes("--scope"),
    "and its plan carries no scope: it runs once");
});

test("Reply to one ask: the message names it, wake on", () => {
  const a = M.answerTarget({ shoal: A, role: null }, { role: "lane-b" }, new Date(), 2);
  assert.equal(a.target.role, "lane-b");
  assert.equal(a.form.values.text, "re ask 2: ");
  assert.equal(a.form.values.wake, true);
  assert.equal(M.answerTarget({ shoal: A }, { role: "lane-b" }).form.values.text, "", "the role's Answer: no ask named");
});

test("Fill: a role's work from the board or from offers, previewed with set-fill", () => {
  let f = M.newForm("fill");
  assert.equal(M.validate("fill", { shoal: A, role: "core" }, f), "pick self or assigned");
  f = M.setValue(f, "fill", "self");
  assert.deepEqual(M.planArgv("fill", { shoal: A, role: "core" }, f), ["ui", "act-plan", "fill", "--shoal", A, "--role=core", "--no-preview", "--fill=self", "--json"]);
  assert.match(M.validate("fill", { shoal: A, role: null }, f), /pick a role/);
});

test("Queue: the role's queue and candidates, a draft laid out by pick and slot, planned as one batch", () => {
  const t = { shoal: A, role: "core" };
  assert.ok(M.isQueue("queue") && !M.isWrite("queue") && !M.isView("queue"));
  assert.deepEqual(M.planArgv("queue", t), ["ui", "act-plan", "queue", "--shoal", A, "--role=core", "--json"]);
  assert.match(M.validate("queue", { shoal: A, role: null }), /pick a role/);
  const v = M.parseReply(M.fixtureReply(fx, M.planArgv("queue", t)), "view");
  assert.equal(v.kind, "view");
  const q = v.queue;
  assert.equal(q.fill, "self");
  assert.equal(q.nextMax, 3);
  assert.deepEqual(q.queue.map((r) => [r.item, r.src, r.active]), [["example/repo#11:fix", "held", true], ["example/repo#12:fix", "offer", false], ["example/repo#13:review", "board", false]]);
  assert.equal(q.queue[2].blocked, "lane-a:example/repo#20");
  assert.deepEqual(q.verbs, { transfer: true, queueOrder: true });
  // a draft starts as the queue reads, the active item pinned outside it
  let d = M.newDraft(q);
  assert.deepEqual(d.rows.map((r) => r.item), ["example/repo#12:fix", "example/repo#13:review"]);
  assert.equal(M.draftChanged(d, q), false);
  assert.equal(M.draftProblem(d, q), "no change to the queue yet");
  // candidates by source, another role's last, filtered by text
  assert.deepEqual(M.draftCandidates(d, q).map((r) => r.src), ["offer", "board", "released", "other"]);
  assert.deepEqual(M.draftCandidates(d, q, "lane-a").map((r) => r.item), ["example/repo#20:fix"]);
  assert.deepEqual(M.draftCandidates(d, q, "released").map((r) => r.item), ["example/repo#16:fix"]);
  // pick a candidate, then a slot: it goes there and leaves the candidates
  d = M.draftPick(d, "example/repo#15");
  assert.equal(d.picked, "example/repo#15");
  assert.equal(M.draftPick(d, "example/repo#15").picked, null, "picking again unpicks");
  d = M.draftPlace(d, q, d.picked, 0);
  assert.equal(d.picked, null);
  assert.deepEqual(d.rows.map((r) => r.key), ["example/repo#15", "example/repo#12", "example/repo#13"]);
  assert.ok(!M.draftCandidates(d, q).some((r) => r.key === "example/repo#15"));
  // up and down; a move past the ends changes nothing
  d = M.draftMove(d, "example/repo#13", -1);
  assert.deepEqual(d.rows.map((r) => r.key), ["example/repo#15", "example/repo#13", "example/repo#12"]);
  assert.equal(M.draftMove(d, "example/repo#15", -1), d);
  // a row it had is dropped (Undo puts it back); a placed candidate is removed to the candidates
  d = M.draftRemove(d, q, "example/repo#12");
  assert.deepEqual(d.dropped.map((r) => r.item), ["example/repo#12:fix"]);
  const back = M.draftPlace(d, q, "example/repo#12", 9);
  assert.deepEqual(back.rows.map((r) => r.key).at(-1), "example/repo#12");
  assert.deepEqual(back.dropped, []);
  assert.deepEqual(M.draftRemove(d, q, "example/repo#15").dropped.map((r) => r.item), ["example/repo#12:fix"], "a candidate is not dropped");
  // another role's item: placed like any, counted apart from next_max, sent in the order (the CLI makes it a transfer)
  d = M.draftPlace(d, q, "example/repo#20", 1);
  d = M.draftPlace(d, q, "example/repo#14", 3);
  d = M.draftPlace(d, q, "example/repo#16", 4);
  assert.match(M.draftProblem(d, q), /at most 3 next items/);
  d = M.draftRemove(d, q, "example/repo#16");
  assert.equal(M.draftProblem(d, q), null);
  // mark blocked, and clear it
  d = M.draftBlock(d, "example/repo#13:review", "  waits on lane-a's cache change ");
  assert.equal(d.blocked["example/repo#13:review"], "waits on lane-a's cache change");
  assert.deepEqual(M.draftBlock(d, "example/repo#13:review", "").blocked, {});
  assert.deepEqual(M.queueSetArgv(t, d), ["ui", "act-plan", "queue-set", "--shoal", A, "--role=core",
    "--order=example/repo#15:fix,example/repo#20:fix,example/repo#13:review,example/repo#14:fix", "--drop=example/repo#12:fix",
    "--blocked=example/repo#13:review=waits on lane-a's cache change", "--json"]);
  // the plan: several verbs under one token, with what each does
  const p = M.parseReply(M.fixtureReply(fx, M.queueSetArgv(t, d)), "plan");
  assert.equal(p.kind, "plan");
  assert.equal(p.summary.length, 2);
  assert.match(p.summary[0], /from lane-a/);
  assert.match(p.cost, /wakes core once/);
  // refusals come back by item and mark the rows
  const bad = { ...d, rows: [{ ...d.rows[0], item: "fail#1:fix" }] };
  const e = M.parseReply(M.fixtureReply(fx, M.queueSetArgv(t, bad)), "plan");
  assert.equal(e.kind, "error");
  assert.deepEqual(e.refused, { "example/repo#15:fix": "not in core's scope" });
  assert.equal(M.draftRefused(d, e.refused).refused["example/repo#15"], "not in core's scope");
  // pending transfers, by key; a CLI without the verbs holds the plan back with a reason
  assert.equal(M.pendingTransfers(q).get("example/repo#21").from, "docs");
  const old = { ...q, verbs: { transfer: false, queueOrder: false } };
  assert.match(M.draftProblem(d, old), /no queue-order yet/);
  assert.match(M.draftProblem(d, { ...q, verbs: { transfer: false, queueOrder: true } }), /no transfer yet/);
  assert.equal(M.normQueue({ verbs: {} }).verbs.transfer, true, "a CLI that says nothing is not held back");
  // screens: back from the queue's plan, error and result goes to the queue
  for (const s of ["plan", "error", "result", "busy"]) assert.equal(M.backStep(s, "queue"), "queue");
  assert.equal(M.backStep("queue", "queue"), "menu");
  // the colors: another role's items stand apart
  const c = M.palette({});
  assert.equal(M.srcColor("other", c), c.violet);
  assert.notEqual(M.srcColor("other", c), M.srcColor("offer", c));
});

test("the verbs in labeled groups: each in exactly one, in the group's order, only what the target offers", () => {
  const all = M.GROUPS.flatMap((g) => g.verbs);
  assert.deepEqual([...all].sort(), M.VERBS.map((v) => v.verb).sort(), "every verb in a group");
  assert.equal(new Set(all).size, all.length, "and in one only");
  const roleGroups = M.verbGroups({ shoal: A, role: "core" });
  assert.deepEqual(roleGroups.map((g) => g.label), ["Talk", "Work", "Seat", "Shoal", "Inspect"]);
  assert.deepEqual(roleGroups.find((g) => g.key === "work").verbs.map((v) => v.verb), ["next", "queue", "offer", "withdraw", "fill"]);
  assert.deepEqual(roleGroups.flatMap((g) => g.verbs).map((v) => v.verb).sort(), M.verbsFor({ shoal: A, role: "core" }).map((v) => v.verb).sort());
  // a standalone seat: only its own verbs, and the groups it has none in are left out
  const seat = M.verbGroups({ session: "example-seat" });
  assert.deepEqual(seat.map((g) => g.key), ["seat"]);
  assert.deepEqual(seat[0].verbs.map((v) => v.verb), ["handoff", "dismiss", "remove-session"]);
});

test("a pick from the Board tab finds its row or candidate, owner, case and verb aside", () => {
  const q = { queue: [{ key: "example/app#3", item: "example/app#3:fix" }],
              candidates: [{ key: "example/app#9", item: "Example/App#9:review" }] };
  assert.equal(M.pickKey(q, "app#3"), "example/app#3");
  assert.equal(M.pickKey(q, "app#9"), "example/app#9");
  assert.equal(M.pickKey(q, "example/app#9:fix"), "example/app#9");
  assert.equal(M.pickKey(q, "app#4"), null, "not in this queue: nothing picked");
  assert.equal(M.pickKey(q, ""), null);
  assert.equal(M.pickKey(null, "app#3"), null);
});

test("a long command breaks at its arguments, never inside a word or a quoted span", () => {
  const cmd = "shoal handoff --spec /example/ledgers/shoals/alpha-20261101-000000/shoal.json --only core "
    + "--expected-session=aaaa0001-1111-4111-8111-111111111111";
  const lines = M.commandLines(cmd, 60).split("\n");
  assert.ok(lines.length > 1);
  for (const l of lines.slice(0, -1)) assert.ok(l.endsWith(" \\"), l);
  // the words come back whole, in order, with their spaces
  assert.equal(lines.map((l) => l.replace(/ \\$/, "").trim()).join(" "), cmd);
  assert.ok(lines.some((l) => l.trim().startsWith("--only core")), lines.join("\n"));
  // a single-quoted message keeps its spaces and stays on one line
  const msg = "shoal msg --to core --text 'a short note with  two spaces' --kind info";
  assert.ok(M.commandLines(msg, 30).split("\n").some((l) => l.includes("'a short note with  two spaces'")));
  // short commands and multi-command plans keep their lines
  assert.equal(M.commandLines("shoal next --shoal x"), "shoal next --shoal x");
  assert.equal(M.commandLines("shoal a\nshoal b"), "shoal a\nshoal b");
});
