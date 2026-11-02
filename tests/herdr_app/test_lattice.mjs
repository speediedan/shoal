// test_lattice.mjs: the shoal-lattice Herdr app mod's pure modules against the bundled fixture.
// Node only, no dependencies: node tests/herdr_app/test_lattice.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "..", "plugins", "shoal-herdr-app", "mods", "shoal-lattice");
const M = await import(join(MOD, "model.js"));
const L = await import(join(MOD, "layout.js"));
const G = await import(join(MOD, "geom.js"));
const S = await import(join(MOD, "scene.js"));
const doc = JSON.parse(readFileSync(join(MOD, "fixtures", "graph-v1.json"), "utf8"));

const A = "alpha-20261101-000000";
const B = "beta-20261101-000000";
let n = 0;
function test(name, fn) {
  try {
    fn();
    n++;
    console.log(`ok ${n} ${name}`);
  } catch (e) {
    console.error(`FAIL ${name}\n${e.stack}`);
    process.exit(1);
  }
}
const finiteDeep = (v) => {
  if (typeof v === "number") return Number.isFinite(v);
  if (Array.isArray(v)) return v.every(finiteDeep);
  if (v && typeof v === "object") return Object.values(v).every(finiteDeep);
  return true;
};

const global = M.buildModel(doc, "global");
const alpha = M.buildModel(doc, A);
const vis = (vm, id) => vm.byId.get(id).visual;

test("node visual classes per state", () => {
  const exp = {
    [`${A}:core`]: "working", [`${A}:dist-ci`]: "local-ci", [`${A}:bench`]: "remote-ci", [`${A}:tune`]: "prompt",
    [`${A}:integration`]: "auth", [`${A}:addressing`]: "self", [`${A}:registry`]: "unknown", [`${A}:scribe`]: "handover",
    [`${B}:research`]: "peer", [`${B}:analysis`]: "upstream", [`${B}:archive`]: "excluded", [`${B}:pool`]: "lease", [`${B}:drift`]: "other",
  };
  for (const [id, key] of Object.entries(exp)) assert.equal(vis(global, id).key, key, id);
  const auth = vis(global, `${A}:integration`);
  assert.equal(auth.color, M.PALETTE.auth);
  assert.equal(auth.urgency, 0);
  assert.ok(auth.ring && auth.halo.period <= 700, "auth pulses fastest");
  for (const [id, v] of global.byId) if (v.visual.halo && v.kind === "role" && id !== `${A}:integration`) assert.ok(v.visual.halo.period > auth.halo.period, `${id} pulses slower than auth`);
  assert.equal(vis(global, `${B}:research`).border, "dashed");
  assert.equal(vis(global, `${A}:registry`).border, "dotted");
  assert.equal(vis(global, `${A}:addressing`).fill, 0);
  assert.ok(vis(global, `${A}:dist-ci`).hatch > 0 && vis(global, `${B}:analysis`).hatch > 0);
  assert.equal(vis(global, `${B}:research`).shape, "hexagon");
  assert.equal(vis(global, `${A}:addressing`).shape, "diamond");
  assert.equal(vis(global, `${A}:core`).shape, "circle");
});

test("every salient wait is distinguishable", () => {
  const keys = ["auth", "ask", "prompt", "design", "assign", "sources", "local-ci", "remote-ci", "lease", "upstream", "peer", "self", "handover", "refill", "hold", "other"];
  const sig = (k) => { const s = M.STATE_STYLE[k]; return [s.color, s.badge ?? "", s.hatch ?? 0, s.border ?? "", s.fill].join("|"); };
  assert.equal(new Set(keys.map(sig)).size, keys.length, "each wait has its own encoding");
  const l = M.STATE_STYLE["local-ci"], r = M.STATE_STYLE["remote-ci"];
  assert.notEqual(l.badge, r.badge);
  assert.notEqual(l.hatch, r.hatch);
  assert.notEqual(l.color, r.color);
});

