// test_lattice_view.mjs: the lattice-view Herdr app mod's pure modules against the bundled fixture.
// Node only, no dependencies: node tests/herdr_app/test_lattice_view.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "..", "plugins", "shoal-herdr-app", "mods", "lattice-view");
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
  assert.equal(g.tethers[0].to, null, "global view: no host region, a chip at the node instead");
  assert.equal(g.anchors.size, 0, "no host pills in the global view");
  assert.ok(a.tethers[0].to, "per shoal: the tether ends at the host band");
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
  // GPUI: wheel up (X button 4) and a trackpad swipe the same way arrive as a positive delta.y
  const z = G.zoomAt(tf, anchor, G.wheelFactor(120));
  const w1 = G.toWorld(z, anchor);
  assert.ok(z.k > tf.k && Math.abs(w0.x - w1.x) < 1e-9 && Math.abs(w0.y - w1.y) < 1e-9, "wheel up zooms in, keeping the anchor");
  assert.ok(G.zoomAt(tf, anchor, G.wheelFactor(-120)).k < tf.k, "wheel down zooms out");
  assert.ok(G.wheelFactor(4) > 1 && G.wheelFactor(4) < 1.05 && G.wheelFactor(-4) < 1, "small trackpad pixel deltas: same sign, small steps");
  assert.ok(Math.abs(G.wheelFactor(40) * G.wheelFactor(-40) - 1) < 1e-12, "up then down returns");
  assert.equal(G.wheelFactor(1e6), 2, "one event zooms at most 2x");
  assert.equal(G.wheelFactor(NaN), 1);
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
  // the feed folds the retired wake relay into the system node: a doc without relay nodes is fine
  const norelay = structuredClone(d);
  norelay.nodes = norelay.nodes.filter((x) => x.kind !== "relay");
  for (const view of ["global", A]) {
    const nvm = M.buildModel(norelay, view);
    assert.ok(!nvm.nodes.some((x) => x.kind === "relay"));
    assert.ok(finiteDeep(S.buildScene(nvm, L.layoutGraph(nvm), { width: 900, height: 600, tf: G.identity() }).items));
  }
  // a bad cursor comes back with cursor_reset and a full tail: a new baseline, nothing replayed
  assert.equal(M.eventsSince(103, { events: [...doc.events, { seq: 200 }] }).fresh.length, 1);
  const reset = M.eventsSince(500, { cursor_reset: true, events: doc.events });
  assert.deepEqual(reset.fresh, []);
  assert.equal(reset.lastSeq, 103);
});

const MO = await import(join(MOD, "motion.js"));

