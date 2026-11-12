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
const R = await import(join(MOD, "replay.js"));
const D = await import(join(MOD, "depth.js"));
const S = await import(join(MOD, "scene.js"));
const SP = await import(join(MOD, "sphere.js"));
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
    [`${A}:integration`]: "auth", [`${A}:addressing`]: "queued", [`${A}:registry`]: "unknown", [`${A}:scribe`]: "handover",
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
  assert.ok(vis(global, `${A}:dist-ci`).hatch > 0 && vis(global, `${B}:analysis`).hatch > 0);
  assert.equal(vis(global, `${B}:research`).shape, "circle", "the agent's type is in the hover card");
  assert.equal(vis(global, `${A}:core`).shape, "circle");
});

test("every salient wait is distinguishable", () => {
  const keys = ["auth", "ask", "prompt", "design", "assign", "sources", "local-ci", "remote-ci", "research-proto", "lease", "upstream", "peer", "handover", "refill", "hold", "other"];
  const sig = (k) => { const s = M.STATE_STYLE[k]; return [s.color, s.badge ?? "", s.hatch ?? 0, s.border ?? "", s.fill].join("|"); };
  assert.equal(new Set(keys.map(sig)).size, keys.length, "each wait has its own encoding");
  const l = M.STATE_STYLE["local-ci"], r = M.STATE_STYLE["remote-ci"];
  assert.notEqual(l.badge, r.badge);
  assert.notEqual(l.hatch, r.hatch);
  // a research or prototyping run reads apart from both CI waits: its own badge, hatch and a dashed outline
  const rp = M.STATE_STYLE["research-proto"];
  for (const ci of [l, r]) {
    assert.notEqual(rp.badge, ci.badge);
    assert.notEqual(rp.hatch, ci.hatch);
    assert.notEqual(rp.border, ci.border);
  }
  assert.equal(rp.spinner, true, "a run, so it spins like CI");
  assert.equal(M.stateKey({ state: { class: "wait", event: "research-proto", group: "research" } }), "research-proto");
  assert.equal(M.stateKey({ state: { class: "wait", event: "", group: "research" } }), "research-proto");
  assert.ok(M.LEGEND_GROUPS.find((g) => g.id === "waiting").keys.includes("research-proto"));
  assert.ok(M.STATE_RANK.includes("research-proto"));
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
    mk("stall", "unknown", "", 9999), mk("design", "wait", "user-design", 10), { ...mk("assign", "wait", "assignment", 50), state: { class: "wait", event: "assignment", since_s: 50, needs_you: true } },
    mk("quiet", "wait", "assignment", 60),
    mk("ask", "wait", "user-ask", 5), mk("prompt", "wait", "user-prompt", 500), mk("auth", "wait", "user-auth", 1), mk("busy", "work", "turn", 1),
  ]);
  assert.deepEqual(rows.map((r) => r.id), ["auth", "prompt", "ask", "design", "assign", "stall"],
    "an empty queue needs you only when its status row says so (one rule with the agents list)");
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
  // a single shoal keeps its own edges; the multi-shoal view merges each pair (its own test below)
  assert.equal(M.edgeBuckets(alpha.edges).find((b) => b.edges.some((e) => e.id === `${A}:core|${A}:dist-ci|msg`)).dash, null);
  const keep = M.incidentEdges(global, `${A}:tune`);
  const faded = M.edgeBuckets(global.edges, keep);
  assert.ok(faded.some((b) => b.tier === "fade"));
  assert.ok(faded.filter((b) => b.tier !== "fade").every((b) => b.edges.every((e) => keep.has(e.id))));
  assert.ok(global.edges.some((e) => e.flow) && global.edges.filter((e) => e.flow).every((e) => e.tier === "hot" || e.tier === "warm"));
  assert.ok(global.broadcasts.some((b) => b.src === `${A}:core`), "broadcast is a ripple, not an edge");
  assert.ok(!global.edges.some((e) => e.cls === "broadcast"));
});

test("the multi-shoal view merges each pair into one edge with both directions in its card", () => {
  const ids = global.edges.map((e) => [e.src, e.dst].sort().join("|"));
  assert.equal(new Set(ids).size, ids.length, "one edge per pair of nodes");
  const mk = (src, dst, cls, n1, n24, tier = "warm") => ({ id: `${src}|${dst}|${cls}`, src, dst, cls, tier, color: "#fff", bend: 0.14,
    rate: n1, n_rate: 0, n_1h: n1, n_24h: n24, last_age_s: 60, shoal: "s", flow: false, flowMs: 0, cross: false, stale: false,
    open: null, recent: [{ summary: `${src}->${dst}`, age_s: 60 }] });
  const [m] = M.mergePairs([mk("s:a", "s:b", "msg", 2, 5), mk("s:b", "s:a", "ask", 1, 1, "hot"), mk("s:a", "s:b", "offer", 0, 1, "cold")]);
  assert.equal(m.id, "s:a|s:b|pair");
  assert.equal(m.cls, "ask", "drawn as its most important class");
  assert.equal(m.tier, "hot", "its hottest tier");
  assert.equal(m.heads, "both", "traffic both ways: arrowheads at both ends");
  assert.deepEqual([m.n_1h, m.n_24h], [3, 7]);
  assert.deepEqual([m.dirs.fwd.n_24h, m.dirs.back.n_24h], [6, 1]);
  assert.deepEqual(m.dirs.fwd.classes.sort(), ["msg", "offer"]);
  const one = M.mergePairs([mk("s:b", "s:a", "msg", 1, 2)])[0];
  assert.equal(one.heads, "src", "a reverse-only pair points back at its source end");
  // the card shows each direction on its own row
  const vm = { edges: [m], byId: new Map([["s:a", { label: "a", host: "h1" }], ["s:b", { label: "b", host: "h1" }]]) };
  const card = M.edgeCardLines(vm, m.id);
  assert.equal(card.title, "a ↔ b");
  assert.ok(card.rows.some(([k, v]) => k === "a → b" && v.startsWith("2 / 6")), JSON.stringify(card.rows));
  assert.ok(card.rows.some(([k, v]) => k === "b → a" && v.startsWith("1 / 1") && v.includes("ask")), JSON.stringify(card.rows));
  // a single shoal keeps its per-class edges
  assert.ok(alpha.edges.some((e) => e.id.endsWith("|msg")) && !alpha.edges.some((e) => e.id.endsWith("|pair")));
});

test("an open ask reads as an ask over a CI wait, and a seat's card names its host", () => {
  const role = (event, asks) => ({ id: "s:r", kind: "role", state: { class: "wait", event, needs_you: asks.length > 0 }, work: { asks } });
  assert.equal(M.stateKey(role("local-ci", ["merge it?"])), "ask");
  assert.equal(M.stateKey(role("local-ci", [])), "local-ci");
  assert.equal(M.stateKey(role("user-prompt", ["x?"])), "prompt", "a wait that already needs you keeps its look");
  const d = structuredClone(doc);
  d.nodes.push({ id: "ext:seat", kind: "external", label: "seat", host: "host-z", saved: true,
    standalone: { harness: "claude", id: "abcd1234", state: "alive", dismissed: false }, state: { class: "unknown" }, metrics: {} });
  const vm = M.buildModel(d, "global");
  assert.ok(M.cardLines(vm, "ext:seat").rows.some(([k, v]) => k === "host" && v === "host-z"));
});

