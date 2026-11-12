// test_shoal_status.mjs: the shoal-status Herdr app mod's pure model. Node only, no dependencies.
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const M = await import(join(here, "..", "..", "plugins", "shoal-herdr-app", "mods", "shoal-status", "model.js"));

const doc = {
  accounts: [
    { provider: "claude", rows: [{ bucket: "claude", windows: [{ name: "5h", used_percent: 4 }, { name: "week", used_percent: 29 }] }] },
    { provider: "codex", stale: true, rows: [{ bucket: "plus", windows: [{ name: "week", used_percent: 95 }, { name: "5h", used_percent: null }] }] },
    { provider: "muse", error: "no key", rows: [] },
    { provider: "idle", rows: [{ bucket: "idle", windows: [] }] },
  ],
};
const rows = M.usageRows(doc);
assert.deepEqual(rows.map((r) => r.name), ["claude", "codex/plus", "muse"], "one entry per provider and bucket; nothing to say is left out");
assert.deepEqual(rows[0].windows.map((w) => [w.name, w.pct]), [["5h", 4], ["wk", 29]]);
assert.equal(rows[1].stale, true);
assert.deepEqual(rows[1].windows.map((w) => w.pct), [95], "only windows with a reading");
assert.equal(rows[2].error, "no key");
assert.deepEqual(M.usageRows({ accounts: [{ provider: "x", rows: [{ windows: [{ name: "5h", used_percent: 240 }] }] }] })[0].windows[0].pct, 100, "clamped");
assert.deepEqual(M.usageRows(null), []);
// the hover gives each window's time to reset, not the percentages the bars already show
const tipRow = M.usageRows({ accounts: [{ provider: "claude", rows: [{ windows: [
  { name: "5h", used_percent: 4, resets_at: 1000 + 2 * 3600 + 14 * 60 },
  { name: "week", used_percent: 45, resets_at: 1000 + 3 * 86400 + 4 * 3600 + 59 },
  { name: "day", used_percent: 1 }] }] }] })[0];
assert.equal(M.usageTip(tipRow, 1000), "claude: 5h resets in 2h 14m, wk resets in 3d 4h, day resets in ?");
assert.ok(!/%/.test(M.usageTip(tipRow, 1000)));
assert.equal(M.usageTip({ ...tipRow, windows: [{ name: "5h", resetsAt: 1000 }] }, 1030), "claude: 5h resets in <1m", "a passed reset reads as now");
assert.equal(M.usageTip({ name: "x", error: "no key", windows: [] }, 0), "no key");
assert.equal(M.untilText(-1), "");
assert.ok(M.usageColor(5) !== M.usageColor(75) && M.usageColor(75) !== M.usageColor(95));
assert.deepEqual(M.parseUsage({ code: 0, stdout: JSON.stringify(doc) }).length, 3);
assert.equal(M.parseUsage({ code: 1, stdout: JSON.stringify(doc) }), null, "a failed read keeps the last good rows");
assert.equal(M.parseUsage({ code: 0, stdout: "not json" }), null);
assert.equal(M.parseUsage(null), null);
assert.deepEqual(M.usageMachines([{ active: true, machine: "lane-b" }], ["lane-a", "lane-b"]), ["lane-b", "lane-a", null], "the space in view first, then the rest");

// Ctrl+Alt+N: the needs-you agents in the pulse's order (auth, asks, grants, the rest; the longest unchanged first), wrapping
const ag = [
  { address: "t1", pane: "p1", status: "blocked", seq: 9 },
  { address: "t2", pane: "p2", status: "working", seq: 1 },
  { address: "t3", pane: "p3", status: "blocked", seq: 5 },
  { address: "t4", pane: "p4", status: "blocked", seq: 7 },
  { address: "t5", pane: null, status: "blocked", seq: 1 },
];
const kind = { t1: "question", t3: null, t4: "auth" };
const kindOf = (a) => kind[a.address];
assert.deepEqual(M.needsYouOrder(ag, kindOf).map((a) => a.address), ["t4", "t1", "t3"], "auth first, then a question, then the rest; no pane, no stop");
assert.deepEqual(M.needsYouOrder(ag).map((a) => a.address), ["t3", "t4", "t1"], "with no kinds: the longest unchanged first");
assert.equal(M.nextNeedsYou(ag, kindOf, "t2").address, "t4", "from an agent that needs nothing: the first");
assert.equal(M.nextNeedsYou(ag, kindOf, "t4").address, "t1");
assert.equal(M.nextNeedsYou(ag, kindOf, "t3").address, "t4", "wraps");
assert.equal(M.nextNeedsYou([ag[1]], kindOf, null), null, "nobody needs you");

// the Lattice and Home buttons: an open tab is selected directly, with no process; none, and the CLI opens one
const sps = [
  { id: "s1", name: "alpha", active: true, machine: "lane-b", tabs: [{ id: "t1", name: "overview" }, { id: "t2", name: "lattice" }] },
  { id: "s2", name: "SHOAL-home", active: false, machine: null, tabs: [{ id: "t3", name: "lattice" }, { id: "t4", name: "home" }] },
  { id: "s3", name: "SHOAL-home", active: false, machine: "lane-b", tabs: [{ id: "t5", name: "home" }] },
];
assert.deepEqual(M.latticeTab(sps), { space: "s1", tab: "t2" }, "the shown space's own lattice tab");
assert.equal(M.latticeTab([{ ...sps[0], tabs: [{ id: "t1", name: "overview" }] }]), null, "none: the CLI reopens it");
assert.deepEqual(M.homeTab(sps), { space: "s2", tab: "t4" }, "this computer's home space, its home tab");
assert.equal(M.homeTab([sps[2]]), null, "another machine's home space is not this computer's");
assert.deepEqual(M.homeTab(sps, "SHOAL-home", "lattice"), { space: "s2", tab: "t3" }, "the home space's global lattice");
assert.equal(M.homeTab([{ ...sps[1], tabs: [{ id: "t4", name: "home" }] }], "SHOAL-home", "lattice"), null, "no lattice tab there: none");
// the shoal in view, from a card terminal in the shown space (a remote one's space id carries its machine)
const terms = [{ address: "lane-b/t9", labels: { "herdr.space": "s1-local", "shoal.pane": "alpha-1/core" } },
               { address: "t2", labels: { "herdr.space": "s5", "shoal.pane": "standalone/seat" } }];
assert.equal(M.shoalInView([{ id: "lane-b/s1-local", active: true, tabs: [] }], terms), "alpha-1");
assert.equal(M.shoalInView([{ id: "s5", active: true, tabs: [] }], terms), null, "a standalone space has no shoal");

assert.deepEqual(M.needsYouOrder([{ address: "b", pane: "p", status: "blocked" }, { address: "a", pane: "q", status: "blocked", seq: 0 }]).map((a) => a.address), ["a", "b"], "no seq reads as 0: a stable order");
assert.deepEqual(M.usageMachines([{ active: true, machine: null }], ["lane-a", null]), ["lane-a", null]);
assert.deepEqual(M.usageMachines([], []), [null]);
console.log("shoal-status: ok");