test("motion tiers: wake sources per tier, budgets, auto resolution", () => {
  const lay = L.layoutGraph(global);
  const tf = G.fitTransform(G.boundsOf([...lay.pos.values()], 40), { width: 1000, height: 700 });
  const packets = [{ seq: 1, kind: "packet", src: `${A}:core`, dst: `${A}:dist-ci`, color: "#ffffff", phase: "go", ms: 900 }];
  const scene = (tier, budget, vm = global, l = lay) => S.buildScene(vm, l, { width: 1000, height: 700, tf, packets, policy: MO.motionPolicy(tier, budget) });
  const loops = (sc) => sc.items.filter((i) => i.loops?.length).length;
  const frames = (sc) => sc.items.filter((i) => i.frames);
  const full = scene("full"), bal = scene("balanced"), low = scene("low"), off = scene("off");
  assert.ok(loops(full) > 0 && full.items.some((i) => i.t === "spin"));
  for (const sc of [bal, low, off]) assert.equal(loops(sc), 0, "only full uses loops (30 fps wakes)");
  for (const sc of [bal, low, off]) assert.ok(!sc.items.some((i) => i.t === "spin"));
  assert.ok(new Set(frames(bal).map((i) => i.frames.ms)).size === 1, "balanced: one shared frame interval");
  assert.ok(frames(bal).every((i) => i.frames.ms >= 180));
  const balDotEdges = new Set(frames(bal).filter((i) => i.id.startsWith("dot-")).map((i) => i.id.replace(/-\d+$/, "")));
  assert.ok(balDotEdges.size > 0 && balDotEdges.size <= 6, "balanced: dots on the top edges only");
  assert.ok(bal.items.some((i) => i.id.startsWith("pk-")), "balanced keeps packets");
  assert.ok(!frames(low).some((i) => i.id.startsWith("dot-")), "low: no marching dots");
  assert.ok(!low.items.some((i) => i.id.startsWith("pk-")), "low: no packets");
  assert.equal(low.animated, 0, "low: zero animated elements by default (a window frame is the cost on CPU)");
  const blink = scene("low", { frame_ms: 900 });
  const lowPulses = frames(blink).map((i) => i.id.slice("pulse-".length));
  assert.ok(lowPulses.length > 0 && lowPulses.every((id) => global.byId.get(id).visual.needsYou), "low + frame_ms: only needs-you blink");
  assert.ok(frames(blink).every((i) => i.frames.ms === 900));
  assert.equal(MO.motionPolicy("low", { frame_ms: 100 }).frameMs, 500, "low blinks no faster than 500 ms");
  assert.equal(off.animated, 0);
  assert.equal(frames(off).length + off.items.filter((i) => i.transitions?.length).length, 0, "off: nothing animates");
  // low with nothing needing attention: zero animated elements
  const calm = structuredClone(doc);
  for (const x of calm.nodes) if (x.kind === "role" && x.state?.class !== "work") x.state = { class: "idle" };
  const cvm = M.buildModel(calm, "global");
  assert.equal(scene("low", null, cvm, L.layoutGraph(cvm)).animated, 0);
  // budget overrides
  assert.equal(frames(scene("balanced", { max_dots: 0 })).filter((i) => i.id.startsWith("dot-")).length, 0);
  assert.ok(frames(scene("balanced", { frame_ms: 400 })).every((i) => i.frames.ms === 400));
  assert.equal(frames(scene("low", { frame_ms: 800, max_pulses: 1 })).length, 1);
  assert.ok(!scene("balanced", { packets: false }).items.some((i) => i.id.startsWith("pk-")));
  assert.equal(MO.motionPolicy("off", { max_dots: 50, packets: true }).maxDots, 0);
  // auto
  assert.equal(MO.resolveTier("auto", {}).tier, "balanced");
  assert.equal(MO.resolveTier("auto", { renderer: "cpu" }).tier, "low");
  assert.equal(MO.resolveTier("auto", { renderer: "gpu" }).tier, "full");
  assert.equal(MO.resolveTier("auto", { renderer: "gpu", pulse: true }).tier, "balanced");
  assert.equal(MO.resolveTier("auto", { renderer: "cpu", focused: false }).tier, "off");
  assert.equal(MO.resolveTier("low", { renderer: "gpu", focused: false }).tier, "low", "an explicit tier is kept");
  assert.equal(MO.rendererFromProcess("A=1\0VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json\0"), "cpu");
  assert.equal(MO.rendererFromProcess("VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/nvidia_icd.json"), "gpu");
  assert.equal(MO.rendererFromProcess("LIBGL_ALWAYS_SOFTWARE=1"), "cpu");
  assert.equal(MO.rendererFromProcess("", "7f00 r-xp /usr/lib/libvulkan_lvp.so"), "cpu");
  assert.equal(MO.rendererFromProcess("HOME=/x", ""), null);
  assert.deepEqual(MO.MOTION_CYCLE, ["auto", "full", "balanced", "low", "off"]);
  // static rate encoding: busier edges draw wider without any motion
  const b = M.edgeBuckets([{ id: "a", cls: "msg", tier: "warm", rate: 0.2, color: "#fff" }, { id: "b", cls: "msg", tier: "warm", rate: 5, color: "#fff" }]);
  assert.equal(b.length, 2);
  assert.ok(b.find((x) => x.band === 2).core > b.find((x) => x.band === 0).core);
});

