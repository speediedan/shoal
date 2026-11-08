// depth.js: the lattice's depth mode (P3b), pure (no Herdr imports, so it tests under Node). GPUI has no 3D, but every
// position is the mod's own: each node gets a plane (its shoal in the global view, its host per shoal; the operator and
// outside seats on a plane above), and the layout is projected before the scene draws it. A camera turns the plane
// (yaw) and tilts it back (pitch), so a circle on a plane is an ellipse whose height is cos(pitch) of its width.

const DEG = Math.PI / 180;

/** The camera presets `d` cycles through; flat is the 2D view, untouched. */
export const CAMERAS = {
  flat: null,
  tilt: { pitch: 38 * DEG, yaw: 0 },
  iso: { pitch: 54.7 * DEG, yaw: 45 * DEG },
};
export const DEPTH_CYCLE = ["flat", "tilt", "iso"];
export const PLANE_GAP = 110; // world units between planes, before the tilt
export const YAW_STEP = 15 * DEG;
const NEAR = 1.15, FAR = 0.8; // a node's size from the far plane to the near one

/** The camera for a preset and a yaw turned on top of it (`,` and `.`), or null for flat. */
export function camera(name, yawTurn = 0) {
  const c = CAMERAS[name];
  if (!c) return null;
  const yaw = c.yaw + yawTurn;
  return { name, pitch: c.pitch, yaw, c: Math.cos(c.pitch), s: Math.sin(c.pitch), cyaw: Math.cos(yaw), syaw: Math.sin(yaw) };
}

/** A plane point (x, y) at height z, seen through `cam`: its place in the layout's 2D space, and its depth (larger is nearer). */
export function project(cam, p, z = 0) {
  const xr = p.x * cam.cyaw - p.y * cam.syaw;
  const yr = p.x * cam.syaw + p.y * cam.cyaw;
  return { x: xr, y: yr * cam.c - z * cam.s, depth: yr * cam.s + z * cam.c };
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
 * The layout as the camera sees it: every position projected, clusters and bands carrying their projected centers and
 * the vertical squash `ry` (cos(pitch)) and the yaw their angles turn by, drawn far to near; `scaleOf` (a node's size
 * from the far plane to the near) and `order` (vm.nodes far to near) for the scene. Flat (no camera): the layout as is.
 */
export function projectLayout(vm, lay, cam) {
  if (!cam || !lay) return lay;
  const { zOf, planes } = planesOf(vm, lay);
  const z = (id) => zOf.get(id) ?? 0;
  const pos = new Map();
  const depth = new Map();
  for (const [id, p] of lay.pos) {
    const q = project(cam, p, z(id));
    pos.set(id, { x: q.x, y: q.y });
    depth.set(id, q.depth);
  }
  const ds = [...depth.values()];
  const d0 = Math.min(...ds), d1 = Math.max(...ds);
  const scaleOf = new Map([...depth].map(([id, d]) => [id, d1 > d0 ? FAR + ((d - d0) / (d1 - d0)) * (NEAR - FAR) : 1]));
  const sats = (lay.sats ?? []).map((s) => {
    const a = project(cam, { x: s.x, y: s.y }, z(s.owner)), b = project(cam, { x: s.ax, y: s.ay }, z(s.owner));
    return { ...s, x: a.x, y: a.y, ax: b.x, ay: b.y };
  });
  const planeZ = new Map(planes.map((p) => [p.id, p.z]));
  const clusters = (lay.clusters ?? []).map((c) => {
    const zc = vm.global ? planeZ.get(c.id) ?? 0 : 0;
    const q = project(cam, c, zc);
    return { ...c, x: q.x, y: q.y, ry: cam.c, depth: q.depth };
  }).sort((a, b) => a.depth - b.depth);
  const bands = (lay.bands ?? []).map((b) => {
    const q = project(cam, { x: 0, y: 0 }, planeZ.get(b.host) ?? 0);
    return { ...b, cx: q.x, cy: q.y, ry: cam.c, a0: b.a0 + cam.yaw, a1: b.a1 + cam.yaw, depth: q.depth };
  }).sort((a, b) => a.depth - b.depth);
  const anchors = new Map([...(lay.anchors ?? new Map())].map(([h, a]) => {
    const q = project(cam, a, planeZ.get(h) ?? 0);
    return [h, { ...a, x: q.x, y: q.y }];
  }));
  const tethers = (lay.tethers ?? []).map((t) => ({
    ...t,
    from: pos.get(t.id) ?? t.from,
    to: t.to ? (() => { const q = project(cam, t.to, planeZ.get(t.host) ?? 0); return { x: q.x, y: q.y }; })() : null,
  }));
  const order = [...vm.nodes].sort((a, b) => (depth.get(a.id) ?? 0) - (depth.get(b.id) ?? 0));
  return { ...lay, pos, sats, clusters, bands, anchors, tethers, cam, scaleOf, order, planes };
}

/** The next preset in the cycle. */
export function nextDepth(name) {
  return DEPTH_CYCLE[(DEPTH_CYCLE.indexOf(name) + 1) % DEPTH_CYCLE.length] ?? "flat";
}

/** The camera that applies: a tilt is dropped to flat at the low and off tiers on a CPU renderer (the design's rule). */
export function depthFor(name, tier, renderer) {
  return renderer === "cpu" && (tier === "low" || tier === "off") ? "flat" : CAMERAS[name] === undefined ? "flat" : name;
}
