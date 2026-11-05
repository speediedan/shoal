// test_shoal_home.mjs: the shoal-home Herdr app mod's pure modules against its bundled fixture.
// Node only, no dependencies: node --test tests/herdr_app/*.mjs (or node this file)
import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const PREVIEW = join(here, "..", "..", "plugins", "shoal-herdr-app", "preview");
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

// ---- the shoals' links and the solo vines

const ART_SIZES = [[1425, 860], [1150, 770], [1920, 1200], [1100, 760]];
const Lo = await import(join(MOD, "logo.js"));
const Gl = await import(join(MOD, "glyphs.js"));
const { readPng } = await import(join(PREVIEW, "png.mjs"));
const finitePts = (cmds) => cmds.every((c) => c.slice(1).every((v) => typeof v === "boolean" || Number.isFinite(v)));

/** Even-odd: is (x, y) inside the logo's ink? `polys` from `inkPolys`. */
const inkPolys = (lg) => Lo.logoScene(lg).letters.flatMap((l) => l.loops.map(Lo.sampleLoop));
function inInk(polys, x, y) {
  let inside = false;
  for (const poly of polys) {
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [xi, yi] = poly[i], [xj, yj] = poly[j];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
  }
  return inside;
}
const seg2pts = (g) => O.sampleCurve([["M", g[0], g[1]], ["C", g[2], g[3], g[4], g[5], g[6], g[7]]]);

test("ocean: exactly one direct link between the two shoals, off the letters", () => {
  for (const [w, h] of ART_SIZES) {
    const lay = L.homeLayout({ width: w, height: h }, heightsOf(list), { art: true });
    const sea = O.oceanScene(lay);
    const lg = lay.logo;
    assert.equal(sea.bridge.length, 1, `${w}x${h}: one bridge`);
    const [b] = sea.bridge;
    assert.ok(b.from.x < lg.x && b.to.x > lg.x + lg.w, `${w}x${h}: it runs from the left set to the right one`);
    assert.equal(sea.strands[b.from.strand].side, 0);
    assert.equal(sea.strands[b.to.strand].side, 1);
    // no other edge joins the two sets directly: every other crossing goes through a solo vine's float
    const byId = new Map(sea.nodes.map((n) => [n.id, n]));
    const side = (id) => { const n = byId.get(id); return n && n.strand >= 0 ? sea.strands[n.strand].side : null; };
    const direct = sea.edges.filter((e) => side(e.a) !== null && side(e.b) !== null && side(e.a) !== side(e.b));
    assert.equal(direct.length, 1, `${w}x${h}: one direct edge across, found ${direct.length}`);
    assert.ok(direct[0].bridge);
    for (const p of O.sampleCurve(b.cmds)) {
      assert.ok(!(p.x > lg.x && p.x < lg.x + lg.w && p.y > lg.y && p.y < lg.y + lg.h), `${w}x${h}: the bridge stays off the letters`);
    }
  }
});

test("ocean: two solo vines rise between the sets, each float tied to both shoals", () => {
  for (const [w, h] of ART_SIZES) {
    const lay = L.homeLayout({ width: w, height: h }, heightsOf(list), { art: true });
    const sea = O.oceanScene(lay);
    const lg = lay.logo;
    assert.equal(sea.vines.length, 2, `${w}x${h}: two solo vines`);
    assert.equal(sea.links.length, 4, `${w}x${h}: two links each`);
    assert.equal(new Set(sea.links.map((e) => e.a)).size, 4, `${w}x${h}: each link leaves its own float`);
    for (const v of sea.vines) {
      const ls = sea.links.filter((e) => e.b === v.tip.id);
      assert.deepEqual(ls.map((e) => sea.strands[e.from.strand].side).sort(), [0, 1], `${w}x${h}: ${v.id} reaches both sets`);
      assert.ok(v.tip.y < lg.y && v.tip.y - v.tip.r > sea.top - 8, `${w}x${h}: ${v.id}'s float sits above the logo`);
      assert.equal(v.segs[0][1], sea.seabed, `${w}x${h}: ${v.id} is rooted on the seabed`);
      assert.deepEqual(v.segs.at(-1).slice(6), [v.tip.x, v.tip.y], `${w}x${h}: ${v.id} ends on its float`);
      const xs = v.segs.flatMap((g) => [g[0], g[6]]);
      assert.ok(Math.min(...xs) > lg.x && Math.max(...xs) < lg.x + lg.w, `${w}x${h}: ${v.id} stays between the two sets`);
      for (const e of ls) {
        for (const p of O.sampleCurve(e.cmds)) {
          assert.ok(!(p.x > lg.x && p.x < lg.x + lg.w && p.y > lg.y && p.y < lg.y + lg.h), `${w}x${h}: ${e.a} to ${e.b} stays off the letters`);
        }
      }
    }
    const [a, b] = sea.vines.map((v) => v.tip.x);
    assert.ok(a < lg.x + lg.w / 2 && b > lg.x + lg.w / 2, `${w}x${h}: one float each side of the coral`);
  }
});