test("unknown event and class are tolerated", () => {
  const drift = global.byId.get(`${B}:drift`);
  assert.equal(drift.visual.key, "other");
  assert.match(drift.visual.label, /warmup-v6/, "raw event name shown");
  assert.ok(M.cardLines(global, drift.id).rows.some(([, v]) => v.includes("warmup-v6")));
  assert.equal(M.stateKey({ state: { class: "brand-new-class" } }), "unknown");
  assert.equal(M.stateKey({ state: { class: "wait", event: "", group: "system" } }), "system");
  assert.equal(M.stateKey(null), "unknown");
  assert.doesNotThrow(() => M.nodeVisual({}));
  assert.doesNotThrow(() => M.nodeVisual({ kind: 7, state: { class: 3, event: {} } }));
  for (const bad of [null, {}, { nodes: "x", edges: 5 }, { nodes: [{ id: "a", state: null }], edges: [{ src: "a", dst: "b" }] }]) {
    const vm = M.buildModel(bad, "global");
    assert.ok(Array.isArray(vm.nodes));
    assert.doesNotThrow(() => L.layoutGraph(vm));
  }
  assert.ok(M.legendEntries(global).states.some((s) => s.key === "other"));
});

test("needs-you ordering: auth > ask/prompt > design > assign > stalls", () => {
  assert.deepEqual(global.needsYou.map((r) => r.id), [`${A}:integration`, `${A}:tune`, `${A}:registry`]);
  const mk = (id, cls, event, since) => ({ id, kind: "role", state: { class: cls, event, since_s: since } });
  const rows = M.needsYouList([
    mk("stall", "unknown", "", 9999), mk("design", "wait", "user-design", 10), mk("assign", "wait", "assignment", 50),
    mk("ask", "wait", "user-ask", 5), mk("prompt", "wait", "user-prompt", 500), mk("auth", "wait", "user-auth", 1), mk("busy", "work", "turn", 1),
  ]);
  assert.deepEqual(rows.map((r) => r.id), ["auth", "prompt", "ask", "design", "assign", "stall"]);
});

test("edge bucketing by color x tier (and cross-host dash)", () => {
  const buckets = M.edgeBuckets(global.edges);
  const seen = new Map();
  for (const b of buckets) for (const e of b.edges) { assert.ok(!seen.has(e.id), "one bucket per edge"); seen.set(e.id, b); }
  assert.equal(seen.size, global.edges.length);
  assert.equal(new Set(buckets.map((b) => b.key)).size, buckets.length);
  for (const b of buckets) for (const e of b.edges) { assert.equal(e.cls, b.cls); assert.equal(e.color, b.color); }
  const cross = alpha.edges.find((e) => e.id === `${A}:core|${A}:scribe|msg`);
  assert.ok(cross.cross, "host-a -> host-b edge is cross-host");
  assert.deepEqual(seen.get(cross.id).dash, M.CROSS_HOST_DASH);
  assert.equal(seen.get(`${A}:core|${A}:dist-ci|msg`).dash, null);
  const keep = M.incidentEdges(global, `${A}:tune`);
  const faded = M.edgeBuckets(global.edges, keep);
  assert.ok(faded.some((b) => b.tier === "fade"));
  assert.ok(faded.filter((b) => b.tier !== "fade").every((b) => b.edges.every((e) => keep.has(e.id))));
  assert.ok(global.edges.some((e) => e.flow) && global.edges.filter((e) => e.flow).every((e) => e.tier === "hot" || e.tier === "warm"));
  assert.ok(global.broadcasts.some((b) => b.src === `${A}:core`), "broadcast is a ripple, not an edge");
  assert.ok(!global.edges.some((e) => e.cls === "broadcast"));
});

test("blocked-on edges present; alias edge in global view only", () => {
  const bo = (vm) => vm.edges.filter((e) => e.cls === "blocked-on").map((e) => e.id).sort();
  assert.deepEqual(bo(global), [`${A}:core|${A}:dist-ci|blocked-on`, `${B}:research|${B}:analysis|blocked-on`]);
  assert.deepEqual(bo(alpha), [`${A}:core|${A}:dist-ci|blocked-on`]);
  assert.ok(global.edges.some((e) => e.cls === "alias"));
  assert.ok(!alpha.edges.some((e) => e.cls === "alias"));
  assert.ok(!M.buildModel(doc, B).edges.some((e) => e.cls === "alias"));
  assert.ok(alpha.byId.has("ext:research") && alpha.byId.has("user"), "per-shoal keeps its externals and the hub");
  assert.ok(!alpha.byId.has(`${B}:analysis`));
});

test("layout determinism (same input, same positions)", () => {
  for (const vm of [global, alpha]) {
    const a = L.layoutGraph(vm), b = L.layoutGraph(M.buildModel(doc, vm.view));
    for (const [id, p] of a.pos) assert.deepEqual(p, b.pos.get(id), id);
    assert.deepEqual(a.sats, b.sats);
  }
});

