// test_shoal_board.mjs: the shoal-board Herdr app mod's pure module.
// Node only, no dependencies: node --test tests/herdr_app/*.mjs (or node this file)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "..", "plugins", "shoal-herdr-app", "mods", "shoal-board");
const M = await import(join(MOD, "model.js"));
const modJson = JSON.parse(readFileSync(join(MOD, "mod.json"), "utf8"));

const node = (key, o = {}) => ({ key, item: key, repo: `example/${key.split("#")[0]}`, number: Number(key.split("#")[1]),
  title: `title of ${key}`, url: `https://github.com/example/${key.replace("#", "/issues/")}`, work: "unqueued", role: "",
  pos: 0, blocker: "", parent: "", blocked_by: [], sub_issues: 0, labels: [], assignees: [], pos_board: 0, ...o });
const BOARD = {
  id: "example/1", title: "Example board", open: 6, done: 4, closed: false,
  nodes: [
    node("app#1", { sub_issues: 2, pos_board: 1 }),
    node("app#2", { parent: "app#1", work: "active", state: "in-progress", why: "working", role: "core", pos_board: 2 }),
    node("app#3", { parent: "app#1", work: "queued", state: "queued", role: "core", pos: 1, pos_board: 3 }),
    node("app#4", { blocked_by: ["app#3"], work: "queued", state: "blocked", why: "hold", role: "docs", pos: 2, blocker: "hold: waits on #3", pos_board: 4 }),
    node("app#5", { pos_board: 5 }),
    node("app#6", { work: "queued", state: "queued", role: "docs", pos: 1, pos_board: 6 }),
  ],
  edges: [{ from: "app#1", to: "app#2", kind: "parent" }, { from: "app#1", to: "app#3", kind: "parent" },
          { from: "app#3", to: "app#4", kind: "blocks" }],
  metrics: { open: 6, done: 4, total: 10, pct_done: 40, in_progress: 1, queued: 2, blocked: 1, blocked_hold: 1, unqueued: 2, dep_blocked: 1,
             epics: 1, closed_7d: 3, close_rate_7d: 0.43, open_change_14d: -2, eta_days: 14,
             series: { days: 14, open: [null, null, 8, 8, 7, 6], closed: [0, 0, 0, 1, 1, 1] } },
};
const DOC = { v: 1, kind: "shoal-board", shoals: [
  { id: "alpha-20261101-000000", tag: "alpha", roles: [{ slug: "core" }, { slug: "docs" }], boards: [BOARD] },
  { id: "beta-20261101-000000", tag: "beta", roles: [], boards: [] },
] };

test("a tab mod that takes the keyboard", () => {
  assert.equal(modJson.place, "none");
  assert.equal(modJson.focusable, true);
});

test("the reply and the view: a failed read keeps the last; the shoal asked for, else the first with a board", () => {
  assert.equal(M.parseReply({ code: 1, stdout: "{}" }), null);
  assert.equal(M.parseReply({ code: 0, stdout: "{\"kind\":\"other\",\"shoals\":[]}" }), null);
  assert.equal(M.parseReply({ code: 0, stdout: JSON.stringify(DOC) }).shoals.length, 2);
  assert.deepEqual(M.boardArgv({}), ["shoal", "ui", "board", "--json"]);
  assert.equal(M.pickView(DOC).board.id, "example/1");
  assert.equal(M.pickView(DOC, { shoal: "beta-20261101-000000" }).shoal.tag, "beta");
  assert.equal(M.pickView(DOC, { shoal: "beta-20261101-000000" }).board, null);
  assert.equal(M.pickView(DOC, { shoal: "nope" }).shoal.tag, "alpha", "an unknown shoal: the first with a board");
  assert.deepEqual(M.pickView(null), { shoal: null, board: null });
});