test("ocean: each solo vine weaves, under one stroke of a letter and over another", () => {
  for (const [w, h] of ART_SIZES) {
    const lay = L.homeLayout({ width: w, height: h }, heightsOf(list), { art: true });
    const sea = O.oceanScene(lay);
    const polys = inkPolys(lay.logo);
    for (const v of sea.vines) {
      const isFront = (g) => v.front.includes(g);
      const hits = (g) => seg2pts(g).some((p) => inInk(polys, p.x, p.y));
      assert.ok(v.segs.filter(isFront).some(hits), `${w}x${h}: ${v.id} passes in front of a stroke`);
      assert.ok(v.segs.filter((g) => !isFront(g)).some(hits), `${w}x${h}: ${v.id} passes behind a stroke`);
      for (const g of v.front) {
        assert.ok(!inInk(polys, g[0], g[1]) && !inInk(polys, g[6], g[7]), `${w}x${h}: ${v.id}'s over-crossing starts and ends in open water, so no seam shows`);
      }
      assert.ok(v.width < lay.logo.font * 0.07, `${w}x${h}: a vine is fine beside the letters`);
    }
    for (const it of sea.front) {
      assert.equal(it.kind, "vine");
      assert.ok(finitePts(it.cmds), "finite front paths");
      assert.ok(it.sway && it.sway.period > 0, "the front copy sways with the vine behind it");
    }
    assert.ok(sea.front.length <= 6, `${w}x${h}: few layers in front (${sea.front.length})`);
    const back = sea.items.filter((it) => it.kind === "vine");
    assert.equal(back.length, sea.front.length, "each vine stroke has a back copy and a front copy");
    back.forEach((it, i) => assert.deepEqual(it.sway, sea.front[i].sway, "the two copies move as one"));
  }
});

test("motion: the vines move only through the budget, natively; the logo never moves", () => {
  const src = readFileSync(join(MOD, "main.js"), "utf8");
  assert.match(src, /drift && it\.sway \? el\.loop\("left"/, "sway is a native loop, gated on drift");
  assert.match(src, /motionPolicy\(tier\.tier\)\.loops/, "drift follows the budget");
  assert.doesNotMatch(src, /setTimeout|setInterval|requestAnimationFrame/, "no timer redraw");
  assert.equal((src.match(/cx\.timer\.every/g) ?? []).length, 1, "the one timer is the host list poll");
  assert.match(src, /logo\.items\) canvas\.child\(seaEl\(it\)\)/, "the logo is drawn still");
  const lay = L.homeLayout({ width: 1150, height: 770 }, heightsOf(list), { art: true });
  assert.ok(Lo.logoDraw(lay.logo, false).items.every((it) => !it.sway), "no logo layer sways");
  const sea = O.oceanScene(lay);
  assert.ok(sea.items.filter((it) => it.kind === "vine").every((it) => it.sway.amp <= 2.5), "a small sway");
});

// ---- the logo

const LOGO_BOXES = [
  ...[[1920, 1200], [1425, 860], [1150, 770], [994, 700], [800, 560], [700, 500], [640, 460], [420, 320], [320, 240]]
    .map(([w, h]) => L.homeLayout({ width: w, height: h }, heightsOf(list)).logo),
  { x: 100, y: 50, w: 126 * 3.9, h: 126 * 1.25, font: 126 },
  { x: 0, y: 0, w: 34 * 3.9, h: 34 * 1.25, font: 34 },
];

