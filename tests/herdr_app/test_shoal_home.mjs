// test_shoal_home.mjs: the shoal-home Herdr app mod's pure modules against its bundled fixture.
// Node only, no dependencies: node --test tests/herdr_app/*.mjs (or node this file)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const MODS = join(here, "..", "..", "plugins", "shoal-herdr-app", "mods");
const MOD = join(MODS, "shoal-home");
const M = await import(join(MOD, "model.js"));
const L = await import(join(MOD, "layout.js"));
const Mo = await import(join(MOD, "motion.js"));
const fixtureText = readFileSync(join(MOD, "fixtures", "launch-list.json"), "utf8");
const list = M.parseList(fixtureText);
const heightsOf = (l, status = new Map()) => l.hosts.map((h) => L.cardHeight(M.cardLines(h, status.get(h.name) ?? null).length));

test("fixture parses: local first, routes in order, fields normalized", () => {
  assert.deepEqual(list.hosts.map((h) => h.name), ["local", "lane-a", "lane-b", "lane-c", "lane-d"]);
  assert.equal(list.last, "lane-a");
  const [local, a, b, c, d] = list.hosts;
  assert.equal(local.local, true);
  assert.equal(local.route, null);
  assert.equal(a.reachable, true);
  assert.equal(b.reachable, false);
  assert.equal(c.reachable, null, "null reachable means not probed");
  assert.equal(M.hostState(a), "up");
  assert.equal(M.hostState(b), "down");
  assert.equal(M.hostState(c), "unknown");
  assert.equal(a.summary.needsYou, 2);
  assert.equal(a.summary.pinOk, true);
  assert.deepEqual(a.summary.shoals[0], { id: "alpha-20261101-000000", tag: "alpha", roles: 7, working: 3, waiting: 3, needsYou: 2 });
  assert.equal(d.summary.pinOk, false);
  assert.equal(b.summary, null);
});

test("unknown fields are ignored; bad rows skipped; contract errors reported", () => {
  const doc = JSON.parse(fixtureText);
  doc.future = { x: 1 };
  doc.hosts[0].future_field = [1, 2];
  doc.hosts[0].summary.shoals[0].future = "y";
  doc.hosts.push({ name: "" }, { nope: 1 }, "str", null, { name: "lane-a", reachable: false });
  const l = M.parseList(doc);
  assert.deepEqual(l, list, "extra fields, nameless rows and a repeated name change nothing");
  assert.equal(M.listSignature(l), M.listSignature(list));
  // missing optional fields
  const bare = M.parseList({ hosts: [{ name: "lane-x" }, { name: "local" }] });
  assert.deepEqual(bare.hosts.map((h) => h.name), ["local", "lane-x"]);
  assert.equal(bare.last, null);
  assert.equal(bare.hosts[1].reachable, null);
  assert.equal(bare.hosts[1].summary, null);
  // a last that names no host is dropped; a tag falls back to the id's short form
  const odd = M.parseList({ last: "gone", hosts: [{ name: "h", summary: { shoals: [{ id: "zeta-20261101-000000", needs_you: 3 }], pin_ok: "yes" } }] });
  assert.equal(odd.last, null);
  assert.equal(odd.hosts[0].summary.shoals[0].tag, "zeta");
  assert.equal(odd.hosts[0].summary.needsYou, 3, "needs_you falls back to the shoal sum");
  assert.equal(odd.hosts[0].summary.pinOk, null);
  assert.throws(() => M.parseList("not json"), /not JSON/);
  assert.throws(() => M.parseList({ v: 2, hosts: [] }), /v2/);
  assert.throws(() => M.parseList({ kind: "shoal-graph", hosts: [] }), /not a launch list/);
  assert.throws(() => M.parseList([]), /not an object/);
});