test("layers: prerequisites below, a sub-issue under its parent and a blocker under what it blocks; a cycle still gets a layer", () => {
  const { layer } = M.layers(BOARD);
  assert.equal(layer.get("app#1"), 1);
  assert.equal(layer.get("app#2"), 2);
  assert.equal(layer.get("app#4"), 1, "it waits on #3, so it sits above it, off the root");
  assert.equal(layer.get("app#3"), 2, "under its parent and under the item it blocks");
  // a foundation blocking several items sits under all of them, as the five above #864 on a live board
  const fan = { nodes: ["f#1", "f#2", "f#3", "f#9"].map((k) => node(k)),
                edges: ["f#1", "f#2", "f#3"].map((k) => ({ from: "f#9", to: k, kind: "blocks" })) };
  const lf = M.layers(fan).layer;
  assert.deepEqual(["f#1", "f#2", "f#3", "f#9"].map((k) => lf.get(k)), [1, 1, 1, 2]);
  // a sub-issue that blocks its parent is consistent now (it was a cycle when a blocker sat above)
  const sub = { nodes: [node("s#1"), node("s#2", { parent: "s#1" })],
                edges: [{ from: "s#1", to: "s#2", kind: "parent" }, { from: "s#2", to: "s#1", kind: "blocks" }] };
  assert.deepEqual([M.layers(sub).layer.get("s#1"), M.layers(sub).layer.get("s#2")], [1, 2]);
  const cyc = { nodes: [node("a#1"), node("a#2")], edges: [{ from: "a#1", to: "a#2", kind: "blocks" }, { from: "a#2", to: "a#1", kind: "blocks" }] };
  const l = M.layers(cyc).layer;
  assert.ok(l.get("a#1") >= 1 && l.get("a#2") >= 1);
});

test("layout: the board at the top, the tree below it, items in no dependency in their own grid", () => {
  const lay = M.layout(BOARD, 1200);
  const at = new Map(lay.nodes.map((n) => [n.key, n]));
  assert.ok(lay.root.y < at.get("app#1").y && at.get("app#1").y < at.get("app#2").y && at.get("app#4").y < at.get("app#3").y);
  assert.ok(at.get("app#5").loose && at.get("app#6").loose && !at.get("app#1").loose);
  assert.equal(lay.loose.count, 2);
  assert.ok(at.get("app#6").x < at.get("app#5").x, "in the loose grid, queued before not queued");
  assert.ok(lay.loose.y > at.get("app#4").y, "the grid comes after the graph");
  // edges: the root to the tree's top, the parent edges, the blocker edge; none to a loose item
  assert.deepEqual(lay.edges.map((e) => `${e.kind}:${e.from}>${e.to}`).sort(),
    ["blocks:app#3>app#4", "parent:app#1>app#2", "parent:app#1>app#3", `root:${M.ROOT_KEY}>app#1`, `root:${M.ROOT_KEY}>app#4`].sort());
  // the blocker's edge rises from its top into the bottom of what it unblocks, the arrowhead pointing up
  const e = lay.edges.find((x) => x.kind === "blocks");
  assert.equal(e.dir, "up");
  assert.equal(e.y1, at.get("app#3").y);
  assert.equal(e.y2, at.get("app#4").y + M.NODE.h);
  assert.equal(M.edgeCmds(e)[0][0], "M");
  const head = M.arrowCmds(e);
  assert.deepEqual(head.at(-1), ["Z"]);
  assert.ok(head[0][2] > e.y2 && head[1][2] === e.y2, "the head's base below its tip");
  assert.ok(lay.edges.filter((x) => x.kind !== "blocks").every((x) => x.dir === "down"), "tree edges run down");
  // a narrow canvas wraps a wide layer into rows, never wider than it needs
  const wide = { nodes: Array.from({ length: 9 }, (_, i) => node(`w#${i + 2}`, { parent: "w#1" })).concat([node("w#1")]),
                 edges: Array.from({ length: 9 }, (_, i) => ({ from: "w#1", to: `w#${i + 2}`, kind: "parent" })) };
  const narrow = M.layout(wide, 600);
  assert.ok(new Set(narrow.nodes.filter((n) => n.key !== "w#1").map((n) => n.y)).size > 1, "wrapped");
  assert.ok(narrow.nodes.every((n) => n.x + M.NODE.w <= narrow.width));
});