test("logo: five letters on one baseline, finite, inside the logo box at every size", () => {
  for (const lg of LOGO_BOXES) {
    const sc = Lo.logoScene(lg);
    assert.equal(sc.letters.map((l) => l.ch).join(""), "SHOAL");
    assert.equal(new Set(sc.letters.map((l) => l.baseline)).size, 1, `font ${lg.font}: one baseline`);
    assert.equal(sc.letters[0].baseline, sc.baseline);
    for (let i = 1; i < 5; i++) assert.ok(sc.letters[i].x > sc.letters[i - 1].x, "in order");
    for (const it of sc.items) assert.ok(finitePts(it.cmds), `font ${lg.font}: ${it.kind} finite`);
    for (const l of sc.letters) {
      for (const cmds of l.loops) {
        assert.ok(finitePts(cmds));
        for (const [x, y] of Lo.sampleLoop(cmds)) {
          assert.ok(x >= lg.x && x <= lg.x + lg.w, `font ${lg.font}: ${l.ch} inside the box horizontally (${x})`);
          assert.ok(y >= lg.y && y <= lg.y + lg.h, `font ${lg.font}: ${l.ch} inside the box vertically (${y})`);
        }
      }
    }
    const b = sc.bounds;
    assert.ok(b.x >= lg.x - 1 && b.y >= lg.y - 1 && b.x + b.w <= lg.x + lg.w + 1 && b.y + b.h <= lg.y + lg.h + 1, `font ${lg.font}: the ink bounds fit`);
    assert.ok(sc.items.length <= 6, `font ${lg.font}: a handful of paths, not hundreds of text copies (${sc.items.length})`);
  }
});

test("logo: every letter's body really stands on the baseline and reaches the cap line, only drips below", () => {
  const font = 1000;
  const lg = { x: 0, y: 0, w: font * 3.9, h: font * 1.25, font };
  const sc = Lo.logoScene(lg);
  const capY = sc.baseline - Gl.CAP_EM * font;
  for (const l of sc.letters) {
    const polys = l.loops.map(Lo.sampleLoop);
    // the row's ink: {total, run}, in 1/1000 em, its widest unbroken run included
    const row = (y) => {
      let total = 0, run = 0, cur = 0;
      for (let x = l.x - 300; x < l.x + l.adv + 300; x += 4) {
        if (inInk(polys, x, y)) { total += 4; cur += 4; run = Math.max(run, cur); } else cur = 0;
      }
      return { total, run };
    };
    const inkWidth = (y) => row(y).total;
    // the lowest and highest rows with a wider run of ink than a drip or a splash has
    let bottom = null, top = null;
    for (let y = sc.baseline + 300; y > capY - 200; y -= 4) if (row(y).run > 110) { bottom = y; break; }
    for (let y = capY - 200; y < sc.baseline; y += 4) if (row(y).run > 150) { top = y; break; }
    assert.ok(Math.abs(bottom - sc.baseline) < 30, `${l.ch}: body ends on the baseline (off by ${bottom - sc.baseline} of 1000)`);
    assert.ok(Math.abs(top - capY) < 45, `${l.ch}: body reaches the cap line (off by ${top - capY} of 1000)`);
    // below the baseline only drips: narrow, and at least one of them
    assert.ok(inkWidth(sc.baseline + 90) > 0, `${l.ch}: a drip runs off it`);
    assert.ok(inkWidth(sc.baseline + 90) < 150, `${l.ch}: only drips below the baseline`);
  }
});