test("render gating: unchanged polls skip the redraw; low and off space data redraws out", () => {
  const vm1 = M.buildModel(doc, "global");
  const same = M.buildModel(structuredClone(doc), "global");
  assert.equal(M.visualSignature(vm1), M.visualSignature(same));
  const demo = { ...doc, events: [...doc.events, ...M.demoEvents(doc, 3)], ts: "2026-11-02T20:00:04Z", cursor: "next" };
  assert.equal(M.visualSignature(M.buildModel(demo, "global")), M.visualSignature(vm1), "new events and a new ts alone change nothing drawn");
  const d2 = structuredClone(doc);
  d2.shoals[0].metrics.msgs_1h = 99;
  assert.notEqual(M.visualSignature(M.buildModel(d2, "global")), M.visualSignature(vm1));
  assert.equal(M.urgentSignature(M.buildModel(d2, "global")), M.urgentSignature(vm1));
  const d3 = structuredClone(doc);
  d3.nodes.find((x) => x.id === `${A}:core`).state = { class: "wait", event: "user-ask", group: "human" };
  assert.notEqual(M.urgentSignature(M.buildModel(d3, "global")), M.urgentSignature(vm1));
  const base = { sig: 2, urgentSig: 1, lastSig: 1, lastUrgentSig: 1, sinceMs: 4000 };
  assert.equal(MO.shouldRender({ ...base, sig: 1 }), false, "nothing changed");
  assert.equal(MO.shouldRender({ ...base, renderMs: 0 }), true);
  assert.equal(MO.shouldRender({ ...base, renderMs: 15000 }), false, "low: not yet");
  assert.equal(MO.shouldRender({ ...base, renderMs: 15000, sinceMs: 16000 }), true);
  assert.equal(MO.shouldRender({ ...base, renderMs: 30000, urgentSig: 9 }), true, "what needs you changed: at once");
  assert.ok(MO.motionPolicy("low").renderMs > 0 && MO.motionPolicy("off").renderMs >= MO.motionPolicy("low").renderMs && MO.motionPolicy("full").renderMs === 0);
});

test("labels: no overlaps, right ellipsis, alias labels hidden beside their role", () => {
  assert.equal(S.fitText("short", 100, 10), "short");
  const long = S.fitText("a-very-long-role-name-indeed", 90, 10);
  assert.ok(long.endsWith("…") && "a-very-long-role-name-indeed".startsWith(long.slice(0, -1)), "ellipsis from the right");
  const d = structuredClone(doc);
  d.nodes.find((x) => x.id === `${A}:addressing`).label = "addressing-with-a-much-longer-name";
  for (const view of ["global", A]) {
    const vm = M.buildModel(d, view);
    const lay = L.layoutGraph(vm);
    const sc = S.buildScene(vm, lay, { width: 1100, height: 720, tf: G.fitTransform(G.boundsOf([...lay.pos.values()], 60), { width: 1100, height: 720 }), policy: MO.motionPolicy("off") });
    const labels = sc.items.filter((i) => i.id.startsWith("label-"));
    for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) {
      const a = labels[i], b = labels[j];
      const est = (x) => ({ x: x.x, y: x.y, w: x.text.length * x.size * 0.6, h: x.h });
      const ea = est(a), eb = est(b);
      const hit = ea.x < eb.x + eb.w && eb.x < ea.x + ea.w && ea.y < eb.y + eb.h && eb.y < ea.y + ea.h;
      assert.ok(!hit, `${view}: ${a.id} overlaps ${b.id}`);
    }
    // cluster titles ("ALPHA r12", "2 need you") are obstacles too
    const titles = sc.items.filter((i) => i.id.startsWith("disc-label-") || i.id.startsWith("disc-needs-")).map((t) => { const w = t.text.length * t.size * 0.6; return { x: t.x + t.w / 2 - w / 2, y: t.y, w, h: t.h }; });
    for (const l of labels.filter((x) => !vm.byId.get(x.id.slice("label-".length))?.kind?.startsWith("role"))) {
      const el = { x: l.x, y: l.y, w: l.text.length * l.size * 0.6, h: l.h };
      for (const t of titles) assert.ok(!(el.x < t.x + t.w && t.x < el.x + el.w && el.y < t.y + t.h && t.y < el.y + el.h), `${view}: ${l.id} overlaps a cluster title`);
    }
    for (const n of vm.nodes.filter((x) => x.kind === "role")) assert.ok(labels.some((l) => l.id === `label-${n.id}`), `${n.id} keeps its label`);
    const lab = labels.find((l) => l.id === `label-${A}:addressing`);
    if (lab) assert.ok(lab.text.endsWith("…"));
    if (view === "global") assert.ok(!labels.some((l) => l.id === "label-ext:research"), "aliased external label hidden beside its role");
  }
});