test("styles: in progress (and what runs), queued with its place, blocked on what, not queued, off board", () => {
  const c = M.palette({});
  const s = (o) => M.nodeStyle(node("x#1", o), c);
  assert.equal(s({ state: "in-progress", why: "working", role: "core" }).badge, "▶ core");
  assert.equal(s({ state: "in-progress", why: "ci", role: "core" }).badge, "▶ CI · core", "what runs comes first: the role may be cut");
  assert.equal(s({ state: "queued", role: "core", pos: 2 }).badge, "core #2");
  const onIssue = s({ state: "blocked", why: "issue", on: "x#9", role: "docs", pos: 1 });
  assert.ok(!onIssue.dashed && onIssue.badge === "⛔ #9" && onIssue.border === c.blocked, "an issue in its own repo reads #n");
  assert.equal(s({ state: "blocked", why: "issue", on: "lib#9" }).badge, "⛔ lib#9");
  assert.equal(s({ state: "blocked", why: "maintainer" }).badge, "⛔ maintainer");
  const hold = s({ state: "blocked", why: "hold" });
  assert.ok(hold.dashed && hold.badge === "⛔ hold", "a free-text hold is dashed: it names nothing");
  assert.equal(s({}).badge, "not queued");
  assert.equal(s({ off_board: true }).badge, "off board");
  assert.notEqual(s({ state: "in-progress" }).border, s({ state: "queued" }).border);
  // a board from a machine whose shoal predates `state` reads by `work`
  assert.equal(M.stateOf({ work: "queued-blocked" }), "blocked");
  assert.equal(M.stateOf({ work: "active" }), "in-progress");
  assert.equal(M.stateOf({}), "not-queued");
});

test("state text: in progress and on what, blocked and on what", () => {
  const t = (o) => M.stateText(node("app#1", o));
  assert.equal(t({ state: "in-progress", why: "working" }), "in progress");
  assert.equal(t({ state: "in-progress", why: "ci" }), "in progress: CI");
  assert.equal(t({ state: "in-progress", why: "window" }), "in progress: research window");
  assert.equal(t({ state: "blocked", why: "issue", on: "app#923" }), "blocked on #923");
  assert.equal(t({ state: "blocked", why: "issue", on: "lib#5" }), "blocked on lib#5");
  assert.equal(t({ state: "blocked", why: "upstream", on: "lib#79" }), "blocked upstream: lib#79");
  assert.equal(t({ state: "blocked", why: "maintainer" }), "blocked: maintainer");
  assert.equal(t({ state: "blocked", why: "hold" }), "blocked: free-text hold");
  assert.equal(t({ state: "not-queued" }), "not queued");
  const inferred = Object.fromEntries(M.hoverLines(node("app#1", { state: "in-progress", why: "ci", inferred: true })));
  assert.equal(inferred.status, "in progress: CI (read from its hold)");
});

test("hover lines: only what the item has", () => {
  const lines = Object.fromEntries(M.hoverLines(BOARD.nodes[3]));
  assert.equal(lines.status, "blocked: free-text hold");
  assert.equal(lines.role, "docs, #2 in its queue");
  assert.equal(lines.blocker, "hold: waits on #3");
  assert.equal(lines["blocked by"], "app#3");
  assert.ok(!("assignees" in lines) && !("milestone" in lines));
});

test("actions: open it; offer an item no role holds; move one another role holds; never to its own role", () => {
  const roles = DOC.shoals[0].roles;
  const free = M.itemActions(BOARD.nodes[4], roles);
  assert.equal(free[0].kind, "open");
  assert.deepEqual(free.slice(1).map((a) => [a.kind, a.role, a.item]), [["offer", "core", "example/app#5:fix"], ["offer", "docs", "example/app#5:fix"]]);
  const held = M.itemActions(BOARD.nodes[2], roles);
  assert.deepEqual(held.slice(1).map((a) => [a.kind, a.role, a.pick]), [["move", "docs", "app#3"]]);
  assert.deepEqual(M.itemActions(node("x#1", { off_board: true, url: "" }), roles), []);
  const o = M.actionOverlay(free[1], "alpha-20261101-000000", "lane-a");
  assert.deepEqual(o.props, { shoal: "alpha-20261101-000000", role: "core", verb: "offer", machine: "lane-a", item: "example/app#5:fix" });
  assert.equal(M.actionOverlay(held[1], "s").props.pick, "app#3");
  assert.equal(M.offerItem(node("app#9", { kind: "pr" })), "example/app#9:review");
});

test("metrics: tiles and sparklines", () => {
  const t = Object.fromEntries(M.tiles(BOARD.metrics).map((x) => [x.id, x]));
  assert.equal(t.open.note, "-2 in 14 days");
  assert.equal(t.done.value, "40%");
  assert.equal(t.blocked.value, "1");
  assert.equal(t.blocked.note, "1 on a free-text hold");
  assert.equal(t.active.value, "1");
  // a board from a machine whose shoal predates `state`
  const legacy = Object.fromEntries(M.tiles({ ...BOARD.metrics, in_progress: undefined, blocked: undefined, active: 3, queued_blocked: 2 }).map((x) => [x.id, x]));
  assert.deepEqual([legacy.active.value, legacy.blocked.value], ["3", "2"]);
  assert.equal(t.eta.value, "14 d");
  assert.equal(M.tiles({ ...BOARD.metrics, eta_days: null, close_rate_7d: 0 }).find((x) => x.id === "eta").value, "–");
  const pts = M.sparkPoints(BOARD.metrics.series.open, 100, 40);
  assert.equal(pts.length, 4, "nulls skipped");
  assert.ok(pts.every(([x, y]) => x >= 0 && x <= 100 && y >= 0 && y <= 40));
  assert.deepEqual(M.sparkPoints([null, 3], 100, 40), []);
});

