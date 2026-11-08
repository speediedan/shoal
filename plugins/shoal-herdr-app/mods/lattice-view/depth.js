// depth.js: the lattice's depth mode (P3b), pure (no Herdr imports, so it tests under Node). GPUI has no 3D, but every
// position is the mod's own: each node gets a plane (its shoal in the global view, its host per shoal; the operator and
// outside seats on a plane above), and the layout is projected before the scene draws it. A camera turns the planes
// (yaw), tilts them back (pitch) and sees them in perspective from a distance, so far planes shrink toward the horizon
// and near ones grow. The scene adds what makes it read as space: slab rims, a floor grid, stems from each plane and from
// the operator down to the floor with their shadows, and fog on what is far.

const DEG = Math.PI / 180;

// A role's place on its shoal's sphere, by rank: what it needs of you first (the needs-you order: auth, a question,
// a prompt, a design call, an empty queue; a stalled session), then what it is doing (a turn, its own job, a hand-over,
// waiting on others, idle with work queued, idle), then how fresh its last ledger write is. Rank 0 is the point that faces
// you; the rest follow a spiral back from it, so the sphere is evenly used and what needs you is nearest.
const ACTIVITY = { working: 0, "local-ci": 1, "remote-ci": 1, handover: 2, refill: 2, lease: 3, upstream: 3, peer: 3, hold: 3, other: 3,
  queued: 4, starved: 5, held: 5, dismissed: 5, excluded: 6 };
const SPHERE_R = 0.92; // of the shoal's disc radius
const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const CAP = 0.95; // how far back the spiral reaches: 1 is the far pole

/** [need, activity, staleness] for sorting: lower is nearer you. */
export function sphereRank(n) {
  const v = n?.visual ?? {};
  const need = v.needsYou || v.stall ? (Number.isFinite(v.urgency) ? v.urgency : 4) : 9;
  const act = ACTIVITY[v.key] ?? 3;
  const age = Number(n?.metrics?.last_write_age_s);
  return [need, act, Number.isFinite(age) ? age : 1e12];
}

const byRank = (a, b) => {
  const x = sphereRank(a), y = sphereRank(b);
  return x[0] - y[0] || x[1] - y[1] || x[2] - y[2] || (a.id < b.id ? -1 : 1);
};

/**
 * The i-th of n points on a unit sphere, as a world offset {x, y, z} for `cam`: i = 0 faces the camera, and the rest
 * spiral back from it (a Fibonacci spiral over the front CAP of the sphere).
 */
export function spherePoint(cam, i, n) {
  const cos = 1 - (2 * CAP * i) / Math.max(1, n);
  const sin = Math.sqrt(Math.max(0, 1 - cos * cos));
  const phi = i * GOLDEN;
  // in the camera's turned frame: toward it is (0, s, c); across is (1, 0, 0); up the screen is (0, c, -s)
  const xr = sin * Math.cos(phi);
  const yr = cos * cam.s + sin * Math.sin(phi) * cam.c;
  const z = cos * cam.c - sin * Math.sin(phi) * cam.s;
  return { x: xr * cam.cyaw + yr * cam.syaw, y: -xr * cam.syaw + yr * cam.cyaw, z };
}

/** The camera presets `d` cycles through; flat is the 2D view, untouched. */
export const CAMERAS = {
  flat: null,
  tilt: { pitch: 52 * DEG, yaw: 0 },
  iso: { pitch: 58 * DEG, yaw: 45 * DEG },
};
export const DEPTH_CYCLE = ["flat", "tilt", "iso"];
export const PLANE_GAP = 190; // world units between planes, before the tilt
export const YAW_STEP = 15 * DEG;
const DIST = 2.6; // the camera's distance, in layout radii: smaller is stronger perspective
const SCALE_MIN = 0.55, SCALE_MAX = 1.6; // what perspective may do to a size
const FLOOR_BELOW = 0.55; // the floor, below the lowest plane, in plane gaps
const GRID = 10; // floor grid lines each way

/** The camera for a preset and a yaw turned on top of it (`,` and `.`), or null for flat. */
export function camera(name, yawTurn = 0) {
  const c = CAMERAS[name];
  if (!c) return null;
  const yaw = c.yaw + yawTurn;
  return { name, pitch: c.pitch, yaw, c: Math.cos(c.pitch), s: Math.sin(c.pitch), cyaw: Math.cos(yaw), syaw: Math.sin(yaw), d: 0, o: { x: 0, y: 0 } };
}

/**
 * A plane point (x, y) at height z, seen through `cam`: its place in the layout's 2D space, its depth (larger is nearer)
 * and `f`, the perspective scale there. With `cam.d` 0 (no distance set) the view is orthographic, `f` 1.
 */
export function project(cam, p, z = 0) {
  const x = p.x - cam.o.x, y = p.y - cam.o.y;
  const xr = x * cam.cyaw - y * cam.syaw;
  const yr = x * cam.syaw + y * cam.cyaw;
  const sy = yr * cam.c - z * cam.s;
  const depth = yr * cam.s + z * cam.c;
  const f = cam.d > 0 ? Math.min(SCALE_MAX, Math.max(SCALE_MIN, cam.d / Math.max(cam.d * 0.2, cam.d - depth))) : 1;
  return { x: cam.o.x + xr * f, y: cam.o.y + sy * f, depth, f };
}