test("hover card: inside the canvas, flipped near edges, capped when tall", () => {
  const size = { width: 720, height: 740 };
  const inside = (b) => b.x >= 8 - 1e-9 && b.y >= 8 - 1e-9 && b.x + b.w <= size.width - 8 + 1e-9 && b.y + b.h <= size.height - 8 + 1e-9;
  const mid = G.placeCard({ x: 200, y: 200, r: 10 }, size, 300, 200);
  assert.ok(inside(mid) && mid.x > 200, "room on the right: beside the node, right");
  const right = G.placeCard({ x: 600, y: 200, r: 10 }, size, 300, 200);
  assert.ok(inside(right) && right.x + right.w < 600, "near the right edge: flipped left of the node");
  const low = G.placeCard({ x: 200, y: 700, r: 10 }, size, 300, 260);
  assert.ok(inside(low) && low.y + low.h <= 720 + 1e-9 && low.y < 700, "near the bottom: flipped above, ending near the node");
  const tall = G.placeCard({ x: 650, y: 690, r: 10 }, size, 300, 2000);
  assert.ok(inside(tall) && tall.h === size.height - 16 && tall.maxH === size.height - 16, "taller than the canvas: capped");
  for (let x = 0; x <= size.width; x += 60) for (let y = 0; y <= size.height; y += 60) assert.ok(inside(G.placeCard({ x, y, r: 12 }, size, 300, 420)), `card at ${x},${y}`);
  // every card line is one line: long items and blockers are cut, the full text kept for a tooltip
  const d = structuredClone(doc);
  const core = d.nodes.find((x) => x.id === `${A}:core`);
  core.work.next[0].blocker = "upstream: a release that carries the fix, then a long explanation ".repeat(4);
  const card = M.cardLines(M.buildModel(d, "global"), `${A}:core`);
  const long = card.links.find((l) => l.text.length > 100);
  assert.ok(long);
  const cut = S.fitText(long.text, 276, 10.5);
  assert.ok(cut.length < long.text.length && cut.endsWith("…") && long.text.startsWith(cut.slice(0, -1)));
});

test("renderer probe: environment forcing wins, then hardware drivers outrank the lavapipe ICD", () => {
  const line = (path, i) => `7f${(0x1000 + i).toString(16)}000-7f${(0x1001 + i).toString(16)}000 r-xp 00000000 103:02 ${1000 + i} ${path}`;
  const maps = (paths) => paths.map(line).join("\n");
  const lib = "/usr/lib/x86_64-linux-gnu/";
  const mesaIcds = ["libvulkan_lvp.so", "libvulkan_intel.so", "libvulkan_intel_hasvk.so", "libvulkan_radeon.so", "libvulkan_nouveau.so", "libvulkan_asahi.so", "libvulkan_gfxstream.so", "libvulkan_virtio.so"].map((f) => lib + f);
  // the laptop: the Vulkan loader maps every installed ICD, lavapipe included, plus the NVIDIA driver
  const laptop = maps([lib + "libvulkan.so.1.3.275", ...mesaIcds, lib + "libGLX_nvidia.so.590.48.01", lib + "libnvidia-glvkspirv.so.590.48.01", lib + "libnvidia-glcore.so.590.48.01", lib + "libEGL_mesa.so.0.0.0", "/dev/nvidia0", "/dev/nvidiactl", "/dev/dri/renderD129"]);
  const laptopEnv = "HOME=/home/<user>\0DISPLAY=:0\0XDG_SESSION_TYPE=wayland\0";
  assert.equal(MO.rendererFromProcess(laptopEnv, laptop), "gpu", "laptop: all ICDs plus nvidia, no forcing env");
  assert.equal(MO.rendererFromProcess(laptopEnv), null, "the environment alone says nothing there");
  // the dev server: a virtual display, lavapipe selected by the environment
  const devEnv = "DISPLAY=:17\0VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json\0VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json\0";
  const devMaps = maps([lib + "libvulkan.so.1.3.204", lib + "libvulkan_lvp.so"]);
  assert.equal(MO.rendererFromProcess(devEnv), "cpu", "dev server: VK_ICD_FILENAMES names lavapipe");
  assert.equal(MO.rendererFromProcess(devEnv, laptop), "cpu", "a forcing environment wins over any maps");
  assert.equal(MO.rendererFromProcess("", devMaps), "cpu", "software only: just libvulkan_lvp");
  assert.equal(MO.rendererFromProcess("GALLIUM_DRIVER=llvmpipe", laptop), "cpu");
  assert.equal(MO.rendererFromProcess("LIBGL_ALWAYS_SOFTWARE=1", laptop), "cpu");
  assert.equal(MO.rendererFromProcess("LIBGL_ALWAYS_SOFTWARE=true"), "cpu");
  assert.equal(MO.rendererFromProcess("SHOAL_LATTICE_RENDERER=cpu", laptop), "cpu");
  assert.equal(MO.rendererFromProcess("SHOAL_LATTICE_RENDERER=gpu", devMaps), "gpu");
  assert.equal(MO.rendererFromProcess("VK_DRIVER_FILES=/usr/share/vulkan/icd.d/radeon_icd.x86_64.json", devMaps), "gpu");
  // an AMD or Intel desktop: every Mesa ICD mapped, and the render node the driver draws through
  assert.equal(MO.rendererFromProcess("", maps([...mesaIcds, "/dev/dri/renderD128"])), "gpu");
  assert.equal(MO.rendererFromProcess("", maps([lib + "libvulkan_radeon.so"])), "gpu", "a hardware ICD and no software one");
  // every Mesa ICD and no device node (a GPU-less box with Mesa installed): unknown, auto stays balanced
  assert.equal(MO.rendererFromProcess("", maps(mesaIcds)), null);
  assert.equal(MO.resolveTier("auto", { renderer: MO.rendererFromProcess(laptopEnv, laptop) }).tier, "full");
  assert.equal(MO.resolveTier("auto", { renderer: MO.rendererFromProcess(devEnv, devMaps) }).tier, "low");
});

