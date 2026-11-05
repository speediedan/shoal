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
assert.ok(M.usageColor(5) !== M.usageColor(75) && M.usageColor(75) !== M.usageColor(95));
assert.deepEqual(M.parseUsage({ code: 0, stdout: JSON.stringify(doc) }).length, 3);
assert.equal(M.parseUsage({ code: 1, stdout: JSON.stringify(doc) }), null, "a failed read keeps the last good rows");
assert.equal(M.parseUsage({ code: 0, stdout: "not json" }), null);
assert.equal(M.parseUsage(null), null);
assert.equal(M.usageMachine([{ active: true, machine: "lane-b" }], ["lane-a"]), "lane-b");
assert.equal(M.usageMachine([{ active: true, machine: null }], ["lane-a"]), "lane-a");
assert.equal(M.usageMachine([], []), null);
console.log("shoal-status: ok");