test("warm start is stable and nothing is NaN", () => {
  for (const vm of [global, alpha]) {
    const a = L.layoutGraph(vm);
    const b = L.layoutGraph(vm, a.pos);
    let max = 0;
    for (const [id, p] of a.pos) max = Math.max(max, G.dist(p, b.pos.get(id)));
    assert.ok(max < 1, `warm start moved ${max}`);
    assert.ok(finiteDeep([...a.pos.values()]) && finiteDeep(a.sats) && finiteDeep(a.tethers.map((t) => [t.from, t.to])));
    const ids = [...a.pos.keys()];
    for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) assert.ok(G.dist(a.pos.get(ids[i]), a.pos.get(ids[j])) > 10, `${ids[i]} overlaps ${ids[j]}`);
    // a new node warm-starts the rest: the old ones stay near
    const l = new L.Layout();
    l.update(vm).run();
    const before = new Map([...l.pos].map(([k, p]) => [k, { ...p }]));
    const doc2 = structuredClone(doc);
    doc2.nodes.push({ id: `${A}:extra`, kind: "role", shoal: A, slug: "extra", label: "extra", host: "host-a", state: { class: "idle" } });
    l.update(M.buildModel(doc2, vm.view)).run();
    assert.ok(finiteDeep([...l.pos.values()]));
    assert.ok(l.pos.has(`${A}:extra`));
    for (const [k, p] of before) assert.ok(G.dist(p, l.pos.get(k)) < 220, `${k} jumped`);
  }
});

test("hosts: tags, bands, job-host tethers, summaries", () => {
  for (const n of global.nodes.filter((x) => x.kind === "role")) assert.ok(n.hostColor, `${n.id} has a host tag`);
  assert.equal(global.hostColor.get("host-a"), M.HOST_COLORS[0]);
  const bench = global.byId.get(`${A}:bench`);
  assert.equal(bench.jobHost, "host-b");
  assert.equal(global.byId.get(`${A}:core`).jobHost, null);
  const g = L.layoutGraph(global), a = L.layoutGraph(alpha);
  assert.deepEqual(g.tethers.map((t) => [t.id, t.host]), [[bench.id, "host-b"]]);
  assert.ok(g.anchors.get("host-b").marker);
  assert.deepEqual(a.bands.map((b) => b.host).sort(), ["host-a", "host-b"]);
  assert.ok(a.anchors.get("host-b").band);
  const hs = new Map(M.hostSummaries(global).map((h) => [h.name, h]));
  assert.equal(hs.get("host-a").home, true);
  assert.equal(hs.get("host-b").jobsIn, 1);
  assert.equal(hs.get("host-a").roles + hs.get("host-b").roles, global.nodes.filter((x) => x.kind === "role").length);
  assert.ok(M.legendEntries(global).hosts.length === 2);
});

test("grants: role badge and operator count", () => {
  assert.equal(vis(global, `${A}:integration`).grants, 1);
  assert.equal(vis(global, "user").grants, 1);
  assert.equal(vis(global, "user").badge, "4", "3 asks + 1 grant");
  assert.equal(global.shoals.find((s) => s.id === A).grants, 1);
  const sc = S.buildScene(global, L.layoutGraph(global), { width: 1000, height: 700, tf: G.identity() });
  assert.ok(sc.items.some((i) => i.id === `grant-bg-${A}:integration`));
});