test("layout stability: same nodes never move; an added node leaves the rest in place", () => {
  for (const view of ["global", A]) {
    const l = new L.Layout();
    l.update(M.buildModel(doc, view)).run();
    const before = new Map([...l.pos].map(([k, p]) => [k, { ...p }]));
    // the same graph with every metric, state age, rate and item changed: identical positions
    const d = structuredClone(doc);
    for (const x of d.nodes) {
      x.metrics = { ...(x.metrics ?? {}), done_24h: 40, msgs_out_1h: 30, msgs_in_1h: 12, context_k: 180 };
      if (x.state) x.state.since_s = (x.state.since_s ?? 0) + 3600;
      if (Array.isArray(x.work?.done_recent)) x.work.done_recent = x.work.done_recent.slice(0, 1);
    }
    for (const e of d.edges) Object.assign(e, { n_1h: 50, rate_per_h: 50, n_rate: 9, last_age_s: 2 });
    // global: some edges go quiet too (per shoal, an external with no traffic leaves the view)
    if (view === "global") d.edges = d.edges.filter((e, i) => i % 3 !== 0 || e.class === "blocked-on" || e.class === "alias");
    assert.equal(L.layoutKey(M.buildModel(d, view)), L.layoutKey(M.buildModel(doc, view)), `${view}: same node set`);
    d.nodes.reverse();
    l.update(M.buildModel(d, view));
    assert.equal(l.moved, false, `${view}: same node set, nothing to move`);
    l.run();
    for (const [k, p] of before) assert.deepEqual(l.pos.get(k), p, `${view}: ${k} moved on a metrics-only poll`);
    // one external writer appears, talking to a role: everything else stays within epsilon
    const d2 = structuredClone(doc);
    d2.nodes.push({ id: "ext:newcomer", kind: "external", label: "newcomer", state: { class: "unknown" } });
    d2.edges.push({ id: `ext:newcomer|${A}:core|msg`, src: "ext:newcomer", dst: `${A}:core`, class: "msg", n_rate: 1, n_1h: 1, n_24h: 1, rate_per_h: 1, last_ts: null, last_age_s: 30, shoal: A });
    l.update(M.buildModel(d2, view)).run();
    assert.ok(l.pos.has("ext:newcomer") && finiteDeep([...l.pos.values()]));
    let max = 0;
    for (const [k, p] of before) max = Math.max(max, G.dist(p, l.pos.get(k)));
    if (view === "global") assert.ok(max < 0.5, `${view}: an added external moved the others by ${max}`);
    else assert.ok(max < 220, `${view}: ${max}`);
  }
  // a role added to one shoal: the other shoal does not move
  const l = new L.Layout();
  l.update(M.buildModel(doc, "global")).run();
  const before = new Map([...l.pos].map(([k, p]) => [k, { ...p }]));
  const d3 = structuredClone(doc);
  d3.nodes.push({ id: `${B}:extra`, kind: "role", shoal: B, slug: "extra", label: "extra", host: "host-a", state: { class: "idle" } });
  l.update(M.buildModel(d3, "global")).run();
  for (const [k, p] of before) if (k.startsWith(A)) assert.ok(G.dist(p, l.pos.get(k)) < 0.5, `${k} moved for a role added to the other shoal`);
  // f: a full layout from scratch is the deterministic cold layout
  l.update(M.buildModel(doc, "global"), { relayout: true }).run();
  const cold = L.layoutGraph(M.buildModel(doc, "global"));
  for (const [k, p] of cold.pos) assert.deepEqual(l.pos.get(k), p, `relayout ${k}`);
});