test("blocked-on edges present; alias edge in global view only", () => {
  const bo = (vm) => vm.edges.filter((e) => e.cls === "blocked-on").map((e) => e.id).sort();
  // the multi-shoal view merges each pair into one edge, drawn as its most important class (a blocker)
  assert.deepEqual(bo(global), [`${A}:core|${A}:dist-ci|pair`, `${B}:analysis|${B}:research|pair`]);
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

test("feed contract deltas: writer nodes, structural/stale/cross_host edges, status, job, host counts, cursor reset", () => {
  const d = structuredClone(doc);
  d.nodes.push({ id: `${A}:writer:ghost`, kind: "writer", shoal: A, label: "ghost", host: "host-a", state: { class: "unknown" } });
  d.edges.push({ id: `${A}:writer:ghost|${A}:core|msg`, src: `${A}:writer:ghost`, dst: `${A}:core`, class: "msg", n_rate: 0, n_1h: 1, n_24h: 1, rate_per_h: 0.2, last_ts: null, last_age_s: null, shoal: A });
  const bo = d.edges.find((e) => e.id === `${B}:research|${B}:analysis|blocked-on`);
  Object.assign(bo, { last_ts: null, last_age_s: null, structural: true, stale: true, cross_host: false });
  d.edges.find((e) => e.id === `${A}:core|${A}:scribe|msg`).cross_host = false;
  const tune = d.nodes.find((x) => x.id === `${A}:tune`);
  Object.assign(tune.state, { source: "status", quality: ["no-native-probe"] });
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
  // per-class edges are a single shoal's (the multi-shoal view merges each pair)
  const e = M.buildModel(d, B).edges.find((x) => x.id === bo.id);
  assert.equal(e.tier, "stale");
  assert.equal(e.last_age_s, null);
  assert.equal(M.edgeBuckets([e])[0].coreAlpha, M.TIER_STYLE.stale.coreAlpha);
  assert.ok(M.TIER_STYLE.stale.coreAlpha < M.TIER_STYLE.struct.coreAlpha, "stale blockers draw fainter");
  assert.equal(M.buildModel(d, A).edges.find((x) => x.id === `${A}:core|${A}:scribe|msg`).cross, false, "the feed's cross_host wins");
  const card = M.cardLines(vm, `${A}:tune`);
  assert.ok(card.rows.find(([k]) => k === "state")[1].startsWith("~ "), "status readings are marked");
  assert.ok(card.rows.some(([k, v]) => k === "quality" && v.includes("no-native-probe")));
  assert.equal(vm.byId.get(`${A}:dist-ci`).jobHost, "host-b", "state.job.host counts as a job host");
  assert.ok(M.cardLines(vm, `${A}:dist-ci`).rows.some(([k, v]) => k === "job" && v.includes("ci-gpu-1")));
  const hs = new Map(M.hostSummaries(vm).map((h) => [h.name, h]));
  assert.equal(hs.get("host-a").roles, 11);
  assert.equal(hs.get("host-a").msgsPerH, 40);
  assert.equal(hs.get("host-b").roles, 4);
  // the shoal's writer has no node of its own: its status is on the middle of the circle
  for (const view of ["global", A]) {
    const nvm = M.buildModel(d, view);
    assert.ok(!nvm.nodes.some((x) => x.kind === "system"));
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

test("actions open on roles, shoals and saved standalone seats, on the feed's machine", () => {
  const exec = { kind: "exec", argv: ["shoal", "ui", "graph", "--json"], on: "lane-a" };
  const o = M.actionsOverlay(alpha, `${A}:core`, exec);
  assert.deepEqual(o, { mod: "shoal-actions", kind: "dialog", width: 640, height: 600, props: { shoal: A, role: "core", machine: "lane-a" } });
  assert.equal(M.actionsOverlay(alpha, `${A}:core`, { kind: "exec", on: null }).props.machine, null, "null is this computer");
  assert.equal(M.actionsOverlay(alpha, `${A}:core`, { kind: "file", path: "<feed>", on: "lane-b" }).props.machine, "lane-b");
  assert.equal(M.actionsOverlay(global, `${A}:core`, null).props.machine, null);
  const fx = M.actionsOverlay(alpha, `${A}:core`, { kind: "fixture" });
  assert.equal(fx.props.fixture, true, "a fixture copy opens the dialog on fixtures");
  assert.equal(fx.props.machine, null);
  assert.ok(!("fixture" in o.props), "a live copy does not");
  // a shoal's circle acts on that shoal, anywhere else on every shoal
  assert.deepEqual(M.scopeOverlay(alpha, A, exec).props, { shoal: A, role: null, machine: "lane-a" });
  assert.deepEqual(M.scopeOverlay(global, null, exec).props, { shoal: null, role: null, scope: "all", machine: "lane-a" });
  assert.equal(M.scopeOverlay(null, A, exec), null);
  // the hub, an external writer, a missing id or no model: no role to act on
  assert.equal(M.actionsOverlay(global, "user", exec), null);
  for (const nd of global.nodes.filter((x) => x.kind !== "role")) {
    const overlay = M.actionsOverlay(global, nd.id, exec);
    if (nd.kind === "external" && nd.saved && !nd.alias_of) {
      assert.equal(overlay.props.session, nd.seat ?? nd.label);
      assert.equal(overlay.props.machine, "lane-a");
    } else assert.equal(overlay, null, nd.id);
  }
  assert.equal(M.actionsOverlay(alpha, null, exec), null);
  assert.equal(M.actionsOverlay(alpha, "nope", exec), null);
  assert.equal(M.actionsOverlay(null, `${A}:core`, exec), null);
  // every role node in the fixture gets its own slug
  for (const nd of global.nodes.filter((x) => x.kind === "role")) {
    const p = M.actionsOverlay(global, nd.id, exec).props;
    assert.equal(p.shoal, nd.shoal);
    assert.equal(p.role, nd.slug ?? nd.label);
  }
});

test("edge cards: hit test, card lines, focus and signature", () => {
  const vm = M.buildModel(doc, A);
  const lay = L.layoutGraph(vm);
  const size = { width: 1280, height: 800 };
  const tf = S.fitScene(vm, lay, size);
  const id = `${A}:core|${A}:dist-ci|msg`;
  const sc = S.buildScene(vm, lay, { ...size, tf, motion: "off" });
  assert.equal(sc.edgeHits.length, vm.edges.filter((e) => lay.pos.get(e.src) && lay.pos.get(e.dst)).length);
  // the curve's middle hits this edge, or one running within the slop of it; far away hits nothing
  const at = sc.edgeScreen.get(id);
  assert.ok(at && Number.isFinite(at.x) && Number.isFinite(at.y));
  assert.equal(G.hitEdge(sc.edgeHits.filter((h) => h.id === id), at), id);
  assert.equal(G.hitEdge(sc.edgeHits, { x: -500, y: -500 }), null);
  assert.equal(G.hitEdge([], at), null);
  assert.equal(G.hitEdge(sc.edgeHits.filter((h) => h.id === id), { x: at.x + 40, y: at.y + 40 }, 5), null);

  const c = M.cardLines(vm, id);
  assert.equal(c.title, "core → dist-ci");
  assert.equal(c.subtitle, "msg");
  assert.deepEqual(c.rows[0], ["1h / 24h", "4 / 18"], "the rate window is no longer quoted: 1 h / 24 h");
  assert.deepEqual(c.links.map((l) => l.url), ["https://github.com/example-org/repo-one/pull/103", null], "newest first, a url only when the event has one");
  assert.ok(c.links[0].text.endsWith("PR 103 rebased; please rerun the GPU leg"), c.links[0].text);
  assert.equal(c.command, null);
  assert.equal(M.cardLines(vm, "nope|nope|msg"), null);
  // an edge from a feed without `recent` still has its card
  const bare = structuredClone(doc);
  for (const e of bare.edges) delete e.recent;
  assert.deepEqual(M.cardLines(M.buildModel(bare, A), id).links, []);

  // hovering an edge keeps it and its ends bright: a node hover's fade, on one edge
  const hov = S.buildScene(vm, lay, { ...size, tf, motion: "off", hover: id });
  assert.notDeepEqual(hov.items.filter((it) => it.t === "path" && it.id.startsWith("ec-")).map((it) => it.bg), sc.items.filter((it) => it.t === "path" && it.id.startsWith("ec-")).map((it) => it.bg));

  // a new event on the open edge redraws; on a closed one it does not
  const d2 = structuredClone(doc);
  const e2 = d2.edges.find((e) => e.id === id);
  e2.recent.unshift({ ts: "2026-11-02T20:00:30Z", age_s: 10, kind: "info", summary: "newer" });
  e2.n_24h += 1;
  assert.notEqual(M.visualSignature(M.buildModel(d2, A), id), M.visualSignature(vm, id));
  assert.equal(M.visualSignature(M.buildModel(d2, A)), M.visualSignature(vm));
});

test("the legend: five groups, what is on screen, the full key one click away", () => {
  assert.deepEqual(M.LEGEND_GROUPS.map((g) => g.id), ["you", "working", "waiting", "idle", "off"]);
  const grouped = M.LEGEND_GROUPS.flatMap((g) => g.keys);
  assert.equal(new Set(grouped).size, grouped.length, "a state is in one group");
  assert.deepEqual([...grouped].sort(), Object.keys(M.STATE_STYLE).sort(), "every state has a group");
  for (const key of M.LEGEND_GROUPS.find((g) => g.id === "waiting").keys) assert.equal(M.STATE_STYLE[key].color, M.STATE_STYLE["local-ci"].color, key);
  const colors = new Set(Object.values(M.STATE_STYLE).map((s) => s.color));
  assert.ok(colors.size <= 8, `state colors: ${[...colors]}`);
  const vm = M.buildModel(doc, A);
  const now = M.legendEntries(vm);
  const drawn = new Set(vm.nodes.filter((n) => n.kind === "role").map((n) => n.visual.key));
  assert.deepEqual(new Set(now.states.map((s) => s.key)), drawn, "only what is on screen");
  assert.ok(now.hiddenStates > 0);
  const full = M.legendEntries(vm, true);
  assert.equal(full.states.length, Object.keys(M.STATE_STYLE).length);
  assert.equal(full.hiddenStates, 0);
  assert.ok(now.edges.length <= 3 && now.edges[0].cls === "messages");
  assert.ok(!now.shapes.some(([, l]) => /claude|codex|opencode/.test(l)));
  assert.equal(new Set(Object.entries(M.EDGE_COLOR).filter(([c]) => c !== "blocked-on" && c !== "alias").map(([, v]) => v)).size, 1, "traffic is one color");
  assert.equal(M.legendEntries({ ...vm, hostColor: new Map([["host-a", "#fff"]]) }).hosts.length, 0, "one host needs no key");
});

test("a role with no work assigned goes quiet after the grace, unless it asks", () => {
  const role = (since, extra = {}, work = {}) => ({ kind: "role", id: "s:r", state: { class: "wait", event: "assignment", group: "human", since_s: since, needs_you: true, ...extra }, work });
  assert.equal(M.stateKey(role(600)), "assign");
  assert.equal(M.nodeVisual(role(600)).needsYou, true, "at first it does ask");
  assert.equal(M.stateKey(role(M.STARVED_AFTER_S)), "starved");
  const q = M.nodeVisual(role(M.STARVED_AFTER_S));
  assert.equal(q.needsYou, false);
  assert.equal(q.halo, null);
  assert.equal(q.urgency, null);
  assert.equal(M.stateKey(role(M.STARVED_AFTER_S, {}, { asks: ["ok?"] })), "assign", "an open question still counts");
  assert.equal(M.stateKey(role(M.STARVED_AFTER_S, { reasons: ["waiting-on-you"] })), "assign");
  const d = structuredClone(doc);
  const n = d.nodes.find((x) => x.id === `${A}:core`);
  n.state = { class: "wait", event: "assignment", group: "human", since_s: M.STARVED_AFTER_S + 5, needs_you: true };
  n.work = { asks: [] };
  const vm = M.buildModel(d, A);
  assert.ok(!vm.needsYou.some((r) => r.id === `${A}:core`));
  const s = vm.shoals.find((x) => x.id === A);
  assert.equal(s.starved, 1);
  assert.ok(M.cardLines(vm, M.SHOAL_CARD + A).rows.some(([k, v]) => k === "no work assigned" && v === "1"));
});

test("wait:self draws its cause, and is never its own state or a legend entry", () => {
  const role = (state, work = {}) => ({ kind: "role", id: "s:r", state: { class: "wait", event: "self", group: "self", ...state }, work });
  assert.equal(M.stateKey(role({})), "queued");
  assert.equal(M.stateKey(role({}, { asks: ["ok to merge?"] })), "ask");
  assert.equal(M.stateKey(role({ reasons: ["waiting-on-you"] })), "ask");
  assert.equal(M.stateKey(role({ reasons: ["blocked-on-question"], shoal_offline: true })), "prompt", "a question outranks offline");
  assert.equal(M.stateKey(role({ reasons: ["dead-worker"] })), "dead");
  assert.equal(M.stateKey(role({ reasons: ["parked-work"], shoal_offline: true, needs_you: true })), "held");
  assert.equal(M.nodeVisual(role({ reasons: ["parked-work"], shoal_offline: true, needs_you: true })).needsYou, false, "a held role asks nothing while you are away");
  assert.equal(M.stateKey(role({ reasons: ["dead-worker", "parked-work"], shoal_offline: true, needs_you: true })), "dead", "offline never hides a dead session");
  assert.equal(M.nodeVisual(role({ reasons: ["dead-worker"], shoal_offline: true, needs_you: true })).needsYou, true);
  assert.equal(M.nodeVisual(role({ reasons: ["waiting-on-you"], needs_you: true })).needsYou, true);
  const keys = M.legendEntries().states.map((s) => s.key);
  assert.ok(!keys.includes("self"));
  for (const k of ["queued", "held", "dead"]) assert.ok(keys.includes(k), k);
  assert.ok(M.STATE_RANK.every((k) => M.STATE_STYLE[k]), "every ranked state has a style");
  const card = M.cardLines(M.buildModel(doc, "global"), `${A}:addressing`);
  assert.ok(!card.rows.find(([k]) => k === "state")[1].includes("self"), "the raw event stays out of the card");
});

test("legend and labels: no retired relay, no writer square, no generation", () => {
  const L0 = M.legendEntries();
  assert.ok(!L0.shapes.some(([, label]) => /relay|shoal writer/.test(label)), JSON.stringify(L0.shapes));
  const vm = M.buildModel(doc, "global");
  const lay = L.layoutGraph(vm);
  const sc = S.buildScene(vm, lay, { width: 1200, height: 800, tf: S.fitScene(vm, lay, { width: 1200, height: 800 }), motion: "off" });
  for (const it of sc.items) if (it.t === "text") assert.ok(!/\br\d+\b/.test(it.text), it.text);
});

test("the middle of a shoal's circle is its status; a right-click there acts on the shoal", () => {
  const size = { width: 1200, height: 800 };
  for (const view of ["global", A]) {
    const vm = M.buildModel(doc, view);
    const lay = L.layoutGraph(vm);
    const sc = S.buildScene(vm, lay, { ...size, tf: S.fitScene(vm, lay, size), motion: "off" });
    const d = sc.discs.find((x) => x.id === A);
    assert.ok(d && d.hole > 0 && d.hole <= d.r, view);
    assert.equal(G.hitHole(sc.discs, { x: d.x, y: d.y }), A);
    assert.equal(G.hitDisc(sc.discs, { x: d.x + d.hole + 1, y: d.y }), A, "still inside the circle");
    assert.equal(G.hitHole(sc.discs, { x: d.x + d.hole + 1, y: d.y }), null);
    assert.equal(G.hitDisc(sc.discs, { x: -5000, y: -5000 }), null, "outside every circle: all shoals");
    const c = M.cardLines(vm, M.SHOAL_CARD + A);
    assert.equal(c.title, "alpha");
    assert.ok(c.rows.some(([k]) => k === "needs you") && c.rows.some(([k]) => k === "mode"));
    assert.equal(M.cardLines(vm, M.SHOAL_CARD + "nope"), null);
  }
  const g = M.buildModel(doc, "global");
  const lay = L.layoutGraph(g);
  const sc = S.buildScene(g, lay, { ...size, tf: S.fitScene(g, lay, size), motion: "off" });
  const t = sc.discs[0].title;
  assert.equal(G.hitDisc(sc.discs, { x: t.x + 1, y: t.y + 1 }), sc.discs[0].id, "the shoal's name counts too");
});

test("a role away from its shoal's home names its host in the global view", () => {
  const d = structuredClone(doc);
  const id = `${A}:core`;
  d.shoals.find((s) => s.id === A).home = "home-host";
  d.nodes.find((x) => x.id === id).host = "away-host";
  const size = { width: 1400, height: 900 };
  const labels = (view) => {
    const vm = M.buildModel(d, view);
    const lay = L.layoutGraph(vm);
    return S.buildScene(vm, lay, { ...size, tf: S.fitScene(vm, lay, size), motion: "off" }).items.filter((i) => i.t === "text").map((i) => i.text);
  };
  assert.equal(M.buildModel(d, "global").byId.get(id).awayHost, "away-host");
  assert.ok(labels("global").some((t) => t === "core @away-host"), "global: in the label");
  assert.ok(!labels(A).some((t) => t.includes("@")), "per shoal: the host bands say it");
  d.shoals.find((s) => s.id === A).home = "away-host";
  assert.equal(M.buildModel(d, "global").byId.get(id).awayHost, null, "at home: nothing to say");
});

test("a stale ledger clone is flagged, and a fresh or unknown one is not", () => {
  const d = structuredClone(doc);
  d.shoals[0].ledger_age_s = 4 * 86400;
  d.shoals[1].ledger_age_s = 30;
  const vm = M.buildModel(d, "global");
  assert.ok(M.ledgerStale(vm.shoals.find((s) => s.id === d.shoals[0].id)));
  assert.ok(!M.ledgerStale(vm.shoals.find((s) => s.id === d.shoals[1].id)));
  assert.ok(M.cardLines(vm, M.SHOAL_CARD + d.shoals[0].id).rows.some(([k, v]) => k === "ledger clone" && v.includes("4d")));
  assert.ok(!M.ledgerStale({ ledgerAge: null }));
  assert.notEqual(M.visualSignature(M.buildModel(doc, "global")), M.visualSignature(vm), "the warning redraws");
});

test("hiding: outside writers and parked roles only, and the count says how to bring them back", () => {
  const vm = M.buildModel(doc, "global");
  const ext = vm.nodes.find((x) => x.kind === "external");
  assert.equal(M.cardLines(vm, ext.id).hideable, true);
  assert.equal(M.cardLines(vm, `${A}:core`).hideable, false);
  assert.equal(M.cardLines(vm, `${B}:archive`).hideable, true, "a parked role");
  const h = M.buildModel(doc, "global", { hidden: [ext.id, 7, null] });
  assert.ok(!h.byId.has(ext.id));
  assert.equal(h.hiddenN, 1);
  assert.ok(!h.edges.some((e) => e.src === ext.id || e.dst === ext.id), "its edges go with it");
  assert.deepEqual(M.hiddenList(["a", 1, null, "b"]), ["a", "b"]);
  assert.deepEqual(M.hiddenList("x"), []);
});

test("removal: a role for good, an outside writer by its saved name, an aliased writer not at all", () => {
  const vm = M.buildModel(doc, "global");
  const exec = { kind: "exec", on: "lane-a" };
  const role = M.removeOverlay(vm, `${B}:archive`, exec);
  assert.deepEqual(role.props, { shoal: B, role: "archive", verb: "remove-role", machine: "lane-a" });
  const ext = vm.nodes.find((x) => x.kind === "external" && !x.alias_of);
  assert.deepEqual(M.removeOverlay(vm, ext.id, exec).props, { shoal: null, role: null, session: ext.label, verb: "remove-session", machine: "lane-a" });
  const aliased = vm.nodes.find((x) => x.kind === "external" && x.alias_of);
  if (aliased) assert.equal(M.removeOverlay(vm, aliased.id, exec), null);
  assert.equal(M.removeOverlay(vm, "user", exec), null);
  assert.equal(M.removeOverlay(vm, "nope", exec), null);
  assert.equal(M.removeOverlay(null, ext.id, exec), null);
  // a writer with no saved session has nothing to remove, and its card says so
  const d = structuredClone(doc);
  d.nodes.find((x) => x.id === ext.id).saved = false;
  const vm2 = M.buildModel(d, "global");
  assert.equal(M.removeOverlay(vm2, ext.id, exec), null);
  assert.ok(M.cardLines(vm2, ext.id).rows.some(([k, v]) => k === "session" && v.startsWith("none saved")));
  assert.equal(M.cardLines(vm2, ext.id).hideable, true);
  assert.ok(!M.cardLines(vm, ext.id).rows.some(([k]) => k === "session"));
});

test("a role's card says its context and whether it can restart now", () => {
  const vm = M.buildModel(doc, "global");
  const row = (id, k) => M.cardLines(vm, id).rows.find(([x]) => x === k);
  assert.ok(row(`${A}:core`, "context"));
  assert.equal(row(`${A}:core`, "restart-ready"), undefined, "mid-turn: not asked");
  assert.deepEqual(row(`${A}:addressing`, "restart-ready"), ["restart-ready", "no"]);
  const d = structuredClone(doc);
  d.nodes.find((x) => x.id === `${A}:addressing`).state.restart_ready = true;
  assert.deepEqual(M.cardLines(M.buildModel(d, "global"), `${A}:addressing`).rows.find(([x]) => x === "restart-ready"), ["restart-ready", "yes"]);
});

test("each shoal's boards reach the rail with a link and their counts", () => {
  const vm = M.buildModel(doc, "global");
  const a = vm.shoals.find((s) => s.id === A).boards;
  assert.equal(a.items[0].url, "https://github.com/users/acme/projects/2");
  assert.deepEqual(a.items[0].counts, { done: 134, open: 58, inFlight: 4, assigned: 10 });
  assert.deepEqual(a.offBoard, { inFlight: 1, assigned: 2 });
  assert.deepEqual(M.boardCells(a.items[0].counts).map((c) => c.n), [134, 58, 4, 10]);
  assert.deepEqual(M.boardCells(a.items[0].counts).map((c) => c.label), ["done", "open", "in flight", "assigned"]);
  assert.ok(M.boardCells(a.items[0].counts).every((c) => c.about.length > 0), "each cell says what it counts");
  // both views carry it
  assert.equal(M.buildModel(doc, A).shoalsInView[0].boards.items.length, 1);
  // a board this host has not read is still a link, named by its id
  const b = vm.shoals.find((s) => s.id === B).boards.items[0];
  assert.equal(b.counts, null);
  assert.equal(b.title, "acme/3");
});

test("a board's note says why it has no counts, and a bad link is dropped", () => {
  assert.equal(M.boardsOf({ home: "host-b" }, "host-a").note, "counts are read on host-b");
  assert.equal(M.boardsOf({ home: "host-a" }, "host-a").note, "no board read yet");
  assert.deepEqual(M.boardsOf({}).items, []);
  assert.deepEqual(M.boardsOf({ boards: { items: [{ id: "x", url: "javascript:alert(1)" }, { id: "y" }] } }).items, []);
});

test("the board bar fills its width and keeps a small cell visible", () => {
  const widths = (c, w) => M.boardBar(c, w).map((s) => s.w);
  const big = widths({ done: 500, open: 300, inFlight: 1, assigned: 1 }, 226);
  assert.equal(big.reduce((t, x) => t + x, 0), 226);
  assert.ok(big[2] >= 2 && big[3] >= 2);
  assert.deepEqual(widths({ done: 0, open: 0, inFlight: 0, assigned: 0 }, 226), []);
  assert.deepEqual(widths({ done: 0, open: 3, inFlight: 0, assigned: 0 }, 226), [226]);
});

test("a shoal's name: its board's numbers on hover, and the same numbers in the ring's hole", () => {
  const vm = M.buildModel(doc, "global");
  const c = M.cardLines(vm, M.BOARD_CARD + A);
  assert.equal(c.title, "alpha priorities");
  assert.deepEqual(c.boards[0].counts, { done: 134, open: 58, inFlight: 4, assigned: 10 });
  assert.ok(c.rows.some(([k, v]) => k === "off the board" && v === "+1 in flight, +2 assigned"));
  assert.ok(c.rows.some(([k, v]) => k === "read" && v === "4m ago"));
  assert.equal(M.cardLines(vm, M.SHOAL_CARD + A).boards.length, 1);
  // a board with no read here still says why
  const b = M.cardLines(vm, M.BOARD_CARD + B);
  assert.deepEqual(b.boards, []);
  assert.ok(b.rows.some(([k]) => k === "counts"));
  assert.equal(M.boardSummary(c.boards[0].counts), "134 done · 58 open · 4 in flight · 10 assigned");
  assert.equal(M.offBoardText({ inFlight: 0, assigned: 0 }), "");
  // no board: no card, and hitTitle finds the name only
  const d = structuredClone(doc);
  delete d.shoals[0].boards;
  assert.equal(M.cardLines(M.buildModel(d, "global"), M.BOARD_CARD + A), null);
  const discs = [{ id: "s", x: 0, y: 0, r: 50, title: { x: -20, y: -70, w: 40, h: 16 } }];
  assert.equal(G.hitTitle(discs, { x: 0, y: -60 }), "s");
  assert.equal(G.hitTitle(discs, { x: 0, y: 0 }), null, "the disc itself drills in");
  assert.equal(G.hitDisc(discs, { x: 0, y: 0 }), "s");
});

test("a standalone session's card says which it is and whether it runs; a blocked session's own words reach you", () => {
  const d = structuredClone(doc);
  const ext = d.nodes.find((x) => x.id === "ext:evolution");
  ext.standalone = { harness: "claude", id: "abcdef123456", state: "alive" };
  const vm = M.buildModel(d, "global");
  const row = (k) => M.cardLines(vm, ext.id).rows.find(([x]) => x === k)?.[1];
  assert.equal(row("session"), "claude abcdef123456");
  assert.equal(row("now"), "alive, between turns");
  assert.equal(row("app"), undefined);
  ext.standalone.dismissed = true;
  assert.equal(M.cardLines(M.buildModel(d, "global"), ext.id).rows.find(([x]) => x === "app")[1], "dismissed: no space of its own");
  ext.standalone.state = "unread";
  assert.equal(M.cardLines(M.buildModel(d, "global"), ext.id).rows.find(([x]) => x === "now")[1], "not read here");
  const n = d.nodes.find((x) => x.kind === "role" && x.shoal === A);
  n.state = { ...n.state, says: "reply yes to apply it" };
  n.work = { ...n.work, asks: [] };
  assert.ok(M.cardLines(M.buildModel(d, "global"), n.id).list.includes("? reply yes to apply it"));
  const r = M.needsYouList([{ ...n, state: { ...n.state, class: "wait", event: "session-blocked", needs_you: true } }]);
  assert.equal(r[0]?.text, "reply yes to apply it");
});

test("a dismissed role is quiet on the lattice, and its ask is still on its card", () => {
  const d = structuredClone(doc);
  const n = d.nodes.find((x) => x.kind === "role" && x.shoal === A);
  n.state = { ...n.state, class: "wait", event: "user-ask", needs_you: false, dismissed: true };
  n.work = { ...n.work, asks: ["ok to go?"] };
  const vm = M.buildModel(d, "global");
  const got = vm.byId.get(n.id);
  assert.equal(got.visual.key, "dismissed");
  assert.equal(got.visual.needsYou, false);
  assert.ok(!vm.needsYou.some((r) => r.id === n.id), "off the rail's list");
  assert.ok(M.cardLines(vm, n.id).list.includes("? ok to go?"));
  assert.ok(M.LEGEND_GROUPS.find((g) => g.id === "idle").keys.includes("dismissed"));
});

test("a satellite can be hovered and clicked: its card names the item, its title and what it waits on", () => {
  const d = structuredClone(doc);
  const n = d.nodes.find((x) => x.kind === "role" && x.work?.next?.length);
  n.work.next[0] = { ...n.work.next[0], title: "Make it fast", url: "https://github.com/acme/repo/issues/7" };
  const vm = M.buildModel(d, "global");
  const id = `${n.id}#${vm.byId.get(n.id).items.findIndex((x) => x.kind === "next")}`;
  const c = M.cardLines(vm, id);
  assert.equal(c.title, n.work.next[0].item);
  assert.ok(c.rows.some(([k, v]) => k === "title" && v === "Make it fast"));
  assert.ok(c.rows.some(([k]) => k === "blocked on"));
  assert.deepEqual(c.links, [{ kind: "next", text: "open it", url: "https://github.com/acme/repo/issues/7" }]);
  assert.equal(M.itemOf(vm, `${n.id}#99`), null);
  // a click opens only a web link: item urls are ledger text
  assert.equal(M.webUrl("file:///etc/passwd"), null);
  assert.equal(M.webUrl("javascript:alert(1)"), null);
  assert.equal(M.webUrl("HTTPS://github.com/x"), "HTTPS://github.com/x");
  const bad = structuredClone(d);
  bad.nodes.find((x) => x.id === n.id).work.next[0].url = "vscode://open?x";
  const bvm = M.buildModel(bad, "global");
  assert.deepEqual(M.cardLines(bvm, id).links, []);
  assert.equal(M.itemOf(vm, "nope"), null);
  // the scene gives every satellite a hit target, and a blocked next item is a ring
  const lay = L.layoutGraph(vm);
  const sc = S.buildScene(vm, lay, { width: 1000, height: 700, tf: G.identity() });
  assert.equal(sc.satHits.length, lay.sats.length);
  const blocked = lay.sats.some((s) => s.kind === "next" && s.blocker && !s.stale);
  assert.equal(sc.items.some((it) => it.id.startsWith("sat-ring-")), blocked);
  assert.ok(sc.satScreen.get(id), "a satellite's card has a place on screen");
  const at = sc.satScreen.get(id);
  assert.equal(G.hitNode(sc.satHits, { x: at.x + 1, y: at.y }, 2), id, "a point on the dot hits it");
});

test("replay: the day's events in order, binned by quarter hour, stepped and sought", () => {
  assert.equal(R.replayModel({}), null);
  assert.equal(R.replayModel({ replay: { from: "2026-01-02T00:00:00Z", to: "2026-01-01T00:00:00Z", events: [] } }), null);
  const ev = (seq, ts) => ({ seq, ts, src: "a", dst: "b", type: "msg", class: "msg" });
  const rm = R.replayModel({ replay: { from: "2026-01-01T00:00:00Z", to: "2026-01-02T00:00:00Z", dropped: 3,
    events: [ev(2, "2026-01-01T06:10:00Z"), ev(1, "2026-01-01T00:05:00Z"), ev(3, "2026-01-01T06:11:00Z"), ev(4, "2027-01-01T00:00:00Z"), ev(5, "bad")] } });
  assert.deepEqual(rm.events.map((e) => e.seq), [1, 2, 3], "in time order; out of the day or unreadable: left out");
  assert.equal(rm.bins.length, R.REPLAY_BINS);
  assert.equal(rm.bins[0], 1);
  assert.equal(rm.bins[24], 2, "06:10 and 06:11 share the 06:00 quarter hour");
  assert.equal(rm.max, 2);
  assert.equal(rm.dropped, 3);
  assert.deepEqual(R.replayStep(rm, rm.from, rm.from + 6 * 3600 + 600).events.map((e) => e.seq), [1, 2]);
  assert.equal(R.replayStep(rm, rm.from + 6 * 3600 + 600, rm.to).n, 1, "a step takes what is after its start");
  assert.equal(R.replayAdvance(rm) * (1000 / R.REPLAY_TICK_MS) * R.REPLAY_DAY_S, 86400, "a day in REPLAY_DAY_S");
  assert.equal(R.binOf(rm, R.binStart(rm, 24)), 24);
  assert.equal(R.clock(rm.from + 6 * 3600 + 600), "06:10Z");
  const many = { replay: { from: "2026-01-01T00:00:00Z", to: "2026-01-02T00:00:00Z", events: Array.from({ length: 40 }, (_, i) => ev(i + 1, "2026-01-01T01:00:00Z")) } };
  const st = R.replayStep(R.replayModel(many), 0, Infinity);
  assert.equal(st.n, 40);
  assert.equal(st.events.length, R.REPLAY_BURST, "a busy quarter hour is counted, not flooded");
  const own = R.replayOfDoc(doc);
  assert.ok(own && own.to === doc.ts && Array.isArray(own.events), "the fixture replays its own events");
});

test("depth: planes stacked under a tilted camera; flat is the layout as is", () => {
  assert.equal(D.camera("flat"), null);
  const lay = L.layoutGraph(global);
  assert.equal(D.projectLayout(global, lay, null), lay, "flat: untouched");
  const cam = D.camera("tilt");
  const q = D.project(cam, { x: 10, y: 20 }, 0), up = D.project(cam, { x: 10, y: 20 }, 100);
  assert.ok(Math.abs(q.y - 20 * Math.cos(cam.pitch)) < 1e-9 && up.y < q.y && up.depth > q.depth, "a higher plane is drawn higher and nearer");
  const far = D.project({ ...cam, d: 500 }, { x: 100, y: -200 }, 0), near = D.project({ ...cam, d: 500 }, { x: 100, y: 200 }, 0);
  assert.ok(far.f < 1 && near.f > 1, "in perspective, the far side is smaller and the near side larger");
  const { zOf, planes } = D.planesOf(global, lay);
  assert.equal(planes.at(-1).kind, "operator");
  assert.ok(zOf.get("user") > Math.max(...planes.filter((p) => p.kind === "shoal").map((p) => p.z)), "the operator above the shoals");
  const pl = D.projectLayout(global, lay, cam);
  assert.equal(pl.pos.size, lay.pos.size);
  assert.ok(finiteDeep([...pl.pos.values()]) && finiteDeep(pl.sats) && pl.clusters.every((c) => c.ry === 1), "global: spheres, circles at any tilt");
  assert.ok(pl.grid.length === 22 && finiteDeep(pl.grid), "a floor grid, ten lines each way plus the edges");
  assert.ok(pl.stems.some((s) => s.id === "n-user") && pl.stems.every((s) => s.y1 >= s.y0 - 1e-9), "stems drop to the floor, the operator's too");
  assert.ok([...pl.fogOf.values()].every((f) => f >= 0.55 * 0.3 - 1e-9 && f <= 1 + 1e-9), "far fades; round the back of a sphere, more");
  assert.equal(pl.order.length, global.nodes.length);
  for (let i = 1; i < pl.clusters.length; i++) assert.ok(pl.clusters[i - 1].depth <= pl.clusters[i].depth, "far to near");
  assert.ok([...pl.scaleOf.values()].every((s) => s >= 0.55 * 0.66 - 1e-9 && s <= 1.6 * 1.28 + 1e-9));
  assert.ok(Math.max(...pl.scaleOf.values()) / Math.min(...pl.scaleOf.values()) > 1.15, "perspective: sizes differ visibly with depth");
  // per shoal: the cutout sphere, each host a lune of its inner surface
  const la = L.layoutGraph(alpha);
  const pa = D.projectLayout(alpha, la, D.camera("iso"));
  assert.ok(pa.bowl && pa.bands.every((b) => b.bowl && b.a1 > b.a0), "per shoal: a bowl, its hosts as lunes");
  // the scene draws either, with no NaN, and slab edges only in depth
  for (const [vm, l] of [[global, pl], [alpha, pa]]) {
    const sc = S.buildScene(vm, l, { width: 1000, height: 700, tf: G.identity() });
    assert.ok(finiteDeep(sc.items), "no NaN in the depth scene");
    assert.ok(sc.items.some((it) => it.id.startsWith("sphere-body-") || it.id.startsWith("bowl-host-")), "a sphere (global) or a host lune (per shoal)");
    assert.ok(sc.items.some((it) => it.id === "floor-grid") && sc.items.some((it) => it.id.startsWith("stem-")), "the floor and the stems");
  }
  assert.ok(!S.buildScene(global, lay, { width: 1000, height: 700, tf: G.identity() }).items.some((it) => it.id.startsWith("disc-slab-")));
  // spheres: global depth view; the role that needs you most sits on the side facing you
  assert.ok(pl.clusters.every((c) => c.sphere && c.ry === 1), "each shoal a sphere");
  const sc2 = S.buildScene(global, pl, { width: 1000, height: 700, tf: G.identity() });
  assert.ok(sc2.items.some((it) => it.id.startsWith("sphere-rim-")) && !sc2.items.some((it) => it.id.startsWith("disc-rim-")));
  const front = D.spherePoint(cam, 0, 5);
  assert.ok(Math.abs(front.z - cam.c) < 1e-9 && Math.hypot(front.x, front.y, front.z) > 0.999, "rank 0 faces the camera");
  const backmost = D.spherePoint(cam, 4, 5);
  const toward = (u) => u.x * 0 + (u.x * cam.syaw + u.y * cam.cyaw) * cam.s + u.z * cam.c;
  assert.ok(toward(front) > toward(backmost), "later ranks are further round");
  const mk = (key, extra = {}) => ({ id: key, visual: { key, needsYou: false, stall: false, urgency: null, ...extra }, metrics: { last_write_age_s: 60 } });
  const order = [mk("queued"), mk("working"), mk("ask", { needsYou: true, urgency: 1 }), mk("local-ci"), mk("auth", { needsYou: true, urgency: 0 })]
    .sort((a, b) => { const x = D.sphereRank(a), y = D.sphereRank(b); return x[0] - y[0] || x[1] - y[1] || x[2] - y[2]; }).map((n) => n.id);
  assert.deepEqual(order, ["auth", "ask", "working", "local-ci", "queued"], "need first, then activity");
  const ranked = global.nodes.filter((n) => n.kind === "role" && n.shoal === A)
    .sort((a, b) => { const x = D.sphereRank(a), y = D.sphereRank(b); return x[0] - y[0] || x[1] - y[1] || x[2] - y[2] || (a.id < b.id ? -1 : 1); });
  const at = (n) => pl.order.findIndex((x) => x.id === n.id); // order runs far to near
  assert.ok(at(ranked[0]) > at(ranked.at(-1)), "the role that needs you most is drawn nearer than the least");
  assert.ok(pl.fogOf.get(ranked[0].id) > pl.fogOf.get(ranked.at(-1).id), "and the one round the back is fainter");
  assert.equal(D.depthFor("tilt", "off", "cpu"), "flat", "a CPU renderer at off stays flat");
  assert.equal(D.depthFor("tilt", "full", "gpu"), "tilt");
  assert.equal(D.nextDepth("iso"), "flat");
});

test("a change on a board redraws the rail", () => {
  const before = M.visualSignature(M.buildModel(doc, "global"));
  const d = structuredClone(doc);
  d.shoals[0].boards.items[0].counts.in_flight += 1;
  assert.notEqual(M.visualSignature(M.buildModel(d, "global")), before);
});

console.log(`lattice-view: ${n} tests passed`);

test("2026-11-08: replay speeds, rates per 24 h, the edge card's 1 h / 24 h", () => {
  const rm = R.replayModel({ replay: { from: "2026-01-01T00:00:00Z", to: "2026-01-02T00:00:00Z", events: [] } });
  assert.equal(R.REPLAY_DAY_S, 32, "three times faster than 96 s");
  assert.equal(R.replayAdvance(rm) * (1000 / R.REPLAY_TICK_MS) * 32, 86400);
  assert.equal(R.replayAdvance(rm, 3.2) / R.replayAdvance(rm, 32), 10, "a picked speed");
  assert.deepEqual(R.REPLAY_SPEEDS.map((s) => s.day_s), [96, 32, 11, 3.2]);
  const e = alpha.edges.find((x) => x.n_24h > 0) ?? alpha.edges[0];
  const card = M.cardLines(alpha, e.id);
  assert.deepEqual(card.rows[0], ["1h / 24h", `${e.n_1h} / ${e.n_24h}`]);
  assert.ok(!card.rows.some(([k]) => k === "rate"));
  assert.ok(M.hostSummaries(global).every((h) => typeof h.msgs24 === "number"));
  assert.ok(M.shoalCardLines(global, A).rows.some(([k]) => k === "msgs 1h / 24h"));
});

test("2026-11-08: spheres that pop (a limb, a hot spot), front larger than back", () => {
  const cam = D.camera("iso");
  const pl = D.projectLayout(global, L.layoutGraph(global), cam);
  const sc = S.buildScene(global, pl, { width: 1000, height: 700, tf: G.identity() });
  assert.ok(sc.items.some((it) => it.id.startsWith("sphere-limb-")) && sc.items.some((it) => it.id.startsWith("sphere-spec-")));
  const ranked = global.nodes.filter((n) => n.kind === "role" && n.shoal === A)
    .sort((a, b) => { const x = D.sphereRank(a), y = D.sphereRank(b); return x[0] - y[0] || x[1] - y[1] || x[2] - y[2] || (a.id < b.id ? -1 : 1); });
  assert.ok(pl.scaleOf.get(ranked[0].id) > pl.scaleOf.get(ranked.at(-1).id) * 1.2, "the front of a sphere is clearly larger than its back");
});

test("globe lighting follows surface normals and stays inside the silhouette", () => {
  assert.ok(Math.abs(SP.lightAt(SP.LIGHT.x, SP.LIGHT.y) - 1) < 1e-9);
  assert.ok(SP.lightAt(0.7, 0.7) < 0, "the lower right is in shadow");
  const shade = SP.sphereShade("test", 0, 0, 1, "#50d090");
  assert.equal(shade.paths.length, SP.STEPS, "one contour per glass layer");
  for (let i = 0; i < shade.paths.length; i++) {
    assert.ok(shade.paths[i].cmds.length <= 8, "the native render budget needs short curved contours");
    let p0;
    for (const cmd of shade.paths[i].cmds) {
      if (cmd[0] === "M") p0 = { x: cmd[1], y: cmd[2] };
      if (cmd[0] !== "C") continue;
      const curve = { p0, c1: { x: cmd[1], y: cmd[2] }, c2: { x: cmd[3], y: cmd[4] }, p3: { x: cmd[5], y: cmd[6] } };
      for (let j = 0; j <= 20; j++) {
        const { x, y } = G.pointAt(curve, j / 20);
        assert.ok(Math.hypot(x, y) <= 1.0003, "cubic approximation stays within a subpixel of the limb");
        assert.ok(SP.lightAt(x, y) >= i / shade.paths.length - 0.015, "contours respect the light level");
      }
      p0 = curve.p3;
    }
  }
});

test("curved light contours enclose the lit cap, including the clipped limb", () => {
  const shade = SP.sphereShade("test", 0, 0, 1, "#50d090");
  const N = shade.paths.length;
  for (const level of [0, 5, 13, 20, 28, 29, 35, N - 1]) {
    const points = [];
    let p0;
    for (const c of shade.paths[level].cmds) {
      if (c[0] === "M") { p0 = { x: c[1], y: c[2] }; points.push(p0); }
      if (c[0] !== "C") continue;
      const curve = { p0, c1: { x: c[1], y: c[2] }, c2: { x: c[3], y: c[4] }, p3: { x: c[5], y: c[6] } };
      for (let j = 1; j <= 24; j++) points.push(G.pointAt(curve, j / 24));
      p0 = curve.p3;
    }
    for (let x = -0.95; x < 1; x += 0.1) for (let y = -0.95; y < 1; y += 0.1) {
      if (Math.hypot(x, y) >= 0.995 || Math.abs(SP.lightAt(x, y) - level / N) < 0.015) continue;
      let inside = false;
      for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
        const a = points[i], b = points[j];
        if ((a.y > y) !== (b.y > y) && x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x) inside = !inside;
      }
      assert.equal(inside, SP.lightAt(x, y) > level / N, `light cap ${level} at ${x}, ${y}`);
    }
  }
  const moved = SP.sphereShade("test", 73, -29, 190, "#50d090");
  for (let i = 0; i < shade.paths.length; i++) for (let j = 0; j < shade.paths[i].cmds.length; j++) {
    const a = shade.paths[i].cmds[j], b = moved.paths[i].cmds[j];
    assert.equal(a[0], b[0]);
    for (let k = 1; k < a.length; k++) assert.ok(Math.abs(b[k] - ((k % 2 ? 73 : -29) + 190 * a[k])) < 1e-10);
  }
});

test("turning a globe reveals other roles without detaching them or their grid", () => {
  const base = L.layoutGraph(global);
  const a = D.projectLayout(global, base, D.camera("iso"));
  const b = D.projectLayout(global, base, D.camera("iso", Math.PI / 3));
  const first = [...a.surfaceOf.keys()][0];
  assert.ok(Math.hypot(a.pos.get(first).x - b.pos.get(first).x, a.pos.get(first).y - b.pos.get(first).y) > 1);
  assert.notEqual(a.surfaceOf.get(first).facing, b.surfaceOf.get(first).facing, "the facing changes with the camera");
  for (const lay of [a, b]) for (const c of lay.clusters) {
    for (const [id, surface] of lay.surfaceOf) if (surface.shoal === c.id) {
      const p = lay.pos.get(id);
      assert.ok(Math.hypot(p.x - c.x, p.y - c.y) <= c.r + 1e-8, "roles remain on the globe");
    }
    for (const g of c.globeGrid) for (const p of g.points)
      assert.ok(Math.hypot(p.x - c.x, p.y - c.y) <= c.r + 1e-8, "the grid shares the silhouette");
  }
  const sc = S.buildScene(global, a, { width: 1000, height: 700, tf: G.identity(), motion: "off" });
  assert.ok(sc.items.some((x) => x.id.startsWith("contact-")), "front roles cast contact shadows");
  assert.equal(sc.animated, 0, "depth cues do not introduce animation at off");
});

test("2026-11-08: a renamed standalone seat's Remove uses its saved label", () => {
  const d = structuredClone(doc);
  d.nodes.push({ id: "ext:old-label", kind: "external", label: "new-name", seat: "old-label", saved: true, alias_of: null, state: { class: "unknown" } });
  const vm = M.buildModel(d, "global");
  assert.equal(M.removeOverlay(vm, "ext:old-label").props.session, "old-label");
  assert.equal(M.actionsOverlay(vm, "ext:old-label").props.session, "old-label", "Handoff uses the same saved seat identity");
});

const alphaOf = (hex) => parseInt(String(hex).slice(7, 9) || "ff", 16) / 255;
const cmdCount = (paths) => paths.reduce((t, p) => t + p.cmds.length, 0);
// what a stack of layers leaves of the background: 1 minus the opacity the layers add up to
const opacityOf = (colors) => 1 - colors.reduce((w, c) => w * (1 - alphaOf(c)), 1);

test("2026-11-10: glass globes, much clearer idle and a little more opaque in focus, in smooth steps", () => {
  assert.ok(SP.GLASS.idle.lit <= 0.4 && SP.GLASS.idle.body <= 0.1, "idle: mostly clear");
  assert.ok(SP.GLASS.focus.lit > SP.GLASS.idle.lit && SP.GLASS.focus.lit - SP.GLASS.idle.lit <= 0.3, "focus: a little more opaque");
  for (const focused of [false, true]) {
    const g = SP.glassOf(focused), shade = SP.sphereShade("t", 0, 0, 1, "#50d090", 1, focused);
    const layers = shade.paths.map((p) => p.bg.color);
    assert.ok(Math.abs(alphaOf(shade.body) - g.body) < 0.01, "the shadow side is the body alone");
    assert.ok(Math.abs(opacityOf([shade.body, ...layers]) - g.lit) < 0.04, "the lit pole adds up to the lit opacity");
    assert.ok(layers.every((c) => alphaOf(c) <= 7 / 255), "each step adds under 3 percent: no visible bands");
    assert.ok(cmdCount(shade.paths) <= 848 / 2, "half the command budget PR #149 left a globe");
  }
  // the color the eye sees: more tint in focus, the background alone off the globe
  const idle = SP.glassColorAt("#50d090", -0.4, -0.45), focus = SP.glassColorAt("#50d090", -0.4, -0.45, { focused: true });
  assert.ok(G.contrast(focus, "#1d2a3c") > G.contrast(idle, "#1d2a3c") && G.contrast(idle, "#1d2a3c") > 1.2);
  assert.equal(SP.glassColorAt("#50d090", 1.2, 0), "#1d2a3c");
});

test("2026-11-10: a globe in focus: the pointer on it, its middle or name, its roles, their items, an edge's ends", () => {
  const role = global.nodes.find((n) => n.kind === "role" && n.shoal === A);
  const cross = global.edges.find((e) => global.byId.get(e.src)?.shoal && global.byId.get(e.dst)?.shoal && global.byId.get(e.src).shoal !== global.byId.get(e.dst).shoal);
  assert.deepEqual([...S.focusedShoals(global, S.GLOBE + A)], [A]);
  assert.deepEqual([...S.focusedShoals(global, M.SHOAL_CARD + B)], [B]);
  assert.deepEqual([...S.focusedShoals(global, M.BOARD_CARD + A)], [A]);
  assert.deepEqual([...S.focusedShoals(global, role.id)], [A]);
  assert.deepEqual([...S.focusedShoals(global, `${role.id}#0`)], [A], "a satellite's owner");
  if (cross) assert.equal(S.focusedShoals(global, cross.id).size, 2, "an edge across shoals lifts both");
  assert.equal(S.focusedShoals(global, null).size, 0);
  assert.equal(S.focusedShoals(global, "nothing").size, 0);
  // the hovered globe draws more opaque; the other is untouched
  const pl = D.projectLayout(global, L.layoutGraph(global), D.camera("iso"));
  const layerAlpha = (sc, sid) => sc.items.filter((it) => it.id.startsWith(`sphere-lit-${sid}-`)).reduce((t, it) => t + alphaOf(it.bg.color), 0);
  const base = S.buildScene(global, pl, { width: 1000, height: 700, tf: G.identity(), motion: "off" });
  const hov = S.buildScene(global, pl, { width: 1000, height: 700, tf: G.identity(), motion: "off", hover: S.GLOBE + A });
  assert.ok(layerAlpha(hov, A) > layerAlpha(base, A) && layerAlpha(hov, B) === layerAlpha(base, B));
  assert.ok(base.discs.every((d) => d.sphere), "each globe is a disc the pointer can hover");
  // the pointer where two globes overlap: the nearer (later) one
  assert.equal(G.hitGlobe([{ id: "far", x: 0, y: 0, r: 10, sphere: true }, { id: "near", x: 5, y: 0, r: 10, sphere: true }, { id: "flat", x: 0, y: 0, r: 50 }], { x: 3, y: 0 }), "near");
  assert.equal(G.hitGlobe([{ id: "flat", x: 0, y: 0, r: 50 }], { x: 0, y: 0 }), null, "a flat disc is not a globe");
});

test("2026-11-10: a label on a globe or in the bowl gets the ink that reads there, idle or in focus", () => {
  assert.equal(G.readableInk("#ffffff"), G.INK_DARK);
  assert.equal(G.readableInk("#0a0f18"), G.INK_LIGHT);
  assert.ok(G.contrast(G.readableInk("#7a8a70"), "#7a8a70") >= 4.5, "a mid surface still gets 4.5:1");
  const accent = G.readableInk("#d0d0d0", "#ff4f9a");
  assert.ok(G.contrast(accent, "#d0d0d0") >= 3 && accent !== G.INK_DARK, "a needs-you label keeps its hue");
  const cases = [
    [global, D.projectLayout(global, L.layoutGraph(global), D.camera("iso")), null],
    [global, D.projectLayout(global, L.layoutGraph(global), D.camera("iso")), S.GLOBE + A],
    [alpha, D.projectLayout(alpha, L.layoutGraph(alpha), D.camera("tilt")), null],
  ];
  for (const [vm, lay, hover] of cases) {
    const tf = S.fitScene(vm, lay, { width: 1100, height: 800 });
    const sc = S.buildScene(vm, lay, { width: 1100, height: 800, tf, hover, motion: "off" });
    const surf = [];
    for (const c of lay.clusters) {
      const p = G.toScreen(tf, c), r = c.r * tf.k;
      if (c.sphere) surf.push({ kind: "globe", x: p.x, y: p.y, r, tint: c.tint, fog: 0.72 + 0.28 * (c.fog ?? 1), focused: S.focusedShoals(vm, hover).has(c.id) });
      if (c.bowl) surf.push({ kind: "bowl", x: p.x, y: p.y, r, frame: lay.bowl.frame, bands: lay.bands });
    }
    let on = 0;
    for (const it of sc.items.filter((x) => x.id.startsWith("label-") && vm.byId.get(x.id.slice(6))?.kind === "role")) {
      const under = S.surfaceUnder(surf, it.x + it.w / 2 - 5, it.y + it.h / 2);
      if (!under) continue;
      on++;
      if (vm.byId.get(it.id.slice(6)).visual.needsYou) continue;
      assert.ok(G.contrast(it.color.slice(0, 7), under) >= 4.5, `${it.text} reads on its surface`);
    }
    assert.ok(on >= 3, "labels sit on the surfaces");
  }
});

test("2026-11-10: per shoal in depth, the cutout sphere: you at its middle, a lune per host, its roles on it", () => {
  const la = L.layoutGraph(alpha);
  assert.ok(D.bowlTilt(D.camera("iso")) > D.bowlTilt(D.camera("tilt")), "a steeper camera tips the opening up more");
  for (const cam of [D.camera("tilt"), D.camera("iso"), D.camera("iso", Math.PI / 3), D.camera("tilt", -Math.PI / 4)]) {
    const pa = D.projectLayout(alpha, la, cam), fr = pa.bowl.frame, Rs = pa.bowl.R;
    assert.ok(Math.abs(fr.alpha - D.bowlTilt(cam)) < 1e-12 && pa.clusters[0].bowl && pa.clusters[0].r === Rs);
    const user = alpha.nodes.find((n) => n.kind === "user");
    assert.deepEqual(pa.pos.get(user.id), { x: 0, y: 0 }, "you at the middle of the sphere");
    const hosts = [...new Set(alpha.nodes.filter((n) => n.kind === "role").map((n) => n.host ?? "?"))].sort();
    assert.deepEqual(pa.bands.map((b) => b.host).sort(), hosts, "one lune per host");
    const TAU = 2 * Math.PI;
    const lunes = [...pa.bands].sort((a, b) => a.a0 - b.a0);
    for (let i = 1; i < lunes.length; i++) assert.ok(Math.abs(lunes[i].a0 - lunes[i - 1].a1) < 1e-9, "lunes meet: no overlap, no gap");
    assert.ok(Math.abs(lunes.at(-1).a1 - lunes[0].a0 - TAU) < 1e-9, "and close the turn");
    for (const n of alpha.nodes.filter((x) => x.kind === "role")) {
      const s = pa.surfaceOf.get(n.id), p = pa.pos.get(n.id), b = pa.bands.find((x) => x.host === n.host);
      assert.equal(s.host, n.host, `${n.label} on its own host's lune`);
      assert.ok(s.phi >= b.a0 - 1e-9 && s.phi <= b.a1 + 1e-9, `${n.label} inside its lune's azimuths`);
      const u = SP.bowlPoint(fr, s.theta, s.phi);
      const q = SP.bowlProject(fr, u);
      assert.ok(Math.hypot(p.x - Rs * q.x, p.y - Rs * q.y) < 1e-9 && s.theta < Math.PI / 2, "on the inner surface, in perspective");
      assert.ok(SP.bowlSeen(fr, u) <= 0.86 + 1e-6 && SP.bowlKept(fr, u), "on the inside, seen through the rim");
      assert.ok(u.z <= -0.2 + 1e-9, `${n.label} sits behind you`);
      assert.equal(S.bowlHostAt(fr, pa.bands, p.x / Rs, p.y / Rs)?.host, n.host, "the lune drawn under it is its host's");
      assert.ok(pa.order.indexOf(n) < pa.order.indexOf(user), "you are drawn nearer than every role");
    }
    for (const n of alpha.nodes.filter((x) => x.kind === "external" || x.kind === "writer")) {
      const q = pa.pos.get(n.id);
      assert.ok(SP.rimNorm(fr, { x: q.x / Rs, y: q.y / Rs }) > 1.15, `outside seats outside the rim: ${n.label}`);
    }
    for (const t of pa.tethers.filter((x) => x.to)) assert.equal(S.bowlHostAt(fr, pa.bands, t.to.x / Rs, t.to.y / Rs)?.host, t.host, "a CI tether ends on its host's lune");
    // the scene: a lune per host in its own color, the near wall, the cut edge; no stacked host slabs
    const sc = S.buildScene(alpha, pa, { width: 1000, height: 700, tf: G.identity(), motion: "off" });
    assert.ok(finiteDeep(sc.items));
    const washes = sc.items.filter((it) => it.id.startsWith("bowl-host-") && it.fill);
    assert.equal(washes.length, hosts.length);
    assert.equal(new Set(washes.map((it) => it.bg.color)).size, hosts.length, "each host its own color");
    assert.ok(["bowl-in", "bowl-lip", "bowl-grid"].every((id) => sc.items.some((it) => it.id === id)));
    assert.ok(!sc.items.some((it) => it.id === "bowl-shell" || it.id === "bowl-limb"), "the cut leaves no outside wall to draw");
    assert.ok(!sc.items.some((it) => it.id.startsWith("band-slab-") || it.id.startsWith("band-rim-")));
    // the outline: every lune stays inside the sphere's silhouette, and the whole bowl is a small part of the budget
    for (const b of pa.bands) {
      let p0;
      for (const c of SP.luneCmds(fr, 0, 0, 1, b.a0, b.a1)) {
        if (c[0] === "M") p0 = { x: c[1], y: c[2] };
        if (c[0] !== "C") continue;
        const curve = { p0, c1: { x: c[1], y: c[2] }, c2: { x: c[3], y: c[4] }, p3: { x: c[5], y: c[6] } };
        for (let j = 0; j <= 16; j++) { const q = G.pointAt(curve, j / 16); assert.ok(SP.rimNorm(fr, q) <= 1.002, "inside the rim"); }
        p0 = curve.p3;
      }
    }
    // the bowl (48 faint glow layers, the most of it) stays under half of one globe's 970 or so
    assert.ok(cmdCount(sc.items.filter((it) => it.t === "path" && it.id.startsWith("bowl-"))) <= 480, "the bowl costs a fraction of one globe");
  }
  // a lune's outline is exact: the rim, where each meridian meets the cut, projects to the rim ellipse
  const fr = SP.bowlFrame(0.4);
  for (const phi of [0, 1, 2.5, 4]) {
    const u = SP.bowlPoint(fr, SP.rimTheta(fr, phi), phi);
    assert.ok(Math.abs(SP.bowlSeen(fr, u) - 1) < 1e-9 && Math.abs(u.x * fr.cut.n.x + u.y * fr.cut.n.y + u.z * fr.cut.n.z - fr.cut.h) < 1e-12);
    const e = SP.rimCmds(fr, 0, 0, 1);
    assert.ok(Math.abs(SP.rimNorm(fr, { x: e[0][1], y: e[0][2] }) - 1) < 1e-9, "the drawn rim is the rim");
  }
  assert.ok(SP.bowlReach(fr, Math.PI / 2) < SP.bowlReach(fr, -Math.PI / 2), "less is reached on the near side, where the cut is lower");
  assert.ok(D.bowlTheta(fr, 0, 0, 5) !== D.bowlTheta(fr, 0, 1, 5) && D.bowlTheta(fr, 0, 0, 2) === D.bowlTheta(fr, 0, 1, 2), "two rows from four roles");
});

test("2026-11-10: depth edges get a dark casing and a little more core; flat edges are as they were", () => {
  const cold = { tier: "cold", core: 0.9, coreAlpha: 0.32 }, fade = { tier: "fade", core: 0.8, coreAlpha: 0.1 };
  assert.ok(S.edgeCasing(cold).coreAlpha > cold.coreAlpha && S.edgeCasing(cold).width > cold.core && S.edgeCasing(cold).alpha > 0);
  assert.equal(S.edgeCasing(fade).coreAlpha, fade.coreAlpha, "a faded edge stays faded");
  assert.ok(S.edgeCasing(fade).alpha < S.edgeCasing(cold).alpha);
  const flat = S.buildScene(global, L.layoutGraph(global), { width: 1000, height: 700, tf: G.identity(), motion: "off" });
  assert.ok(!flat.items.some((it) => it.id.startsWith("ek-") || it.id.startsWith("eak-")));
  const deep = S.buildScene(global, D.projectLayout(global, L.layoutGraph(global), D.camera("tilt")), { width: 1000, height: 700, tf: G.identity(), motion: "off" });
  const cores = deep.items.filter((it) => it.id.startsWith("ec-")), casings = deep.items.filter((it) => it.id.startsWith("ek-"));
  assert.equal(casings.length, cores.length, "one casing per core");
  assert.ok(deep.items.findIndex((it) => it.id.startsWith("ek-")) < deep.items.findIndex((it) => it.id.startsWith("ec-")), "under the core");
});

// conjugate coordinates of d in the ellipse with semi-axes a, b: |w| > 1 is outside it
const ellipseNorm = (a, b, d) => {
  const det = a.x * b.y - a.y * b.x;
  return Math.hypot((d.x * b.y - d.y * b.x) / det, (a.x * d.y - a.y * d.x) / det);
};
const globeCams = [D.camera("iso"), D.camera("tilt"), D.camera("iso", Math.PI / 3), D.camera("tilt", -Math.PI / 4)];

test("2026-11-11: one light: it is the globes' screen light, above the floor, and every shadow falls from it", () => {
  for (const cam of globeCams) {
    const Lw = D.lightWorld(cam), flat = { ...cam, o: { x: 0, y: 0 }, d: 0 };
    assert.ok(Math.abs(Math.hypot(Lw.x, Lw.y, Lw.z) - 1) < 1e-9 && Lw.z > 0.3, "a unit direction, from above");
    // seen back through the camera, the world light is the light the globes are shaded from
    const q = D.project(flat, { x: Lw.x, y: Lw.y }, Lw.z);
    assert.ok(Math.abs(q.x - SP.LIGHT.x) < 1e-9 && Math.abs(q.y - SP.LIGHT.y) < 1e-9, "the same light as the shading");
    // orthographic: a ball's shadow is offset straight along the screen light, by its height, and no larger for it
    const P = (p, z) => D.project(flat, p, z);
    const lo = D.castShadow(P, Lw, { x: 40, y: -30 }, 100, 50, -200), hi = D.castShadow(P, Lw, { x: -300, y: 120 }, 500, 50, -200);
    for (const [s, p, z] of [[lo, { x: 40, y: -30 }, 100], [hi, { x: -300, y: 120 }, 500]]) {
      const c = P(p, z), dx = s.x - c.x, dy = s.y - c.y, len = Math.hypot(dx, dy);
      assert.ok(Math.abs(dx / len + SP.LIGHT.x / Math.hypot(SP.LIGHT.x, SP.LIGHT.y)) < 1e-9 && Math.abs(dy / len + SP.LIGHT.y / Math.hypot(SP.LIGHT.x, SP.LIGHT.y)) < 1e-9, "away from the light");
      assert.ok(Math.abs(len / (z + 200) - Math.hypot(SP.LIGHT.x, SP.LIGHT.y) / Lw.z) < 1e-9, "as far as its height says");
    }
    assert.ok(Math.abs(Math.hypot(lo.a.x, lo.a.y) - Math.hypot(hi.a.x, hi.a.y)) < 1e-9, "a directional light: the same size at any height");
    assert.ok(hi.soft > lo.soft && hi.peak < lo.peak, "higher: a softer, paler shadow");
  }
});

test("2026-11-11: each globe floats above the floor and its shadow lies there, beside it, the same way as the others'", () => {
  const base = L.layoutGraph(global);
  for (const cam of globeCams) {
    const pl = D.projectLayout(global, base, cam);
    const top = pl.planes.at(-1).z, P = (p, zz) => D.project(pl.cam, p, zz - top / 2);
    const planeZ = new Map(pl.planes.map((p) => [p.id, p.z]));
    const dirs = [];
    for (const c of base.clusters) {
      const R = c.r * 0.92, zc = planeZ.get(c.id);
      assert.ok(pl.floorZ < zc - R, `${c.id}: the floor is below the globe`);
      const st = pl.stems.find((s) => s.id === `c-${c.id}`), sh = st.shadow;
      // the shadow's middle is where the line from the light through the globe's middle meets the floor
      const t = (zc - pl.floorZ) / pl.light.z, g = P({ x: c.x - t * pl.light.x, y: c.y - t * pl.light.y }, pl.floorZ);
      assert.ok(Math.hypot(g.x - sh.x, g.y - sh.y) < 1e-6, "cast from the one light");
      // it does not swallow its globe: the globe's middle is outside the shadow, and from the presets outside even its
      // soft edge (turned, a near globe's floor grows in perspective, so the penumbra may reach under it)
      const g0 = pl.clusters.find((x) => x.id === c.id), rho = ellipseNorm(sh.a, sh.b, { x: g0.x - sh.x, y: g0.y - sh.y });
      assert.ok(rho > 1, `${c.id}: its middle is outside its shadow`);
      if (cam.yaw === D.CAMERAS[cam.name].yaw) assert.ok(rho > 1 + sh.soft, `${c.id}: and clear of its soft edge`);
      dirs.push(Math.atan2(sh.y - g0.y, sh.x - g0.x));
    }
    // the rays are parallel in the world (above); in perspective they converge a little on screen, as rails do, but
    // every shadow falls the same way
    const spread = Math.max(...dirs) - Math.min(...dirs);
    assert.ok(spread < 0.45, `shadows fall the same way: ${spread.toFixed(3)} rad apart`);
    for (const d of dirs) assert.ok(Math.cos(d - Math.atan2(-SP.LIGHT.y, -SP.LIGHT.x)) > 0.85, "down and away from the lit side");
  }
});

test("2026-11-11: a shadow's falloff is smooth: many fine steps, darkest in the middle, within the render budget", () => {
  const layers = SP.shadowLayers(0.6, 0.3);
  assert.ok(layers.length >= 14 && layers.length <= SP.SHADOW_STEPS, "many layers");
  assert.ok(layers[0].s <= 1.3 + 1e-9 && layers[0].s > 1.25 && layers.every((l, i) => i === 0 || l.s < layers[i - 1].s), "nested, from the soft edge in");
  let prev = 0;
  for (const l of layers) {
    const total = 1 - (1 - prev) * (1 - l.alpha);
    assert.ok(total - prev <= 0.075, "no step adds more than 7.5 percent: no coarse bands");
    prev = total;
  }
  assert.ok(Math.abs(prev - SP.shadowAt(0, 0.6, 0.3)) < 0.03, "the middle adds up to the shadow's darkest");
  assert.ok(SP.shadowAt(1.3, 0.6, 0.3) === 0 && SP.shadowAt(0.5, 0.6, 0.3) > SP.shadowAt(1, 0.6, 0.3), "it fades out by the soft edge");
  const pl = D.projectLayout(global, L.layoutGraph(global), D.camera("iso"));
  const sc = S.buildScene(global, pl, { width: 1000, height: 700, tf: G.identity(), motion: "off" });
  for (const c of pl.clusters) {
    const sh = sc.items.filter((it) => it.id.startsWith(`shadow-c-${c.id}-`));
    assert.ok(sh.length >= 14 && cmdCount(sh) <= SP.SHADOW_STEPS * 6, "a globe's shadow: fine steps, a few cubics each");
    assert.ok(sc.items.indexOf(sh.at(-1)) < sc.items.findIndex((it) => it.id === `sphere-body-${c.id}`), "on the floor, under the globe");
  }
  assert.ok(cmdCount(sc.items.filter((it) => it.id.startsWith("shadow-"))) <= 300, "every shadow together under a sixth of what the globes take");
});

test("2026-11-11: a globe's name is larger and clear above its globe and the roles on its top", () => {
  for (const cam of globeCams) {
    const lay = D.projectLayout(global, L.layoutGraph(global), cam);
    const tf = S.fitScene(global, lay, { width: 1100, height: 800 });
    const sc = S.buildScene(global, lay, { width: 1100, height: 800, tf, motion: "off" });
    for (const c of lay.clusters) {
      const p = G.toScreen(tf, c), r = c.r * tf.k;
      const name = sc.items.find((it) => it.id === `disc-label-${c.id}`), needs = sc.items.find((it) => it.id === `disc-needs-${c.id}`);
      assert.ok(name.size >= 13, "larger than a flat disc's 11 px");
      const bottom = (needs ?? name).y + (needs ?? name).h;
      assert.ok(bottom <= p.y - r - S.GLOBE_TITLE.gap + 1e-6, `${c.id}: clear above the globe`);
      const w = name.text.length * name.size * 0.6, box = { x: p.x - w / 2, y: name.y, w, h: bottom - name.y };
      for (const n of global.nodes.filter((x) => x.kind === "role" && x.shoal === c.id)) {
        const s = sc.nodeScreen.get(n.id);
        const nx = Math.max(box.x, Math.min(s.x, box.x + box.w)), ny = Math.max(box.y, Math.min(s.y, box.y + box.h));
        assert.ok(Math.hypot(s.x - nx, s.y - ny) > s.r, `${n.label}: not under ${c.id}'s name`);
      }
    }
  }
  // the flat view keeps its small titles
  const flat = S.buildScene(global, L.layoutGraph(global), { width: 1000, height: 700, tf: G.identity(), motion: "off" });
  assert.ok(flat.items.filter((it) => it.id.startsWith("disc-label-")).every((it) => it.size === 11));
});

test("2026-11-11: idle globes are fainter than in #153; a hovered one steps up as before", () => {
  assert.ok(SP.GLASS.idle.body < 0.07 && SP.GLASS.idle.lit < 0.34, "fainter idle than the 7 and 34 percent of #153");
  assert.deepEqual(SP.GLASS.focus, { body: 0.16, lit: 0.56 }, "the hovered step is kept");
  const total = (focused) => { const s = SP.sphereShade("t", 0, 0, 1, "#50d090", 1, focused); return opacityOf([s.body, ...s.paths.map((p) => p.bg.color)]); };
  assert.ok(total(true) - total(false) >= 0.25, "hovering clearly lifts it");
});

test("2026-11-11: the bowl from above: no near wall in the way, you at the middle drawn larger, nearest", () => {
  const la = L.layoutGraph(alpha);
  for (const cam of [D.camera("tilt"), D.camera("iso"), D.camera("iso", Math.PI / 3)]) {
    const pa = D.projectLayout(alpha, la, cam), fr = pa.bowl.frame;
    // the cut leaves nothing facing the camera (a point of the unit sphere does when u.z > 1 / d), so all of the
    // inside is seen through the opening, and nothing of the bowl shows past the rim
    let seen = 0, all = 0;
    for (let i = 1; i <= 24; i++) for (let j = 0; j < 48; j++) {
      const phi = (2 * Math.PI * j) / 48, u = SP.bowlPoint(fr, SP.rimTheta(fr, phi) * (i / 24), phi);
      all++;
      assert.ok(u.z < 1 / fr.d - 0.02 && SP.bowlKept(fr, u), "no kept point faces the camera");
      if (SP.bowlSeen(fr, u) <= 1 + 1e-9) seen++;
    }
    assert.equal(seen, all, "the whole inner surface is seen");
    const user = alpha.nodes.find((n) => n.kind === "user"), roles = alpha.nodes.filter((n) => n.kind === "role");
    assert.ok(pa.scaleOf.get(user.id) >= 1.2, "you read larger than in #153 (1)");
    for (const n of roles) assert.ok(pa.scaleOf.get(user.id) >= 1.12 * pa.scaleOf.get(n.id), `you, nearer, larger than ${n.label}`);
    assert.equal(pa.order.findIndex((n) => n.id === user.id) > Math.max(...roles.map((n) => pa.order.indexOf(n))), true, "drawn after every role");
    // seen from above and from the side: the bottom lands below and beside you, the rim is a turned ellipse, the far
    // wall keeps a hemisphere's rim and only the near wall is cut lower
    const pole = SP.bowlProject(fr, fr.pole);
    assert.ok(Math.abs(pole.x) > 0.2 && pole.y > 0.2, `the pole off the middle both ways: ${pole.x.toFixed(2)}, ${pole.y.toFixed(2)}`);
    assert.ok(Math.abs(fr.rim.m[1]) > 0.05 * Math.max(fr.rim.m[0], fr.rim.m[2]), "the rim's ellipse is turned, not square to the screen");
    const rims = Array.from({ length: 72 }, (_, j) => SP.rimTheta(fr, (2 * Math.PI * j) / 72));
    assert.ok(Math.max(...rims) <= Math.PI / 2 + 1e-9 && Math.max(...rims) > Math.PI / 2 - 0.01 && Math.min(...rims) < Math.PI / 2 - 0.2, "far rim at the equator, near rim lower");
    assert.ok(Math.min(...rims) > Math.PI / 3 + 0.05, "both circles of latitude are whole");
    // your shadow from the one light falls on the inside, inside the rim and clear of you
    const sc = S.buildScene(alpha, pa, { width: 1000, height: 700, tf: G.identity(), motion: "off" });
    const shadow = sc.items.filter((it) => it.id.startsWith("bowl-shadow-"));
    assert.equal(shadow.length, SP.BOWL_SHADOW_STEPS);
    const sh = SP.bowlShadow(fr, 0, 0, 1, 0.1).at(-1), you = pa.pos.get(user.id);
    assert.ok(SP.rimNorm(fr, sh) < 0.9 && Math.hypot(sh.x - you.x / pa.bowl.R, sh.y - you.y / pa.bowl.R) > 0.3, "a shadow on the wall, apart from you");
    // the washes are light, so edges and arrows over them read; the inside is clearer than in #155
    assert.ok(SP.BOWL.wash < 0.1 && SP.BOWL.base < 0.16);
  }
});

test("2026-11-11: the bowl's glow is smooth: many faint layers, no step between two of them shows", () => {
  for (const alphaT of [0.4, 0.6]) {
    const fr = SP.bowlFrame(alphaT), layers = SP.bowlGlow(fr, 0, 0, 1);
    assert.ok(layers.length >= 48 && SP.BOWL.glow * 255 <= 2.01, "48 or more layers at 2 of 255 each, not 20 at 5");
    const total = 1 - (1 - Math.round(SP.BOWL.glow * 255) / 255) ** layers.length;
    assert.ok(total > 0.25 && total < 0.34, `about the light it had: ${total.toFixed(3)}`);
    // each layer inside the one before it, and all inside the rim
    const pts = (e) => Array.from({ length: 24 }, (_, i) => { const t = (2 * Math.PI * i) / 24; return { x: e.x + e.a.x * Math.cos(t) + e.b.x * Math.sin(t), y: e.y + e.a.y * Math.cos(t) + e.b.y * Math.sin(t) }; });
    for (const p of pts(layers[0])) assert.ok(Math.abs(SP.rimNorm(fr, p) - 1) < 1e-9, "the outermost layer is the rim");
    for (let i = 1; i < layers.length; i++) {
      const prev = layers[i - 1], m = fr.rim.m;
      for (const p of pts(layers[i])) {
        const x = p.x - prev.x, y = p.y - prev.y;
        assert.ok(m[0] * x * x + 2 * m[1] * x * y + m[2] * y * y <= prev.s * prev.s + 1e-9, "nested");
      }
    }
    // across the inside, the color steps by at most two of 255 between neighboring points
    const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    let prev = null, worst = 0, lit = 0;
    for (let i = -100; i <= 100; i++) {
      const p = { x: fr.rim.cx + (i / 100) * fr.rim.w * 0.99, y: fr.rim.cy + (i / 100) * fr.rim.h * 0.3 };
      if (SP.rimNorm(fr, p) > 1) { prev = null; continue; }
      const c = rgb(SP.bowlColorAt(fr, p.x, p.y, null));
      if (prev) worst = Math.max(worst, ...c.map((v, k) => Math.abs(v - prev[k])));
      lit = Math.max(lit, c[0]);
      prev = c;
    }
    assert.ok(worst <= 2 && lit > 60, `smooth (worst step ${worst}) and still lit`);
  }
  const sc = S.buildScene(alpha, D.projectLayout(alpha, L.layoutGraph(alpha), D.camera("iso")), { width: 1000, height: 700, tf: G.identity(), motion: "off" });
  const glow = sc.items.filter((it) => it.id.startsWith("bowl-glow-"));
  assert.equal(glow.length, SP.BOWL_GLOW);
  assert.ok(glow.every((it) => it.cmds.length === 6 && it.bg.color.endsWith("02")), "a conjugate ellipse each, 2 of 255");
});

test("2026-11-11: the hosts' lunes tile the whole bowl, with no gaps", () => {
  const TAU = 2 * Math.PI;
  for (const H of [1, 2, 3, 5]) {
    // the flat view's sectors: spans by weight, with a gap between each
    const gap = H > 1 ? 0.26 : 0, w = Array.from({ length: H }, (_, i) => 1 + i), tw = w.reduce((t, x) => t + x, 0);
    let a0 = -Math.PI / 2 + gap / 2;
    const bands = w.map((x, i) => { const span = ((TAU - gap * H) * x) / tw, b = { host: `host-${i}`, a0, a1: a0 + span }; a0 += span + gap; return b; });
    for (const yaw of [0, 1.1]) {
      const lunes = D.bowlLunes(bands, yaw);
      assert.deepEqual(lunes.map((b) => b.host), bands.map((b) => b.host), "in the bands' order");
      assert.ok(Math.abs(lunes.reduce((t, b) => t + b.a1 - b.a0, 0) - TAU) < 1e-9, `${H} hosts: the spans add up to a whole turn`);
      const sorted = [...lunes].sort((a, b) => a.a0 - b.a0);
      for (let i = 1; i < H; i++) assert.ok(Math.abs(sorted[i].a0 - sorted[i - 1].a1) < 1e-9, "each lune meets the next");
      for (const b of lunes) assert.ok(b.a0 <= b.s0 + 1e-9 && b.s1 <= b.a1 + 1e-9, "a lune holds its host's sector");
    }
  }
  // the fixture: every point seen inside the rim is one host's or the other's
  for (const cam of [D.camera("tilt"), D.camera("iso", -Math.PI / 5)]) {
    const pa = D.projectLayout(alpha, L.layoutGraph(alpha), cam), fr = pa.bowl.frame;
    let inside = 0;
    const hosts = new Set();
    for (let i = -40; i <= 40; i++) for (let j = -40; j <= 40; j++) {
      const s = { x: fr.rim.cx + (i / 40) * fr.rim.w, y: fr.rim.cy + (j / 40) * fr.rim.h };
      if (SP.rimNorm(fr, s) > 0.999) continue;
      inside++;
      const b = S.bowlHostAt(fr, pa.bands, s.x, s.y);
      assert.ok(b, `a host at ${s.x.toFixed(2)}, ${s.y.toFixed(2)}`);
      hosts.add(b.host);
    }
    assert.ok(inside > 4000 && hosts.size === pa.bands.length, "the hosts share all of it");
  }
});