test("scene: layers, encodings, hit testing, no NaN", () => {
  for (const vm of [global, alpha]) {
    const lay = L.layoutGraph(vm);
    const tf = G.fitTransform(G.boundsOf([...lay.pos.values()], 40), { width: 1000, height: 700 });
    const packets = [{ seq: 1, kind: "packet", src: `${A}:core`, dst: `${A}:dist-ci`, color: "#ffffff", phase: "go", ms: 900 }];
    const sc = S.buildScene(vm, lay, { width: 1000, height: 700, tf, hover: `${A}:tune`, packets });
    assert.ok(finiteDeep(sc.items), "no NaN in the display list");
    assert.equal(sc.items[0].id, "bg");
    const ids = sc.items.map((i) => i.id);
    assert.equal(new Set(ids).size, ids.length, "unique element ids");
    assert.ok(sc.items.some((i) => i.t === "box" && i.loops?.some((l) => l.prop === "opacity")), "native halo loops");
    assert.ok(sc.items.some((i) => i.frames), "marching dots use frames");
    assert.ok(sc.items.some((i) => i.id === "pk-1" && i.transitions.some((t) => t.prop === "left")));
    const hatches = sc.items.filter((i) => i.bg?.kind === "slash").map((i) => i.bg.interval);
    assert.ok(new Set(hatches).size >= 2, "hatch steps differ");
    for (const h of sc.hits) assert.equal(G.hitNode(sc.hits, { x: h.x + 1, y: h.y - 1 }), h.id);
    assert.equal(G.hitNode(sc.hits, { x: -500, y: -500 }), null);
    assert.ok(sc.items.some((i) => i.id === `tether-${A}:bench`));
  }
  const sc = S.buildScene(global, L.layoutGraph(global), { width: 1000, height: 700, tf: G.identity() });
  assert.ok(sc.discs.length === 2 && G.hitDisc(sc.discs, sc.discs[1]) === sc.discs[1].id);
  assert.ok(S.buildScene(global, L.layoutGraph(global), { width: 800, height: 600, tf: G.identity(), motion: "off" }).items.every((i) => !i.frames && !i.loops?.length));
});

test("geometry: pan/zoom round trip, fit, curves", () => {
  const tf = { k: 1.7, tx: 33, ty: -12 };
  const p = { x: 123.4, y: -56.7 };
  const back = G.toWorld(tf, G.toScreen(tf, p));
  assert.ok(Math.abs(back.x - p.x) < 1e-9 && Math.abs(back.y - p.y) < 1e-9);
  const anchor = { x: 400, y: 300 };
  const w0 = G.toWorld(tf, anchor);
  const z = G.zoomAt(tf, anchor, G.wheelFactor(-120));
  const w1 = G.toWorld(z, anchor);
  assert.ok(z.k > tf.k && Math.abs(w0.x - w1.x) < 1e-9 && Math.abs(w0.y - w1.y) < 1e-9, "zoom keeps the anchor");
  assert.equal(G.zoomAt(tf, anchor, 1e9).k, 4);
  const panned = G.panBy(tf, 10, -5);
  assert.deepEqual(G.toScreen(panned, p), { x: G.toScreen(tf, p).x + 10, y: G.toScreen(tf, p).y - 5 });
  const b = { x: -300, y: -200, w: 600, h: 400 };
  const f = G.fitTransform(b, { width: 800, height: 500 }, 20);
  const tl = G.toScreen(f, { x: b.x, y: b.y }), br = G.toScreen(f, { x: b.x + b.w, y: b.y + b.h });
  assert.ok(tl.x >= 19.9 && tl.y >= 19.9 && br.x <= 780.1 && br.y <= 480.1);
  const c = G.edgeCurve({ x: 0, y: 0 }, { x: 100, y: 0 }, { bend: 0.2, trimA: 10, trimB: 10 });
  assert.ok(Math.abs(G.dist(c.p0, { x: 0, y: 0 }) - 10) < 1e-6);
  const rev = G.edgeCurve({ x: 100, y: 0 }, { x: 0, y: 0 }, { bend: 0.2 });
  assert.ok(Math.sign(c.c1.y) !== Math.sign(rev.c1.y), "a->b and b->a bow apart");
  const pts = G.samplesBySpacing(c, 12);
  assert.ok(pts.length >= 5 && finiteDeep(pts));
  assert.equal(G.arrowHead(c).length, 3);
  // dot 0 at frames 0..3: head, then the tail as the head moves on to dots 1, 2, 3
  assert.deepEqual(S.dotFrames(0, 4), ["●", "•", "·", " "]);
  assert.equal(S.dotFrames(1, 4)[1], "●");
  assert.equal(S.dotFrames(2, 4)[2], S.DOT_GLYPHS[0], "dot i is bright at frame i: motion runs src -> dst");
  assert.equal(G.withAlpha("#abc", 1), "#aabbccff");
});

test("event diff by seq", () => {
  const ev = doc.events;
  const first = M.diffEvents(null, ev);
  assert.deepEqual(first.fresh, [], "first read is the baseline");
  assert.equal(first.lastSeq, 103);
  assert.deepEqual(M.diffEvents(103, ev).fresh, []);
  const more = [...ev, { seq: 105, src: "a" }, { seq: 104, src: "b" }, { seq: "x" }];
  const d = M.diffEvents(103, more);
  assert.deepEqual(d.fresh.map((e) => e.seq), [104, 105]);
  assert.equal(d.lastSeq, 105);
  assert.equal(M.diffEvents(101, ev).fresh.length, 2);
  let last = first.lastSeq;
  for (let t = 1; t < 6; t++) {
    const r = M.diffEvents(last, [...ev, ...M.demoEvents(doc, t)]);
    assert.ok(r.fresh.length >= 1, `demo tick ${t} produces an event`);
    last = r.lastSeq;
  }
});