test("fit: every node, label, chip and cluster ring inside the canvas with a margin", () => {
  const d = structuredClone(doc);
  d.nodes.find((x) => x.id === `${A}:addressing`).label = "addressing-with-a-much-longer-name";
  for (const size of [{ width: 722, height: 740 }, { width: 1000, height: 560 }, { width: 520, height: 900 }]) {
    for (const view of ["global", A, B]) {
      const vm = M.buildModel(d, view);
      const lay = L.layoutGraph(vm);
      const tf = S.fitScene(vm, lay, size, { pad: 24, top: 48 });
      const sc = S.buildScene(vm, lay, { ...size, tf, policy: MO.motionPolicy("off") });
      const e = sc.extent;
      const where = `${view} at ${size.width}x${size.height}`;
      assert.ok(e.x0 >= 24 - 1 && e.x1 <= size.width - 24 + 1 && e.y0 >= 48 - 1 && e.y1 <= size.height - 24 + 1, `${where}: extent ${JSON.stringify(e)}`);
      for (const it of sc.items.filter((i) => i.id.startsWith("label-") || i.id.startsWith("tether-chip-"))) {
        const w = it.id.startsWith("label-") ? it.text.length * it.size * 0.6 : it.w;
        assert.ok(it.x >= 0 && it.x + w <= size.width && it.y >= 0 && it.y + it.h <= size.height, `${where}: ${it.id} clipped`);
      }
      for (const c of sc.discs) assert.ok(c.x - c.r >= 0 && c.x + c.r <= size.width && c.y - c.r >= 0 && c.y + c.r <= size.height, `${where}: ring ${c.id} clipped`);
      // a fit is centered, and the whole width or height is used
      const usedW = e.x1 - e.x0, usedH = e.y1 - e.y0;
      assert.ok(usedW >= size.width - 48 - 4 || usedH >= size.height - 72 - 4 || tf.k >= 1.6 - 1e-9, `${where}: fit uses the canvas`);
      assert.ok(Math.abs((e.x0 - 24) - (size.width - 24 - e.x1)) < 3, `${where}: centered`);
    }
  }
});

test("pulse model: a row per shoal, then up to five needs-you roles with event and host", () => {
  const pm = M.pulseModel(doc);
  assert.deepEqual(pm.shoals.map((s) => s.id), [A, B]);
  const a = pm.shoals[0];
  assert.equal(a.urgent, "auth");
  assert.equal(a.urgentColor, M.PALETTE.auth, "the dot takes the most urgent role state's color");
  assert.equal(a.needsYou, 2);
  assert.equal(pm.shoals[1].needsYou, 0);
  assert.deepEqual(pm.needs.map((r) => r.id), [`${A}:integration`, `${A}:tune`], "loudest first; stalls are not needs-you rows");
  assert.deepEqual([pm.needs[0].event, pm.needs[0].badge, pm.needs[0].host], ["user-auth", "⚷", "host-a"]);
  assert.equal(pm.more, 0);
  // capped at five, the rest counted
  const many = structuredClone(doc);
  for (let i = 0; i < 7; i++) many.nodes.push({ id: `${B}:ask${i}`, kind: "role", shoal: B, host: "host-b", state: { class: "wait", event: "user-ask", since_s: i } });
  const pm2 = M.pulseModel(many);
  assert.equal(pm2.needs.length, 5);
  assert.equal(pm2.more, 4);
  assert.equal(pm2.needs[0].id, `${A}:integration`, "auth still first");
  assert.equal(pm2.shoals[1].needsYou, 7);
  assert.equal(M.pulseModel(many, { maxNeeds: 2 }).needs.length, 2);
  // narrowed to one shoal by the `shoal` prop; an unknown shoal shows every shoal
  const only = M.pulseModel(many, { shoal: B });
  assert.deepEqual(only.shoals.map((s) => s.id), [B]);
  assert.ok(only.needs.every((r) => r.shoal === B));
  assert.equal(M.pulseModel(doc, { shoal: "gone-20261101-000000" }).shoals.length, 2);
  // render gating: the same doc hashes the same; a new needs-you role changes it
  assert.equal(M.pulseSignature(M.pulseModel(doc)), M.pulseSignature(M.pulseModel(structuredClone(doc))));
  assert.notEqual(M.pulseSignature(M.pulseModel(doc)), M.pulseSignature(pm2));
  assert.deepEqual(M.pulseModel({}).shoals, []);
  assert.ok(finiteDeep(pm2));
});