test("logo: outlines are simple and never cross, so either fill rule fills them the same", () => {
  const signed = (poly) => poly.reduce((a, [x, y], i) => { const [u, v] = poly[(i + 1) % poly.length]; return a + x * v - u * y; }, 0) / 2;
  const crosses = (a, b, c, d) => {
    const o = (p, q, r) => (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
    return o(a, b, c) * o(a, b, d) < 0 && o(c, d, a) * o(c, d, b) < 0;
  };
  for (const set of ["ink", "lite"]) {
    const counters = {};
    for (const ch of "SHOAL") {
      // at 1000 px to the em, so a coordinate is a table value
      const polys = Gl.GLYPHS[ch][set].map((flat) => {
        const pts = [];
        for (let i = 0; i < flat.length; i += 2) pts.push([flat[i], flat[i + 1]]);
        return Lo.sampleLoop(Lo.loopCmds(pts)).slice(0, -1);
      });
      assert.ok(polys.length >= 1);
      const outer = polys.reduce((m, p) => (Math.abs(signed(p)) > Math.abs(signed(m)) ? p : m));
      const edges = polys.flatMap((p, pi) => p.map((q, i) => ({ a: q, b: p[(i + 1) % p.length], pi, n: p.length })));
      for (let i = 0; i < edges.length; i++) {
        const e = edges[i];
        for (let j = i + 2; j < edges.length; j++) {
          const f = edges[j];
          if (e.pi === f.pi && j - i === e.n - 1) continue;
          assert.ok(!crosses(e.a, e.b, f.a, f.b), `${ch} ${set}: outline edges ${i} and ${j} cross`);
        }
      }
      // a counter winds against the letter's outline; a detached droplet winds with it
      const against = polys.filter((p) => Math.sign(signed(p)) !== Math.sign(signed(outer)));
      counters[ch] = against.length;
      for (const p of against) assert.ok(Math.abs(signed(p)) > 3000, `${ch} ${set}: a counter is a real opening, not a pinhole`);
    }
    assert.deepEqual(counters, { S: 0, H: 0, O: 1, A: 1, L: 0 }, `${set}: O and A have a counter, the others none`);
  }
  // detached droplets are part of the look, on every letter
  for (const ch of "SHOAL") assert.ok(Gl.GLYPHS[ch].ink.length >= 2, `${ch} has droplets`);
});

test("logo: the blue gradient spans the ink in a box of its own", () => {
  const lg = { x: 100, y: 50, w: 84 * 3.9, h: 84 * 1.25, font: 84 };
  for (const body of [Lo.logoScene(lg).items.find((it) => it.kind === "logo-body"), Lo.logoDraw(lg, false).items[0]]) {
    assert.deepEqual(body.gradient, { angle: 180, from: Lo.LOGO.top, to: Lo.LOGO.bottom });
    const b = O.itemBounds(body);
    assert.ok(b.x0 > -6 && b.y0 > -6, "the body's commands are local to its box");
    assert.ok(b.x1 < body.box.w + 8 && b.y1 < body.box.h + 8, "and stay inside it");
  }
  assert.deepEqual(Lo.logoScene(lg).items.find((it) => it.kind === "logo-body").box, Lo.logoScene(lg).bounds);
});

// ---- what a render draws

const png = readPng(readFileSync(join(MOD, "assets", Lo.LOGO_IMAGE.file)));
const pathCmds = (items) => items.reduce((n, it) => n + it.cmds.length, 0);

test("logo image: the shipped PNG is the frame logo.js places, transparent at its edges, and not large", () => {
  const f = Lo.logoImageFrame();
  assert.deepEqual([png.width, png.height], [f.w, f.h], "assets/logo.png is stale: run preview/gen_logo_image.mjs");
  const alpha = (x, y) => png.rgba[(y * png.width + x) * 4 + 3];
  for (let x = 0; x < png.width; x++) assert.equal(alpha(x, 0) + alpha(x, png.height - 1), 0, "clear along the top and bottom");
  for (let y = 0; y < png.height; y++) assert.equal(alpha(0, y) + alpha(png.width - 1, y), 0, "clear down the sides");
  let soft = 0;
  for (let i = 3; i < png.rgba.length; i += 4) if (png.rgba[i] > 0 && png.rgba[i] < 200) soft++;
  assert.ok(soft > 5000, "a soft shadow, not a hard edge");
  assert.ok(readFileSync(join(MOD, "assets", Lo.LOGO_IMAGE.file)).length < 300 * 1024, "no heavier than it must be");
  // twice the largest logo any window asks for, for a 2x display
  const largest = L.homeLayout({ width: 3840, height: 2160 }, heightsOf(list), { art: true }).logo;
  assert.ok(Lo.logoLetters(largest)[0].font * 2 <= f.pxPerEm, "crisp at the largest size");
});

test("logo image: its pixels land on the vector letters, small and large, and so do the vines' crossings", () => {
  const f = Lo.logoImageFrame();
  const boxes = [[1920, 1200], [1100, 760], [800, 560], [640, 460], [320, 240]]
    .map(([w, h]) => L.homeLayout({ width: w, height: h }, heightsOf(list)).logo);
  boxes.push({ x: 7, y: 3, w: 131, h: 43, font: 34 });
  for (const lg of boxes) {
    const box = Lo.logoImageBox(lg);
    assert.ok(Math.abs(box.w / box.h - f.w / f.h) < 0.01, `font ${lg.font}: the image keeps its shape`);
    const sc = Lo.logoScene(lg);
    assert.ok(box.x < sc.bounds.x && box.y < sc.bounds.y && box.x + box.w > sc.bounds.x + sc.bounds.w && box.y + box.h > sc.bounds.y + sc.bounds.h, `font ${lg.font}: the image holds all of the ink`);
    const px = (x, y) => {
      const i = (Math.floor(((y - box.y) / box.h) * png.height) * png.width + Math.floor(((x - box.x) / box.w) * png.width)) * 4;
      return { blue: png.rgba[i + 2], alpha: png.rgba[i + 3] };
    };
    const bright = (x, y) => { const p = px(x, y); return p.alpha > 240 && p.blue > 150; };
    const dim = (x, y) => { const p = px(x, y); return (p.blue * p.alpha) / 255 < 110; };
    // a grid over the ink: wherever the vector geometry is plainly in or out, the pixel must agree
    const polys = inkPolys(lg);
    const m = 1.5; // px from an edge, where the rim and the antialiasing are
    const near = [[0, 0], [m, 0], [-m, 0], [0, m], [0, -m]];
    const step = Math.max(1, sc.bounds.w / 56);
    let inside = 0, outside = 0, litWater = 0;
    for (let y = sc.bounds.y; y < sc.bounds.y + sc.bounds.h; y += step) {
      for (let x = sc.bounds.x; x < sc.bounds.x + sc.bounds.w; x += step) {
        const hits = near.filter(([dx, dy]) => inInk(polys, x + dx, y + dy)).length;
        if (hits === near.length) { inside++; assert.ok(bright(x, y), `font ${lg.font}: ink at ${x},${y} is bright in the image`); }
        else if (hits === 0) { outside++; if (!dim(x, y)) litWater++; }
      }
    }
    assert.ok(inside > 100 && outside > 100, `font ${lg.font}: the grid saw both (${inside}, ${outside})`);
    // a droplet can sit between the five sample points, and a misplaced image lights far more water than that
    assert.ok(litWater / outside < 0.01, `font ${lg.font}: water is dark in the image (${litWater} of ${outside} lit)`);
  }
  for (const [w, h] of ART_SIZES) {
    const lay = L.homeLayout({ width: w, height: h }, heightsOf(list), { art: true });
    const box = Lo.logoImageBox(lay.logo);
    const bright = (x, y) => {
      const i = (Math.floor(((y - box.y) / box.h) * png.height) * png.width + Math.floor(((x - box.x) / box.w) * png.width)) * 4;
      return i >= 0 && i < png.rgba.length && png.rgba[i + 3] > 240 && png.rgba[i + 2] > 150;
    };
    for (const v of O.oceanScene(lay).vines) {
      const under = v.segs.filter((g) => !v.front.includes(g));
      assert.ok(v.front.some((g) => seg2pts(g).some((p) => bright(p.x, p.y))), `${w}x${h}: ${v.id} crosses over the image's letters`);
      assert.ok(under.some((g) => seg2pts(g).some((p) => bright(p.x, p.y))), `${w}x${h}: ${v.id} crosses under them`);
      for (const g of v.front) assert.ok(!bright(g[0], g[1]) && !bright(g[6], g[7]), `${w}x${h}: ${v.id}'s over-crossing ends in open water in the image too`);
    }
  }
});

test("logo: the fallback is the body alone, light, on the same baseline, inside the box", () => {
  for (const lg of LOGO_BOXES) {
    const fb = Lo.logoDraw(lg, false);
    assert.equal(fb.image, null);
    assert.equal(fb.items.length, 1, "one path");
    const [body] = fb.items;
    assert.ok(finitePts(body.cmds));
    assert.equal(body.cmds.filter((c) => c[0] === "M").length, 7, `font ${lg.font}: five letters and the counters of O and A, and nothing else`);
    assert.ok(body.cmds.length < 300, `font ${lg.font}: light (${body.cmds.length} commands)`);
    const b = O.itemBounds(body);
    assert.ok(body.box.x + b.x0 >= lg.x - 4 && body.box.x + b.x1 <= lg.x + lg.w + 4, `font ${lg.font}: inside the box horizontally`);
    assert.ok(body.box.y + b.y0 >= lg.y - 4 && body.box.y + b.y1 <= lg.y + lg.h + 4, `font ${lg.font}: inside the box vertically`);
    const withImage = Lo.logoDraw(lg, true);
    assert.deepEqual(withImage.items, [], "with the image, no path at all");
    assert.deepEqual({ ...withImage.image, file: undefined }, { ...Lo.logoImageBox(lg), file: undefined });
    assert.equal(withImage.image.file, Lo.LOGO_IMAGE.file);
  }
  // the fallback's letters are the image's letters, so a vine crosses them the same way: its counters and feet agree
  const lg = { x: 0, y: 0, w: 1000 * 3.9, h: 1000 * 1.25, font: 1000 };
  const body = Lo.logoDraw(lg, false).items[0];
  const fx = body.box.x, fy = body.box.y;
  const sampled = [];
  let cur = null;
  for (const c of body.cmds) { if (c[0] === "M") { cur = [c]; sampled.push(cur); } else cur.push(c); }
  const polys = sampled.map((c) => Lo.sampleLoop(c).map(([x, y]) => [x + fx, y + fy]));
  const full = inkPolys(lg);
  let same = 0, n = 0;
  for (let y = 0; y < lg.h; y += 25) {
    for (let x = 0; x < lg.w; x += 25) { n++; if (inInk(polys, x, y) === inInk(full, x, y)) same++; }
  }
  assert.ok(same / n > 0.97, `the light outline follows the full one (${((100 * same) / n).toFixed(1)} percent agree)`);
});

// Each command is a native PathBuilder call, and a render has the mod engine's time limit. Measured with `herdr-app
// check`, 24 runs each at the default window, extra commands added to its render: about 2800 never failed, 3500 failed
// twice, 4150 every time. The heaviest render is the one before the asset check: the sea, the vines in front, the fallback logo,
// the background and the bloom. 1350 is that render's total at the largest window and a little room, about half the
// highest count that never failed. The limit is on wall-clock time, so a starved CPU can trip it at any count.
const FIRST_RENDER_CMDS = 1350;

test("first render: the path commands stay under the budget at every window size", () => {
  const src = readFileSync(join(MOD, "main.js"), "utf8");
  assert.match(src, /logoOf\(L\.logo, this\.art === true\)/, "only a confirmed image is drawn; a null art check gets the fallback");
  assert.doesNotMatch(src, /logoScene/, "the full vector logo is never drawn on a render");
  const background = 2 * 5 + 6 * 4; // main.js: two filled rects, six ellipses
  let worst = 0;
  for (const [w, h] of [[640, 460], [1100, 760], [1425, 860], [1920, 1200], [2560, 1440], [3840, 2160]]) {
    for (const hs of [[], heightsOf(list)]) {
      const lay = L.homeLayout({ width: w, height: h }, hs, { art: true });
      const sea = O.oceanScene(lay);
      const total = background + pathCmds(sea?.items ?? []) + pathCmds(sea?.front ?? []) + pathCmds(Lo.logoDraw(lay.logo, false).items);
      worst = Math.max(worst, total);
      assert.ok(total <= FIRST_RENDER_CMDS, `${w}x${h}: ${total} path commands on a first render, budget ${FIRST_RENDER_CMDS}`);
    }
  }
  assert.ok(worst > 800, "the count is of something real");
  // the render `herdr-app check` actually reaches has the image: it costs the logo's whole share less
  const after = L.homeLayout({ width: 3840, height: 2160 }, heightsOf(list), { art: true });
  assert.equal(pathCmds(Lo.logoDraw(after.logo, true).items), 0, "once the image is known, the logo costs no path at all");
});

test("scenes are worked out once per layout, and again on a resize", () => {
  let calls = 0;
  const scene = O.lastResult((lay) => { calls++; return O.oceanScene(lay); });
  const a = L.homeLayout({ width: 1150, height: 770 }, heightsOf(list), { art: true });
  const first = scene(a);
  assert.equal(scene(L.homeLayout({ width: 1150, height: 770 }, heightsOf(list), { art: true })), first, "the same layout, the same object");
  assert.equal(calls, 1);
  const b = L.homeLayout({ width: 1425, height: 860 }, heightsOf(list), { art: true });
  assert.deepEqual(scene(b), O.oceanScene(b), "a resize works it out for the new size");
  assert.equal(calls, 2);
  assert.deepEqual(scene(a), O.oceanScene(a), "and back again");
  const draw = O.lastResult(Lo.logoDraw);
  assert.equal(draw(a.logo, false).image, null, "the art flag is part of the key");
  assert.ok(draw(a.logo, true).image);
  const src = readFileSync(join(MOD, "main.js"), "utf8");
  assert.match(src, /const seaOf = lastResult\(oceanScene\)/);
  assert.match(src, /const logoOf = lastResult\(logoDraw\)/);
});

test("logo: the glyph table is what the generator draws", () => {
  const dir = mkdtempSync(join(tmpdir(), "shoal-glyphs-"));
  try {
    const out = join(dir, "glyphs.js");
    execFileSync(process.execPath, [join(PREVIEW, "gen_logo_glyphs.mjs"), "--out", out], { stdio: "ignore" });
    assert.equal(readFileSync(out, "utf8"), readFileSync(join(MOD, "glyphs.js"), "utf8"), "glyphs.js is stale: run preview/gen_logo_glyphs.mjs");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