test("card lines: summary rows, unreachable hint, pin warning, status", () => {
  const [local, a, b, c, d] = list.hosts;
  assert.deepEqual(M.cardLines(local).map((x) => x.kind), ["sub", "shoal"]);
  assert.equal(M.cardLines(local)[0].text, "this machine");
  assert.deepEqual(M.cardLines(a).map((x) => x.kind), ["sub", "shoal", "shoal"]);
  assert.equal(M.cardLines(a)[0].text, "<user>@lane-a");
  const down = M.cardLines(b);
  assert.equal(down[1].tone, "down");
  assert.match(down[1].text, /timed out/);
  assert.equal(M.cardLines(c)[1].text, "not probed yet");
  assert.deepEqual(M.cardLines(d).map((x) => x.kind), ["sub", "note", "pin"]);
  const many = M.parseList({ hosts: [{ name: "h", reachable: true, summary: { shoals: Array.from({ length: 7 }, (_, i) => ({ id: `s${i}`, needs_you: i })) } }] }).hosts[0];
  const ml = M.cardLines(many);
  assert.equal(ml.filter((x) => x.kind === "shoal").length, M.MAX_SHOALS);
  assert.equal(ml.at(-1).text, "+3 more shoals");
  assert.equal(M.cardLines(a, { phase: "opening" }).at(-1).text, "opening…");
  assert.equal(M.cardLines(a, { phase: "error", text: "exit 2: boom" }).at(-1).tone, "error");
  assert.equal(M.shoalCounts(a.summary.shoals[0]), "3 working · 3 waiting · 7 roles");
  assert.equal(M.hostNeedsYou(a), 2);
  assert.equal(M.hostNeedsYou(b), 0);
});

test("opening: reachable and unprobed cards open, unreachable ones do not; results summarized", () => {
  const [local, a, b, c] = list.hosts;
  assert.equal(M.canOpen(local), true);
  assert.equal(M.canOpen(a), true);
  assert.equal(M.canOpen(b), false);
  assert.equal(M.canOpen(c), true);
  assert.deepEqual(M.openArgv(a), ["ui", "app-open", "--machine", "lane-a"]);
  assert.deepEqual(M.openResult({ code: 0, stdout: "planning\nopened 2 spaces on lane-a\n", stderr: "" }), { ok: true, text: "opened 2 spaces on lane-a" });
  assert.deepEqual(M.openResult({ code: 0, stdout: "", stderr: "" }), { ok: true, text: "opened" });
  assert.deepEqual(M.openResult({ code: 0, stdout: '{"summary":"focused alpha","spaces":2}', stderr: "" }), { ok: true, text: "focused alpha" });
  assert.deepEqual(M.openResult({ code: 3, stdout: "", stderr: "trace\nlane-a unreachable\n" }), { ok: false, text: "exit 3: lane-a unreachable" });
  assert.deepEqual(M.openResult({ code: 1, stdout: '{"error":"no such machine"}', stderr: "" }), { ok: false, text: "exit 1: no such machine" });
  const thrown = M.openResult(new Error("not installed"));
  assert.equal(thrown.ok, false);
  assert.match(thrown.text, /not installed/);
  assert.ok(M.openResult({ code: 1, stdout: "", stderr: "x".repeat(500) }).text.length <= 160);
});

test("keyboard selection: arrows and j/k wrap, digits jump, Enter opens, r refreshes", () => {
  const n = list.hosts.length;
  assert.equal(M.initialSelection(list), 1, "starts on the last choice");
  assert.equal(M.initialSelection({ last: null, hosts: [{ reachable: false }, { reachable: true }] }), 1, "else the first that can open");
  assert.equal(M.initialSelection({ last: null, hosts: [] }), 0);
  assert.deepEqual(M.keyAction("down", 0, n), { sel: 1, action: null });
  assert.deepEqual(M.keyAction("j", n - 1, n), { sel: 0, action: null });
  assert.deepEqual(M.keyAction("up", 0, n), { sel: n - 1, action: null });
  assert.deepEqual(M.keyAction("k", 2, n), { sel: 1, action: null });
  assert.deepEqual(M.keyAction("right", 1, n), { sel: 2, action: null });
  assert.deepEqual(M.keyAction("3", 0, n), { sel: 2, action: null });
  assert.equal(M.keyAction("9", 0, n), null, "a digit past the list does nothing");
  assert.deepEqual(M.keyAction("enter", 2, n), { sel: 2, action: "open" });
  assert.deepEqual(M.keyAction("r", 2, n), { sel: 2, action: "refresh" });
  assert.deepEqual(M.keyAction("r", 0, 0), { sel: 0, action: "refresh" });
  assert.equal(M.keyAction("down", 0, 0), null);
  assert.equal(M.keyAction("x", 0, n), null);
  // a poll keeps the selection on the same host
  const reordered = M.parseList({ hosts: [{ name: "lane-d" }, { name: "lane-a" }, { name: "local" }] });
  assert.equal(reordered.hosts[M.keepSelection(list.hosts, reordered, 1)].name, "lane-a");
  assert.equal(M.keepSelection(list.hosts, { hosts: [{ name: "x" }] }, 4), 0);
});