test("default view from the shoal prop: drilled in, Escape returns home, unknown shoal falls back", () => {
  assert.deepEqual(M.homeView(doc, A), { home: A, missing: false });
  assert.deepEqual(M.homeView(doc, null), { home: "global", missing: false });
  assert.deepEqual(M.homeView(doc, "global"), { home: "global", missing: false });
  assert.deepEqual(M.homeView(doc, "gone-20261101-000000"), { home: "global", missing: true });
  // the home shoal's view holds that constellation only (plus the hub and its externals)
  const home = M.buildModel(doc, M.homeView(doc, B).home);
  assert.equal(home.view, B);
  assert.equal(home.global, false);
  assert.ok(home.nodes.filter((nd) => nd.kind === "role").every((nd) => nd.shoal === B));
  assert.deepEqual(home.shoalsInView.map((s) => s.id), [B]);
  // Escape: unpin, then the legend, then back to the home view; nothing at home
  const H = A;
  assert.deepEqual(M.escapeStep({ pinned: "x", legend: true, view: B, home: H }), { pinned: null });
  assert.deepEqual(M.escapeStep({ legend: true, view: B, home: H }), { legend: false });
  assert.deepEqual(M.escapeStep({ view: B, home: H }), { view: H }, "another shoal goes back to the home shoal");
  assert.deepEqual(M.escapeStep({ view: "global", home: H }), { view: H }, "the global view goes back to the home shoal");
  assert.equal(M.escapeStep({ view: H, home: H }), null, "at home Escape does nothing");
  assert.deepEqual(M.escapeStep({ view: A, home: "global" }), { view: "global" }, "without a home shoal Escape goes global");
  assert.equal(M.escapeStep({ view: "global", home: "global" }), null);
});

test("shared focus: a node id or {node, shoal, t}; persisted and stale requests are not replayed", () => {
  const now = 1_000_000;
  const f = M.readFocus(`${A}:tune`, null, now);
  assert.equal(f.node, `${A}:tune`);
  assert.equal(f.shoal, A, "the shoal is read from the node id");
  assert.equal(M.readFocus(`${A}:tune`, f.key, now), null, "the same value twice is one request");
  const o = M.readFocus({ node: `${B}:pool`, shoal: B, t: now - 1000 }, f.key, now);
  assert.deepEqual([o.node, o.shoal, o.stale], [`${B}:pool`, B, undefined]);
  const again = M.readFocus({ node: `${B}:pool`, shoal: B, t: now }, o.key, now);
  assert.ok(again && again.node === `${B}:pool`, "a repeat click with a new t is a new request");
  const stale = M.readFocus({ node: `${B}:pool`, t: now - 200000 }, null, now);
  assert.equal(stale.stale, true);
  assert.equal(stale.node, null);
  const shoalOnly = M.readFocus({ shoal: B, node: null, t: now }, null, now);
  assert.deepEqual([shoalOnly.node, shoalOnly.shoal], [null, B]);
  for (const bad of [null, undefined, 3, "", {}, { t: now }, []]) assert.equal(M.readFocus(bad, null, now), null);
  // a copy starts with the persisted value as seen
  const persisted = { node: `${A}:core`, t: now - 5 };
  assert.equal(M.readFocus(persisted, M.focusKeyOf(persisted), now), null);
  assert.equal(M.focusKeyOf(null), null);
  assert.equal(M.FOCUS_KEY, "lattice.focus");
});

console.log(`lattice-view: ${n} tests passed`);