test("the root box holds its title and counts lines in both modes (a 48 px nodes root clipped the title)", () => {
  // 7 px padding and a 2 px border on each side, a 13 px and an 11 px line at about 1.4 line height, a 2 px gap
  const need = 2 * 7 + 2 * 2 + Math.ceil(13 * 1.4) + Math.ceil(11 * 1.4) + 2;
  for (const mode of ["cards", "nodes"]) assert.ok(M.DIMS[mode].rootH >= need, `${mode} rootH ${M.DIMS[mode].rootH} < ${need}`);
});

test("zoom: every length scales, the steps stay in range, Fit finds a zoom that holds the whole tree", () => {
  const d = M.scaleDims(M.DIMS.cards, 0.5);
  assert.equal(d.w, M.DIMS.cards.w / 2);
  assert.equal(d.rootH, M.DIMS.cards.rootH / 2);
  assert.equal(d.scale, 0.5);
  assert.ok(M.stepZoom(1, 1) > 1 && M.stepZoom(1, -1) < 1);
  assert.equal(M.stepZoom(M.ZOOM.max, 1), M.ZOOM.max);
  assert.equal(M.stepZoom(M.ZOOM.min, -1), M.ZOOM.min);
  const big = { nodes: Array.from({ length: 40 }, (_, i) => node(`z#${i + 2}`, { parent: "z#1" })).concat([node("z#1")]),
                edges: Array.from({ length: 40 }, (_, i) => ({ from: "z#1", to: `z#${i + 2}`, kind: "parent" })) };
  const z = M.fitZoom(big, 900, 500);
  assert.ok(z < 1 && z >= M.ZOOM.min, String(z));
  const lay = M.layout(big, 900, M.scaleDims(M.DIMS.cards, z));
  assert.ok(lay.height <= 500, `${lay.height}`);
  assert.equal(M.fitZoom(BOARD, 1400, 2000), 1, "a tree that fits is not shrunk");
});

test("nodes: a shape per repo by first appearance, closed outlines, a smaller cell than a card", () => {
  const mixed = { nodes: [node("app#1"), node("lib#2"), node("app#3"), node("doc#4")] };
  assert.deepEqual(M.repoShapes(mixed), { app: "circle", lib: "square", doc: "diamond" });
  const seven = { nodes: Array.from({ length: 7 }, (_, i) => node(`r${i}#1`)) };
  assert.equal(new Set(Object.values(M.repoShapes(seven))).size, 7, "seven repos, seven shapes");
  for (const shape of ["circle", "square", "diamond", "triangle", "hexagon", "pentagon", "octagon", "star"]) {
    const cmds = M.shapeCmds(shape, 50, 50, 10);
    assert.equal(cmds[0][0], "M");
    assert.deepEqual(cmds.at(-1), ["Z"], shape);
    const xs = cmds.flatMap((c) => (c[0] === "Z" ? [] : c.slice(1).filter((_, i) => i % 2 === 0)));
    assert.ok(xs.every((x) => x >= 39 && x <= 61), shape);
  }
  const lay = M.layout(BOARD, 1200, M.DIMS.nodes);
  assert.equal(lay.dims, M.DIMS.nodes);
  assert.ok(M.DIMS.nodes.w < M.DIMS.cards.w / 2);
  const n = lay.nodes.find((x) => x.key === "app#2");
  const o = M.shapeCentre(n, lay.dims);
  assert.ok(o.x === n.x + lay.dims.w / 2 && o.y > n.y && o.y < n.y + lay.dims.h);
});

test("public gate: the module holds no local paths or host names", () => {
  const text = readFileSync(join(MOD, "model.js"), "utf8") + readFileSync(join(MOD, "main.js"), "utf8");
  assert.ok(!/\/home\/|\/mnt\/|speedie/i.test(text));
});