const SIZES = [[1440, 900], [1280, 820], [1100, 760], [994, 700], [800, 560], [700, 500], [640, 460], [600, 700], [420, 320], [320, 240]];

test("layout: art scales with the window; nothing overlaps; everything inside the window", () => {
  const hs = heightsOf(list);
  let prev = Infinity;
  for (const [w, h] of [[1440, 900], [1280, 820], [1100, 760], [994, 700]]) {
    const l = L.homeLayout({ width: w, height: h }, hs);
    assert.equal(l.mode, "art", `${w}x${h} has room for the art`);
    assert.equal(l.creatures.length, 5);
    assert.ok(l.scale <= prev, "a smaller window never scales the art up");
    prev = l.scale;
  }
  for (const [w, h] of SIZES) {
    for (const status of [new Map(), new Map([["lane-a", { phase: "error", text: "exit 1" }]])]) {
      const l = L.homeLayout({ width: w, height: h }, heightsOf(list, status));
      const rects = L.occupied(l);
      for (let i = 0; i < rects.length; i++) {
        for (let j = i + 1; j < rects.length; j++) assert.ok(!L.overlaps(rects[i], rects[j]), `${w}x${h} ${l.mode}: ${rects[i].kind} overlaps ${rects[j].kind}`);
      }
      for (const r of rects) {
        assert.ok(r.x >= 0 && r.x + r.w <= w, `${w}x${h}: ${r.kind} inside horizontally`);
        assert.ok(r.y >= 0, `${w}x${h}: ${r.kind} below the top`);
        if (l.mode === "art") assert.ok(r.y + r.h <= h, `${w}x${h}: ${r.kind} inside vertically (art never scrolls)`);
        else assert.ok(r.y + r.h <= l.contentH, `${w}x${h}: ${r.kind} inside the scrolled content`);
      }
      for (const [i, c] of l.cards.entries()) assert.ok(c.h >= hs[i] || status.size, "a card is as tall as its lines");
      for (const c of l.creatures) assert.ok(Number.isFinite(c.x + c.y + c.size + c.drift + c.period));
    }
  }
});

test("layout: text fallback below the art floor or without images", () => {
  const hs = heightsOf(list);
  for (const [w, h] of [[639, 800], [1200, 459], [420, 320], [320, 240]]) {
    const l = L.homeLayout({ width: w, height: h }, hs);
    assert.equal(l.mode, "text", `${w}x${h} is below the floor`);
    assert.equal(l.creatures.length, 0);
    assert.equal(l.cards.length, list.hosts.length, "the host list is still there");
  }
  const noArt = L.homeLayout({ width: 1440, height: 900 }, hs, { art: false });
  assert.equal(noArt.mode, "text");
  assert.equal(noArt.creatures.length, 0);
  // too many hosts for the art at this size: text, scrolling
  const crowd = L.homeLayout({ width: 700, height: 520 }, Array.from({ length: 12 }, () => L.cardHeight(6)));
  assert.equal(crowd.mode, "text");
  assert.ok(crowd.contentH > 520, "the text screen scrolls");
  // cards: one column when narrow, never wider than the max, rows centered
  const narrow = L.homeLayout({ width: 320, height: 900 }, hs);
  assert.equal(narrow.cols, 1);
  const wide = L.homeLayout({ width: 1440, height: 900 }, hs);
  assert.ok(wide.cards.every((c) => c.w <= L.CARD.maxW));
  assert.equal(wide.cols, 3, "five hosts balance as three and two");
  assert.equal(L.homeLayout({ width: 1440, height: 900 }, []).cards.length, 0);
});