/**
 * Each node's plane height and the planes themselves, far to near: {zOf: Map id -> z, planes: [{id, z, kind}]}. Global:
 * one plane per shoal, in the layout's cluster order; per shoal: one per host band. The operator and outside seats go on
 * a plane of their own above them all.
 */
export function planesOf(vm, lay) {
  const zOf = new Map();
  const planes = [];
  if (vm.global) {
    (lay.clusters ?? []).forEach((c, i) => planes.push({ id: c.id, z: i * PLANE_GAP, kind: "shoal" }));
    const zs = new Map(planes.map((p) => [p.id, p.z]));
    for (const n of vm.nodes) if (zs.has(n.shoal) && n.kind === "role") zOf.set(n.id, zs.get(n.shoal));
  } else {
    (lay.bands ?? []).forEach((b, i) => planes.push({ id: b.host, z: i * PLANE_GAP, kind: "host" }));
    const zs = new Map(planes.map((p) => [p.id, p.z]));
    for (const n of vm.nodes) if (n.kind === "role") zOf.set(n.id, zs.get(n.host ?? "?") ?? 0);
  }
  const top = planes.length * PLANE_GAP;
  for (const n of vm.nodes) if (!zOf.has(n.id)) zOf.set(n.id, top);
  planes.push({ id: "operator", z: top, kind: "operator" });
  return { zOf, planes };
}

/**
 * The layout as the camera sees it: every position projected in perspective about the layout's middle; clusters and
 * bands with their projected centers, their perspective scale `s` and vertical squash `ry`, drawn far to near; and for
 * the scene `scaleOf` (a node's size), `fogOf` (how much of it shows: far fades), `order` (vm.nodes far to near),
 * `grid` (floor lines), `stems` (plane centers and lifted nodes down to the floor, with a shadow). Flat: the layout as is.
 */