test("feed contract deltas: writer nodes, structural/stale/cross_host edges, posture, job, host counts, cursor reset", () => {
  const d = structuredClone(doc);
  d.nodes.push({ id: `${A}:writer:ghost`, kind: "writer", shoal: A, label: "ghost", host: "host-a", state: { class: "unknown" } });
  d.edges.push({ id: `${A}:writer:ghost|${A}:core|msg`, src: `${A}:writer:ghost`, dst: `${A}:core`, class: "msg", n_rate: 0, n_1h: 1, n_24h: 1, rate_per_h: 0.2, last_ts: null, last_age_s: null, shoal: A });
  const bo = d.edges.find((e) => e.id === `${B}:research|${B}:analysis|blocked-on`);
  Object.assign(bo, { last_ts: null, last_age_s: null, structural: true, stale: true, cross_host: false });
  d.edges.find((e) => e.id === `${A}:core|${A}:scribe|msg`).cross_host = false;
  const tune = d.nodes.find((x) => x.id === `${A}:tune`);
  Object.assign(tune.state, { source: "posture", quality: ["no-native-probe"] });
  const dist = d.nodes.find((x) => x.id === `${A}:dist-ci`);
  dist.state.job = { unit: "ci-gpu-1", host: "host-b", placed: true, granted: true, started_age_s: 120 };
  d.hosts = [{ id: "host:host-a", name: "host-a", home: true, roles: 11, working: 3, waiting: 6, needs_you: 2, msgs_1h: 40 }, { id: "host:host-b", name: "host-b", home: false, roles: 4 }];
  for (const view of ["global", A]) {
    const vm = M.buildModel(d, view);
    const w = vm.byId.get(`${A}:writer:ghost`);
    assert.ok(w, `writer node in ${view}`);
    assert.equal(w.visual.key, "writer-unknown");
    assert.equal(w.visual.border, "dashed");
    const lay = L.layoutGraph(vm);
    assert.ok(finiteDeep([...lay.pos.values()]));
    const sc = S.buildScene(vm, lay, { width: 900, height: 600, tf: G.identity() });
    assert.ok(finiteDeep(sc.items));
  }
  const vm = M.buildModel(d, "global");
  const e = vm.edges.find((x) => x.id === bo.id);
  assert.equal(e.tier, "stale");
  assert.equal(e.last_age_s, null);
  assert.equal(M.edgeBuckets([e])[0].coreAlpha, M.TIER_STYLE.stale.coreAlpha);
  assert.ok(M.TIER_STYLE.stale.coreAlpha < M.TIER_STYLE.struct.coreAlpha, "stale blockers draw fainter");
  assert.equal(vm.edges.find((x) => x.id === `${A}:core|${A}:scribe|msg`).cross, false, "the feed's cross_host wins");
  const card = M.cardLines(vm, `${A}:tune`);
  assert.ok(card.rows.find(([k]) => k === "state")[1].startsWith("~ "), "posture readings are marked");
  assert.ok(card.rows.some(([k, v]) => k === "quality" && v.includes("no-native-probe")));
  assert.equal(vm.byId.get(`${A}:dist-ci`).jobHost, "host-b", "state.job.host counts as a job host");
  assert.ok(M.cardLines(vm, `${A}:dist-ci`).rows.some(([k, v]) => k === "job" && v.includes("ci-gpu-1")));
  const hs = new Map(M.hostSummaries(vm).map((h) => [h.name, h]));
  assert.equal(hs.get("host-a").roles, 11);
  assert.equal(hs.get("host-a").msgsPerH, 40);
  assert.equal(hs.get("host-b").roles, 4);
  // a bad cursor comes back with cursor_reset and a full tail: a new baseline, nothing replayed
  assert.equal(M.eventsSince(103, { events: [...doc.events, { seq: 200 }] }).fresh.length, 1);
  const reset = M.eventsSince(500, { cursor_reset: true, events: doc.events });
  assert.deepEqual(reset.fresh, []);
  assert.equal(reset.lastSeq, 103);
});

console.log(`shoal-lattice: ${n} tests passed`);