test("assets and motion: five creatures shipped, motion.js identical to lattice-view's", () => {
  for (const c of L.CREATURES) assert.ok(existsSync(join(MOD, "assets", c.file)), c.file);
  assert.equal(readFileSync(join(MOD, "motion.js"), "utf8"), readFileSync(join(MODS, "lattice-view", "motion.js"), "utf8"),
    "shoal-home/motion.js is a verbatim copy of lattice-view/motion.js: edit there, then copy");
  assert.equal(Mo.motionPolicy(Mo.resolveTier("auto", { renderer: "cpu" }).tier).loops, false, "a CPU renderer never drifts");
  assert.equal(Mo.motionPolicy(Mo.resolveTier("auto", { renderer: "gpu" }).tier).loops, true, "a GPU renderer drifts at full");
  assert.equal(Mo.motionPolicy(Mo.resolveTier("auto", { renderer: "gpu", focused: false }).tier).loops, false, "not while the window is unfocused");
  assert.equal(Mo.motionPolicy(Mo.resolveTier("auto", {}).tier).loops, false, "an unknown renderer stays still");
});

const O = await import(join(MOD, "ocean.js"));

test("ocean: kelp, floats and tendrils stay clear of the logo and the host cards, at every art size", () => {
  const sizes = [[1425, 860], [1150, 770], [1920, 1200], [900, 640], [700, 520]];
  let drawn = 0;
  for (const [w, h] of sizes) {
    const lay = L.homeLayout({ width: w, height: h }, heightsOf(list), { art: true });
    const sea = O.oceanScene(lay);
    if (lay.mode !== "art") { assert.equal(sea, null, `${w}x${h}: no sea in text mode`); continue; }
    drawn++;
    assert.ok(sea.strands.length >= 2 && sea.nodes.length >= 4 && sea.edges.length >= 2, `${w}x${h}: a network`);
    const lg = lay.logo;
    const cardsTop = Math.min(...lay.cards.map((c) => c.y));
    for (const it of sea.items) {
      const b = O.itemBounds(it);
      assert.ok(b.x0 >= 0 && b.x1 <= w && b.y0 >= 0, `${w}x${h}: ${it.kind} inside the window`);
      assert.ok(b.y1 < cardsTop - 8, `${w}x${h}: ${it.kind} above the host cards`);
    }
    for (const s of sea.strands) {
      const xs = s.segs.flatMap((g) => [g[0], g[2], g[4], g[6]]);
      assert.ok(Math.max(...xs) < lg.x || Math.min(...xs) > lg.x + lg.w, `${w}x${h}: a strand stays beside the logo`);
    }
    for (const n of sea.nodes) {
      assert.ok(!(n.x > lg.x && n.x < lg.x + lg.w && n.y > lg.y && n.y < lg.y + lg.h), `${w}x${h}: no float on the logo`);
      assert.ok(n.y <= sea.seabed, `${w}x${h}: floats above the seabed`);
    }
    const ids = new Set(sea.nodes.map((n) => n.id));
    for (const e of sea.edges) {
      assert.ok(ids.has(e.a), "an edge starts at a float");
      assert.ok(ids.has(e.b) || e.b.startsWith("creature:"), "an edge ends at a float or a creature");
    }
    assert.ok(sea.edges.some((e) => e.creature), `${w}x${h}: creatures join the network`);
    assert.ok(sea.items.length < 60, `${w}x${h}: styles merged into few layers (${sea.items.length})`);
  }
  assert.ok(drawn >= 3, "most sizes draw the sea");
  const lay = L.homeLayout({ width: 1150, height: 770 }, heightsOf(list), { art: true });
  assert.deepEqual(O.oceanScene(lay), O.oceanScene(lay), "the same layout draws the same sea");
});