export function projectLayout(vm, lay, cam0) {
  if (!cam0 || !lay) return lay;
  const { zOf, planes } = planesOf(vm, lay);
  const z = (id) => zOf.get(id) ?? 0;
  const pts = [...lay.pos.values()];
  const o = pts.length ? { x: pts.reduce((t, p) => t + p.x, 0) / pts.length, y: pts.reduce((t, p) => t + p.y, 0) / pts.length } : { x: 0, y: 0 };
  const radius = Math.max(200, ...pts.map((p) => Math.hypot(p.x - o.x, p.y - o.y)), ...(lay.clusters ?? []).map((c) => Math.hypot(c.x - o.x, c.y - o.y) + c.r));
  const top = planes.at(-1).z;
  // the eye looks at the middle of the stack, from DIST radii away
  const cam = { ...cam0, o, d: DIST * Math.max(radius, top) };
  const mid = top / 2;
  const P = (p, zz) => project(cam, p, zz - mid);
  // global view: each shoal's roles on its sphere, by rank; a role keeps its satellites' offsets
  const placed = new Map(); // role id -> {x, y, z} in the world
  const behind = new Map(); // role id -> how far round the back of its sphere it is (0 front, 1 far pole)
  if (vm.global) {
    const planeZ0 = new Map(planes.map((p) => [p.id, p.z]));
    for (const c of lay.clusters ?? []) {
      const roles = vm.nodes.filter((n) => n.kind === "role" && n.shoal === c.id && lay.pos.has(n.id)).sort(byRank);
      const R = c.r * SPHERE_R;
      roles.forEach((n, i) => {
        const u = spherePoint(cam, i, roles.length);
        placed.set(n.id, { x: c.x + R * u.x, y: c.y + R * u.y, z: (planeZ0.get(c.id) ?? 0) + R * u.z });
        behind.set(n.id, (2 * CAP * i) / Math.max(1, roles.length) / 2);
      });
    }
  }
  const pos = new Map(), depth = new Map(), scaleOf = new Map();
  for (const [id, p] of lay.pos) {
    const w = placed.get(id);
    const q = w ? P(w, w.z) : P(p, z(id));
    pos.set(id, { x: q.x, y: q.y });
    depth.set(id, q.depth);
    // on a sphere, the front grows and the back shrinks, beyond what perspective alone does
    scaleOf.set(id, q.f * (behind.has(id) ? 1.28 - 0.62 * behind.get(id) : 1));
  }
  const ds = [...depth.values()];
  const d0 = Math.min(...ds), d1 = Math.max(...ds);
  // far fades; a role round the back of its sphere fades more, as seen through it
  const fogOf = new Map([...depth].map(([id, d]) => [id, (d1 > d0 ? 0.55 + 0.45 * ((d - d0) / (d1 - d0)) : 1)
    * (behind.has(id) ? 1 - 0.7 * Math.max(0, behind.get(id) - 0.35) / 0.65 : 1)]));
  const sats = (lay.sats ?? []).map((s) => {
    const w = placed.get(s.owner), p0 = lay.pos.get(s.owner);
    const at = (x, y) => (w && p0 ? P({ x: w.x + x - p0.x, y: w.y + y - p0.y }, w.z) : P({ x, y }, z(s.owner)));
    const a = at(s.x, s.y), b = at(s.ax, s.ay);
    return { ...s, x: a.x, y: a.y, ax: b.x, ay: b.y };
  });
  const planeZ = new Map(planes.map((p) => [p.id, p.z]));
  const clusters = (lay.clusters ?? []).map((c) => {
    const zc = vm.global ? planeZ.get(c.id) ?? 0 : 0;
    const q = P(c, zc);
    // global: a sphere (its outline a circle at any tilt); per shoal the cluster is only the hit area
    return { ...c, x: q.x, y: q.y, r: c.r * (vm.global ? SPHERE_R : 1) * q.f, ry: vm.global ? 1 : cam.c, sphere: vm.global, s: q.f, depth: q.depth, zw: zc };
  }).sort((a, b) => a.depth - b.depth);
  const regionFog = (xs) => {
    const lo = Math.min(...xs.map((x) => x.depth)), hi = Math.max(...xs.map((x) => x.depth));
    for (const x of xs) x.fog = hi > lo ? 0.5 + 0.5 * ((x.depth - lo) / (hi - lo)) : 1;
    return xs;
  };
  regionFog(clusters);
  const center = vm.global ? null : { x: 0, y: 0 };
  const bands = (lay.bands ?? []).map((b) => {
    const q = P(center ?? { x: 0, y: 0 }, planeZ.get(b.host) ?? 0);
    return { ...b, cx: q.x, cy: q.y, ry: cam.c, s: q.f, a0: b.a0 + cam.yaw, a1: b.a1 + cam.yaw, depth: q.depth };
  }).sort((a, b) => a.depth - b.depth);
  regionFog(bands);
  const anchors = new Map([...(lay.anchors ?? new Map())].map(([h, a]) => {
    const q = P(a, planeZ.get(h) ?? 0);
    return [h, { ...a, x: q.x, y: q.y }];
  }));
  const tethers = (lay.tethers ?? []).map((t) => ({
    ...t,
    from: pos.get(t.id) ?? t.from,
    to: t.to ? (() => { const q = P(t.to, planeZ.get(t.host) ?? 0); return { x: q.x, y: q.y }; })() : null,
  }));
  // the floor: a grid below the lowest plane, its lines converging with distance
  const floorZ = -FLOOR_BELOW * PLANE_GAP;
  const R = radius * 1.15;
  const grid = [];
  for (let i = 0; i <= GRID; i++) {
    const t = -R + (2 * R * i) / GRID;
    for (const [a, b] of [[{ x: o.x + t, y: o.y - R }, { x: o.x + t, y: o.y + R }], [{ x: o.x - R, y: o.y + t }, { x: o.x + R, y: o.y + t }]]) {
      const p = P(a, floorZ), q = P(b, floorZ);
      grid.push({ x0: p.x, y0: p.y, x1: q.x, y1: q.y, edge: i === 0 || i === GRID });
    }
  }
  // stems: each plane's middle (per shoal: the band ring's), and every lifted node, down to the floor, with a shadow there
  const stems = [];
  const stem = (id, p, zz, r) => {
    const a = P(p, zz), b = P(p, floorZ);
    stems.push({ id, x0: a.x, y0: a.y, x1: b.x, y1: b.y, r: r * b.f, ry: r * b.f * cam.c });
  };
  if (vm.global) for (const c of lay.clusters ?? []) stem(`c-${c.id}`, c, planeZ.get(c.id) ?? 0, c.r * SPHERE_R * 1.05);
  else if ((lay.bands ?? []).length) for (const b of lay.bands) stem(`b-${b.host}`, { x: 0, y: 0 }, planeZ.get(b.host) ?? 0, 26);
  for (const n of vm.nodes) if (n.kind !== "role" && lay.pos.has(n.id)) stem(`n-${n.id}`, lay.pos.get(n.id), z(n.id), n.kind === "user" ? 30 : 14);
  const order = [...vm.nodes].sort((a, b) => (depth.get(a.id) ?? 0) - (depth.get(b.id) ?? 0));
  return { ...lay, pos, sats, clusters, bands, anchors, tethers, cam, scaleOf, fogOf, order, planes, grid, stems };
}

/** The next preset in the cycle. */
export function nextDepth(name) {
  return DEPTH_CYCLE[(DEPTH_CYCLE.indexOf(name) + 1) % DEPTH_CYCLE.length] ?? "flat";
}

/** The camera that applies: a tilt is dropped to flat at the low and off tiers on a CPU renderer (the design's rule). */
export function depthFor(name, tier, renderer) {
  return renderer === "cpu" && (tier === "low" || tier === "off") ? "flat" : CAMERAS[name] === undefined ? "flat" : name;
}
