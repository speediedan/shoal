// test_shoal_usage.mjs: the shoal-usage Herdr app mod's pure module.
// Node only, no dependencies: node --test tests/herdr_app/*.mjs (or node this file)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "..", "plugins", "shoal-herdr-app", "mods", "shoal-usage");
const M = await import(join(MOD, "model.js"));
const modJson = JSON.parse(readFileSync(join(MOD, "mod.json"), "utf8"));

const NOW = 1_800_000_000;
const win = (name, used, resetIn, minutes) => ({ name, used_percent: used, resets_at: NOW + resetIn, minutes });
const DOC = {
  accounts: [
    { provider: "alpha", source: "account API", age_s: 120, stale: false, error: "",
      rows: [{ bucket: "alpha", plan: "pro", windows: [win("5h", 40, 4 * 3600, 300), win("week", 10, 6 * 86400, 10080)] }] },
    { provider: "beta", source: "account API", age_s: 30, stale: true, error: "",
      rows: [{ bucket: "beta-team", plan: "team", windows: [win("5h", 95, 600, 300)] }] },
    { provider: "gamma", rows: [], error: "no credential" },
  ],
};

test("a tab mod that takes the keyboard", () => {
  assert.equal(modJson.place, "none");
  assert.equal(modJson.focusable, true);
});

test("a window's pace: 40% one hour into five runs out before the reset", () => {
  const v = M.windowView(win("5h", 40, 4 * 3600, 300), NOW);
  assert.equal(v.label, "5-hour");
  assert.ok(Math.abs(v.elapsed - 0.2) < 1e-9);
  assert.ok(Math.abs(v.projected - 200) < 1e-9);
  assert.equal(v.severity, "warn", "on pace to run out reads as a warning before 70%");
  assert.ok(Math.abs(v.runsOutIn - 5400) < 1e-6, "60% left at 40% an hour: 1.5 h");
  assert.equal(M.windowNote(v), "resets in 4h · runs out in 1h 30m at this pace");
  const slow = M.windowView(win("week", 10, 6 * 86400, 10080), NOW);
  assert.equal(slow.severity, "ok");
  assert.match(M.windowNote(slow), /^resets in 6d · on pace for 70%$/);
  // too early in a window to say anything about its pace
  const early = M.windowView(win("5h", 3, 299 * 60, 300), NOW);
  assert.equal(early.projected, null);
  assert.equal(M.windowNote(early), "resets in 4h 59m");
  assert.equal(M.windowNote(M.windowView(win("5h", 100, 60, 300), NOW)), "resets in 1m · used up");
});

test("cards: one per account and bucket, an account with no reading says why", () => {
  const cs = M.cards(DOC, NOW);
  assert.deepEqual(cs.map((c) => c.title), ["alpha", "beta · beta-team", "gamma"]);
  assert.equal(cs[1].stale, true);
  assert.equal(cs[1].windows[0].severity, "high");
  assert.equal(cs[2].error, "no credential");
  assert.equal(cs[2].windows.length, 0);
  const top = M.tightest(cs);
  assert.equal(top.card.provider, "beta");
  assert.equal(top.window.key, "5h");
  assert.equal(M.tightest([]), null);
  assert.equal(M.updatedAge(DOC), 30);
});

test("breakdowns: null until the document carries them, then shares largest first", () => {
  assert.equal(M.breakdownRows(DOC, "by_role"), null);
  const doc = { ...DOC, breakdown: { by_role: [{ name: "core", tokens: 300 }, { name: "docs", tokens: 100 }, { name: "idle", tokens: 0 }] } };
  const rows = M.breakdownRows(doc, "by_role");
  assert.deepEqual(rows.map((r) => [r.name, r.share]), [["core", 0.75], ["docs", 0.25]]);
  assert.deepEqual(M.breakdownRows({ breakdown: { standalone: [] } }, "standalone"), []);
  assert.deepEqual(M.BREAKDOWNS.map((b) => b.key), ["by_shoal", "by_role", "standalone"]);
});

test("the reply and the argv", () => {
  assert.equal(M.parseReply({ code: 1, stdout: "{}" }), null);
  assert.equal(M.parseReply({ code: 0, stdout: "not json" }), null);
  assert.equal(M.parseReply({ code: 0, stdout: "{}" }), null, "no accounts list: not a usage document");
  assert.deepEqual(M.parseReply({ code: 0, stdout: JSON.stringify(DOC) }).accounts.length, 3);
  assert.deepEqual(M.usageArgv({}), ["shoal", "ui", "usage", "--json"]);
  assert.deepEqual(M.usageArgv({}, true), ["shoal", "ui", "usage", "--json", "--refresh"]);
  assert.deepEqual(M.usageArgv({ argv: ["x", "--refresh"] }, true), ["x", "--refresh"]);
  assert.equal(M.duration(45), "45s");
  assert.equal(M.duration(3 * 86400 + 4 * 3600 + 5), "3d 4h");
  assert.equal(M.duration(-1), "");
});