test("ocean: merged items keep one layer per style and every subpath", () => {
  const a = { kind: "node", cmds: [["M", 0, 0], ["Z"]], fill: true, color: "#fff", alpha: 0.5 };
  const b = { kind: "node", cmds: [["M", 1, 1], ["Z"]], fill: true, color: "#fff", alpha: 0.5 };
  const s = { kind: "kelp", cmds: [["M", 2, 2]], width: 3, color: "#0f0", alpha: 1, sway: { amp: 2, period: 4000 } };
  const m = O.mergeItems([a, s, b]);
  assert.equal(m.length, 2);
  assert.deepEqual(m[0].cmds, [["M", 0, 0], ["Z"], ["M", 1, 1], ["Z"]]);
  assert.equal(m[1], s);
});

test("ocean: a bridge joins the two kelp networks over the logo without touching the letters", () => {
  for (const [w, h] of [[1425, 860], [1150, 770], [1920, 1200]]) {
    const lay = L.homeLayout({ width: w, height: h }, heightsOf(list), { art: true });
    const sea = O.oceanScene(lay);
    const lg = lay.logo;
    assert.equal(sea.bridge.length, 3, `${w}x${h}: left float, two buoys, right float`);
    const ends = [sea.bridge[0].from, sea.bridge[2].to];
    assert.ok(ends[0].x < lg.x && ends[1].x > lg.x + lg.w, `${w}x${h}: it runs from the left network to the right one`);
    for (const b of sea.bridge) {
      for (const p of O.sampleCurve(b.cmds)) {
        assert.ok(!(p.x > lg.x && p.x < lg.x + lg.w && p.y > lg.y && p.y < lg.y + lg.h), `${w}x${h}: the bridge stays off the letters`);
      }
    }
    for (const n of sea.nodes.filter((x) => x.id.startsWith("bridge."))) assert.ok(n.y < lg.y, "the buoys float above the logo");
    assert.ok(sea.edges.filter((e) => e.bridge).length === 3);
  }
});

test("ocean: the logo letters ride one wave inside the logo box, in order, without overlapping", () => {
  const lg = { x: 100, y: 50, w: 84 * 3.9, h: 84 * 1.25, font: 84 };
  const ls = O.logoLetters(lg);
  assert.deepEqual(ls.map((l) => l.ch).join(""), "SHOAL");
  for (let i = 1; i < ls.length; i++) assert.ok(ls[i].x >= ls[i - 1].x + ls[i - 1].w, "letters do not overlap");
  assert.ok(ls[0].x >= lg.x && ls[4].x + ls[4].w <= lg.x + lg.w, "the word fits the box");
  const ys = ls.map((l) => l.y);
  assert.ok(Math.max(...ys) - Math.min(...ys) > 84 * 0.08, "the letters are at different heights (a wave)");
  assert.ok(Math.max(...ys.map((y) => Math.abs(y - lg.y))) <= 84 * 0.085 + 0.1, "a gentle wave keeps the word readable");
  assert.equal(new Set(O.LETTER_SHADES).size, O.LETTER_SHADES.length, "the shades grade without repeats");
  // ripple cells cover every pixel of each letter box, and their band edges differ between strips (a ripple)
  for (const l of ls) {
    const cells = O.rippleBands(l, 84, O.LETTER_SHADES.length);
    for (let x = 0; x < Math.floor(l.w); x += 3) {
      for (let y = 0; y < Math.floor(l.h); y += 3) {
        assert.ok(cells.some((c) => x >= c.x && x < c.x + c.w && y >= c.y0 && y < c.y1), `${l.ch}: pixel ${x},${y} shaded`);
      }
    }
    const edgesOf = (j) => cells.filter((c) => c.x === cells.filter((d) => d.k === 0)[j].x).map((c) => c.y0);
    assert.notDeepEqual(edgesOf(0), edgesOf(2), `${l.ch}: band edges ripple across the strips`);
  }
});
