// depth.js: the lattice's depth mode (P3b), pure (no Herdr imports, so it tests under Node). GPUI has no 3D, but every
// position is the mod's own: in the global view each node gets a plane (its shoal's; the operator and outside seats on
// a plane above), and the layout is projected before the scene draws it. A camera turns the planes (yaw), tilts them
// back (pitch) and sees them in perspective from a distance, so far planes shrink toward the horizon and near ones grow.
// Per shoal, the view is a cutout sphere instead (projectBowl): you at its middle, each host a lune of its inner surface.
// The scene adds what makes it read as space: a floor grid below everything, stems down to it, a shadow on it for each
// globe, lifted seat and edge (all cast by the one light, a point above the scene that also shades the globes), and fog
// on what is far.

import { BOWL_DIST, bowlFrame, bowlPoint, bowlProject, bowlReach, bowlRing, bowlCast, bowlKept, bowlSeen, bowlFloor, bowlLightDir, bowlLightAt, bowlLift, sideOf } from "./sphere.js";

const DEG = Math.PI / 180;

// A role's place on its shoal's sphere, by rank: what it needs of you first (the needs-you order: auth, a question,
// a prompt, a design call, an empty queue; a stalled session), then what it is doing (a turn, its own job, a hand-over,
// waiting on others, idle with work queued, idle), then how fresh its last ledger write is. Rank 0 is the point that faces
// you; the rest follow a spiral back from it, so the sphere is evenly used and what needs you is nearest.
const ACTIVITY = { working: 0, "local-ci": 1, "remote-ci": 1, "research-proto": 1, handover: 2, refill: 2, lease: 3, upstream: 3, peer: 3, hold: 3, other: 3,
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
/** The lattice opens in depth, iso: the globes and the bowl (2026-11-13: the maintainer's default). One press of `d` is
 * flat, the next tilt, then back to iso. */
export const DEFAULT_DEPTH = "iso";
export const DEPTH_CYCLE = ["iso", "flat", "tilt"];
export const PLANE_GAP = 190; // world units between planes, before the tilt
export const YAW_STEP = 15 * DEG;
const DIST = 2.6; // the camera's distance, in layout radii: smaller is stronger perspective
const SCALE_MIN = 0.55, SCALE_MAX = 1.6; // what perspective may do to a size
const FLOOR_BELOW = 0.55; // the floor, below the lowest plane, in plane gaps
const FLOOR_CLEAR = 0.45; // and below the lowest globe's bottom, in plane gaps, so every globe floats above it
const GRID = 10; // floor grid lines each way

// The one light of the global view: a point in the world (x and y the layout's plane, z up, as planesOf stacks it), high
// over the scene, LIGHT_DIST times its size from its middle in the direction LIGHT_WORLD: up, to the left and a little
// toward the far side. It stays put as the camera orbits, so the globes' lit sides and every shadow turn with the world,
// as a lamp's would. At the presets it is up and to the left on screen (sphere.js LIGHT), so shadows fall down and to the
// right of what casts them, each where the ray from the light through it meets the floor.
const unit3 = (x, y, z) => { const l = Math.hypot(x, y, z); return { x: x / l, y: y / l, z: z / l }; };
export const LIGHT_WORLD = unit3(-0.37, -0.07, 0.926);
export const LIGHT_DIST = 7;
/** The light's place for a scene whose middle is o (on the plane), `mid` its middle height and `size` its radius. */
export const lightPoint = (o, size, mid) => ({ x: o.x + LIGHT_DIST * size * LIGHT_WORLD.x, y: o.y + LIGHT_DIST * size * LIGHT_WORLD.y, z: mid + LIGHT_DIST * size * LIGHT_WORLD.z });
/** A world direction as `cam` sees it: x right, y down the screen, z toward you (sphere.js's camera space). */
export function toCamera(cam, w) {
  const f = w.x * cam.syaw + w.y * cam.cyaw;
  return { x: w.x * cam.cyaw - w.y * cam.syaw, y: f * cam.c - w.z * cam.s, z: f * cam.s + w.z * cam.c };
}
/** The unit way to the light Lp from a world point q, as `cam` sees it: what a globe there is shaded from. */
export function lightFrom(cam, Lp, q) { const v = toCamera(cam, { x: Lp.x - q.x, y: Lp.y - q.y, z: Lp.z - q.z }); return unit3(v.x, v.y, v.z); }
/** Where the ray from the light Lp through a world point q meets the floor at floorZ (a plane point): q's shadow. */
export function floorCast(Lp, q, floorZ) {
  const s = (Lp.z - floorZ) / Math.max(1e-6, Lp.z - q.z);
  return { x: Lp.x + s * (q.x - Lp.x), y: Lp.y + s * (q.y - Lp.y), s };
}

/**
 * The shadow a ball of radius r, its middle at plane point p and height z, casts on the floor at floorZ, seen through P,
 * from the point light Lp. The ray from the light through the ball's middle meets the floor at the shadow's middle
 * (floorCast); the shadow is r across the ray and r / cos(incidence) along it (a ball's shadow on a plane, longer the
 * lower the light comes in), both grown by how much further the floor is from the light than the ball (s), its penumbra
 * widening and its core paling as the ball rises. {x, y: its middle on screen, a, b: conjugate semi-axes on screen,
 * peak, soft, h: the caster's height above the floor, at: its middle on the floor}.
 */
export function castShadow(P, Lp, p, z, r, floorZ) {
  const h = Math.max(0, z - floorZ), g = floorCast(Lp, { x: p.x, y: p.y, z }, floorZ);
  const ray = { x: g.x - Lp.x, y: g.y - Lp.y, z: floorZ - Lp.z }, rl = Math.hypot(ray.x, ray.y, ray.z), cosI = Math.max(0.2, Math.abs(ray.z) / rl);
  const hl = Math.hypot(ray.x, ray.y);
  const along = hl > 1e-9 ? { x: ray.x / hl, y: ray.y / hl } : { x: 1, y: 0 }, across = { x: -along.y, y: along.x };
  const axis = (d, len) => {
    const q0 = P({ x: g.x - d.x * len, y: g.y - d.y * len }, floorZ), q1 = P({ x: g.x + d.x * len, y: g.y + d.y * len }, floorZ);
    return { x: (q1.x - q0.x) / 2, y: (q1.y - q0.y) / 2 };
  };
  const c = P(g, floorZ), up = h / PLANE_GAP;
  return { x: c.x, y: c.y, a: axis(along, (r * g.s) / cosI), b: axis(across, r * g.s), peak: 0.8 / (1 + 0.18 * up), soft: Math.min(0.5, 0.2 + 0.06 * up), h, at: { x: g.x, y: g.y } };
}

// The arrow keys orbit the camera: left and right round the screen's vertical, up and down round its horizontal (up
// raises the camera, so it looks down from higher). `orbit` {x, y} is that turn, in radians, on top of the preset. In
// the global view x adds to the yaw (round the planes' up, as `,` and `.` do) and y to the pitch, kept in PITCH_RANGE.
// Per shoal, x turns the bowl's axis round the screen's vertical (BOWL_HOME_TURN plus x, kept in BOWL_BETA_RANGE) and y
// tips it further down the screen (BOWL_HOME_TILT plus y, kept in BOWL_ALPHA_RANGE); `,` and `.` still spin it round its
// own axis. The bowl opens turned far round, its opening to the left and its bottom to the right, a little below level:
// -14 degrees tilt and -70 turn, the view the maintainer orbited to from the old opening (10 and -40) and asked for as
// the opening one (2026-11-13). Each axis reaches past it (to -30 tilt, to 78 turn either way), and the two together
// are held so the axis is never more than BOWL_OFF_AXIS_MAX off the line of sight (cos tilt cos turn, the cosine of
// that angle, kept above its floor): further round, the bowl goes edge-on and its inside closes up.
export const ORBIT_STEP = 8 * DEG;
export const PITCH_RANGE = [24 * DEG, 80 * DEG];
export const BOWL_HOME_TILT = -14 * DEG;
export const BOWL_HOME_TURN = -70 * DEG;
export const BOWL_ALPHA_RANGE = [-30 * DEG, 64 * DEG];
export const BOWL_BETA_RANGE = [-78 * DEG, 78 * DEG];
export const BOWL_OFF_AXIS_MAX = 78 * DEG;
const clampTo = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/**
 * The camera for a preset, a yaw turned on top of it (`,` and `.`) and an orbit (the arrows: {x, y} in radians), or
 * null for flat. `spin` is the yaw less the orbit's x: what the bowl turns its hosts round its own axis by.
 */
export function camera(name, yawTurn = 0, orbit = null) {
  const c = CAMERAS[name];
  if (!c) return null;
  const ox = Number.isFinite(orbit?.x) ? orbit.x : 0, oy = Number.isFinite(orbit?.y) ? orbit.y : 0;
  const spin = c.yaw + yawTurn, yaw = spin + ox, pitch = clampTo(c.pitch + oy, ...PITCH_RANGE);
  return { name, pitch, yaw, spin, orbit: { x: ox, y: oy }, c: Math.cos(pitch), s: Math.sin(pitch), cyaw: Math.cos(yaw), syaw: Math.sin(yaw), d: 0, o: { x: 0, y: 0 } };
}

/** The bowl's tilt and turn {alpha, beta} for an orbit: each kept in its range, then the turn held to what the tilt
 * leaves of BOWL_OFF_AXIS_MAX (cos alpha cos beta >= cos BOWL_OFF_AXIS_MAX), so any orbit gives a bowl seen from in
 * front. */
export function bowlAngles(orbit) {
  const alpha = clampTo(BOWL_HOME_TILT + (Number.isFinite(orbit?.y) ? orbit.y : 0), ...BOWL_ALPHA_RANGE);
  let beta = clampTo(BOWL_HOME_TURN + (Number.isFinite(orbit?.x) ? orbit.x : 0), ...BOWL_BETA_RANGE);
  const most = Math.acos(Math.min(1, Math.cos(BOWL_OFF_AXIS_MAX) / Math.cos(alpha)));
  if (Math.abs(beta) > most) beta = Math.sign(beta) * most;
  return { alpha, beta };
}
/** Whether a tilt and turn keep the bowl's axis within BOWL_OFF_AXIS_MAX of the line of sight. */
export const bowlInView = (alpha, beta) => Math.cos(alpha) * Math.cos(beta) >= Math.cos(BOWL_OFF_AXIS_MAX) - 1e-12;

/**
 * The orbit after an arrow (dx, dy in radians) on preset `name`, kept to what the view shows: per shoal (`bowl`) the
 * bowl's turn and tilt stay in their ranges and its axis within BOWL_OFF_AXIS_MAX of the line of sight; globally the
 * pitch does, and the yaw goes round freely. A press that would pass a limit goes as far as the limit and the rest is
 * dropped, not stored, so the way back is never dead, from a corner too. Flat: no orbit.
 */
export function orbitBy(name, orbit, dx = 0, dy = 0, bowl = false) {
  const c = CAMERAS[name];
  if (!c) return { x: 0, y: 0 };
  if (bowl) {
    const a0 = bowlAngles(orbit), from = { x: a0.beta - BOWL_HOME_TURN, y: a0.alpha - BOWL_HOME_TILT };
    const at = (t) => ({
      x: clampTo(BOWL_HOME_TURN + from.x + t * dx, ...BOWL_BETA_RANGE) - BOWL_HOME_TURN,
      y: clampTo(BOWL_HOME_TILT + from.y + t * dy, ...BOWL_ALPHA_RANGE) - BOWL_HOME_TILT,
    });
    const ok = (o) => bowlInView(BOWL_HOME_TILT + o.y, BOWL_HOME_TURN + o.x);
    if (ok(at(1))) return at(1);
    // past the combined limit: as far along the press as stays in view (bisection: the start is in view)
    let lo = 0, hi = 1;
    for (let i = 0; i < 40; i++) { const mid = (lo + hi) / 2; if (ok(at(mid))) lo = mid; else hi = mid; }
    return at(lo);
  }
  const x = Math.atan2(Math.sin((Number.isFinite(orbit?.x) ? orbit.x : 0) + dx), Math.cos((Number.isFinite(orbit?.x) ? orbit.x : 0) + dx));
  const y = clampTo(c.pitch + (Number.isFinite(orbit?.y) ? orbit.y : 0) + dy, ...PITCH_RANGE) - c.pitch;
  return { x, y };
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

/** The inverse of project at a known depth: the plane point and height (as project took them) that `cam` sees at the
 * layout point s, `depth` along its line of sight. */
export function unproject(cam, s, depth) {
  const f = cam.d > 0 ? Math.min(SCALE_MAX, Math.max(SCALE_MIN, cam.d / Math.max(cam.d * 0.2, cam.d - depth))) : 1;
  const xr = (s.x - cam.o.x) / f, sy = (s.y - cam.o.y) / f;
  const yr = sy * cam.c + depth * cam.s, z = depth * cam.c - sy * cam.s;
  return { x: cam.o.x + xr * cam.cyaw + yr * cam.syaw, y: cam.o.y - xr * cam.syaw + yr * cam.cyaw, z };
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
 * Per shoal: the cutout sphere, with the same fields plus `bowl` {frame, R} and `surfaceOf` giving each role's host and
 * place on it.
 */
export function projectLayout(vm, lay, cam0) {
  if (!cam0 || !lay) return lay;
  if (!vm.global) return projectBowl(vm, lay, cam0);
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
  const Lp = lightPoint(o, Math.max(radius, top), mid);
  // global view: each shoal's roles on its sphere, by rank; a role keeps its satellites' offsets
  const placed = new Map(); // role id -> {x, y, z} in the world
  const behind = new Map(); // recomputed from the turned camera, not from the role's rank
  const surfaceOf = new Map();
  if (vm.global) {
    const planeZ0 = new Map(planes.map((p) => [p.id, p.z]));
    for (const c of lay.clusters ?? []) {
      const roles = vm.nodes.filter((n) => n.kind === "role" && n.shoal === c.id && lay.pos.has(n.id)).sort(byRank);
      const R = c.r * SPHERE_R;
      const center = P(c, planeZ0.get(c.id) ?? 0);
      roles.forEach((n, i) => {
        // Anchor coordinates to the preset, so turning the camera actually reveals the globe's other side.
        const u = spherePoint(camera(cam.name), i, roles.length);
        placed.set(n.id, { x: c.x + R * u.x, y: c.y + R * u.y, z: (planeZ0.get(c.id) ?? 0) + R * u.z,
          screen: { x: center.x + R * center.f * (u.x * cam.cyaw - u.y * cam.syaw),
            y: center.y + R * center.f * ((u.x * cam.syaw + u.y * cam.cyaw) * cam.c - u.z * cam.s) } });
        const facing = (u.x * cam.syaw + u.y * cam.cyaw) * cam.s + u.z * cam.c;
        behind.set(n.id, (1 - facing) / 2);
        surfaceOf.set(n.id, { shoal: c.id, facing });
      });
    }
  }
  const pos = new Map(), depth = new Map(), scaleOf = new Map();
  for (const [id, p] of lay.pos) {
    const w = placed.get(id);
    const q = w ? P(w, w.z) : P(p, z(id));
    pos.set(id, w?.screen ?? { x: q.x, y: q.y });
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
    const at = (x, y) => {
      if (!w || !p0) return P({ x, y }, z(s.owner));
      const anchor = P(w, w.z), q = P({ x: w.x + x - p0.x, y: w.y + y - p0.y }, w.z);
      return { x: w.screen.x + q.x - anchor.x, y: w.screen.y + q.y - anchor.y };
    };
    const a = at(s.x, s.y), b = at(s.ax, s.ay);
    return { ...s, x: a.x, y: a.y, ax: b.x, ay: b.y };
  });
  const planeZ = new Map(planes.map((p) => [p.id, p.z]));
  const clusters = (lay.clusters ?? []).map((c) => {
    const zc = vm.global ? planeZ.get(c.id) ?? 0 : 0;
    const q = P(c, zc);
    // global: a sphere (its outline a circle at any tilt); per shoal the cluster is only the hit area
    const globeGrid = [];
    if (vm.global) {
      const R = c.r * SPHERE_R;
      for (const lat of [-Math.PI / 6, 0, Math.PI / 6]) {
        globeGrid.push(...globeCircle(cam, q, R * q.f, { x: 0, y: 0, z: Math.sin(lat) }, { x: Math.cos(lat), y: 0, z: 0 }, { x: 0, y: Math.cos(lat), z: 0 }));
      }
      for (let i = 0; i < 3; i++) {
        const a = i * Math.PI / 3;
        globeGrid.push(...globeCircle(cam, q, R * q.f, { x: 0, y: 0, z: 0 }, { x: Math.cos(a), y: Math.sin(a), z: 0 }, { x: 0, y: 0, z: 1 }));
      }
    }
    // the way to the light from the globe's middle, as the camera sees it: what its glass is shaded from
    return { ...c, x: q.x, y: q.y, r: c.r * (vm.global ? SPHERE_R : 1) * q.f, ry: vm.global ? 1 : cam.c, sphere: vm.global, globeGrid, s: q.f, depth: q.depth, zw: zc,
      light: lightFrom(cam, Lp, { x: c.x, y: c.y, z: zc }) };
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
  // the floor: a grid below the lowest plane and below every globe, its lines converging with distance
  const floorZ = Math.min(-FLOOR_BELOW * PLANE_GAP,
    ...(vm.global ? (lay.clusters ?? []).map((c) => (planeZ.get(c.id) ?? 0) - c.r * SPHERE_R) : [])) - (vm.global ? FLOOR_CLEAR * PLANE_GAP : 0);
  const grid = floorGrid(P, o, radius * 1.15, floorZ);
  // stems: each plane's middle (per shoal: the band ring's), and every lifted node, down to the floor; and the shadow each
  // globe and lifted node casts there, all from the one light
  const stems = [];
  const stem = (id, p, zz, r) => {
    const a = P(p, zz), b = P(p, floorZ);
    stems.push({ id, x0: a.x, y0: a.y, x1: b.x, y1: b.y, r: r * b.f, ry: r * b.f * cam.c, shadow: castShadow(P, Lp, p, zz, r, floorZ) });
  };
  if (vm.global) for (const c of lay.clusters ?? []) stem(`c-${c.id}`, c, planeZ.get(c.id) ?? 0, c.r * SPHERE_R);
  else if ((lay.bands ?? []).length) for (const b of lay.bands) stem(`b-${b.host}`, { x: 0, y: 0 }, planeZ.get(b.host) ?? 0, 26);
  for (const n of vm.nodes) if (n.kind !== "role" && lay.pos.has(n.id)) stem(`n-${n.id}`, lay.pos.get(n.id), z(n.id), n.kind === "user" ? 30 : 14);
  // and each role on a globe: the globe is glass and catches no shadow, so a role's falls on the floor too, cast the same
  // way from where the role is in the world (2026-11-13: without one, a seat's shadow that fell under a globe read as its
  // roles', and their edges' shadows ended in nothing)
  const casts = [];
  for (const n of vm.nodes) {
    const w = placed.get(n.id);
    if (w) casts.push({ id: `r-${n.id}`, shadow: castShadow(P, Lp, w, w.z, Math.max(4, n.visual?.r ?? 8), floorZ) });
  }
  // where each node's shadow falls on the floor (the ray from the light through it), for its edges' shadows: {x, y} on
  // screen and {dx, dy} from the node as drawn
  const shadowOf = new Map();
  for (const [id, p] of lay.pos) {
    const w = placed.get(id), g = floorCast(Lp, w ?? { x: p.x, y: p.y, z: z(id) }, floorZ), sp = P(g, floorZ), at = pos.get(id);
    shadowOf.set(id, { x: sp.x, y: sp.y, dx: sp.x - at.x, dy: sp.y - at.y });
  }
  // what an edge's shadow is cast with (scene.js castCurve): each end's depth along its line of sight, the point of
  // the world seen at a layout point at a depth (the edge's 3D path is its curve on screen, lifted between its ends'
  // depths), where the light's ray through it meets the floor, whether the floor is there, and where that is on screen
  const floorR = radius * 1.15;
  const shade = {
    surface: "floor", light: Lp, floorZ, depthOf: depth,
    lift: (s, dd) => { const q = unproject(cam, s, dd); return { x: q.x, y: q.y, z: q.z + mid }; },
    cast: (X) => (X.z > floorZ && X.z < Lp.z ? { ...floorCast(Lp, X, floorZ), z: floorZ } : null),
    seen: (W) => Math.abs(W.x - o.x) <= floorR && Math.abs(W.y - o.y) <= floorR,
    screen: (W) => { const q = P(W, W.z); return { x: q.x, y: q.y }; },
  };
  const order = [...vm.nodes].sort((a, b) => (depth.get(a.id) ?? 0) - (depth.get(b.id) ?? 0));
  return { ...lay, pos, sats, clusters, bands, anchors, tethers, cam, scaleOf, fogOf, surfaceOf, order, planes, grid, stems, casts, floorZ, light: Lp, shadowOf, shade };
}

/**
 * A circle of a globe's unit sphere, u(t) = C + A cos t + B sin t, seen through `cam` on the globe whose middle is at q
 * (radius Rf on screen): its arcs on the near side and on the far side, [{front, cmds}], each a move and cubics. The
 * globe is seen square on (its outline a circle), so the circle's image is an ellipse, the affine image of (cos t,
 * sin t): a cubic per quarter turn of t stays within a fraction of a pixel of it, where 96 straight lines a circle
 * cost the global scene about 1,100 path commands for its two globes' grids. The two sides meet where the circle
 * crosses the globe's outline, n . u(t) = 0 (n toward the camera).
 */
export function globeCircle(cam, q, Rf, C, A, B) {
  const sx = (u) => q.x + Rf * (u.x * cam.cyaw - u.y * cam.syaw), sy = (u) => q.y + Rf * ((u.x * cam.syaw + u.y * cam.cyaw) * cam.c - u.z * cam.s);
  const dx = (u) => Rf * (u.x * cam.cyaw - u.y * cam.syaw), dy = (u) => Rf * ((u.x * cam.syaw + u.y * cam.cyaw) * cam.c - u.z * cam.s);
  const facing = (u) => (u.x * cam.syaw + u.y * cam.cyaw) * cam.s + u.z * cam.c;
  const at = (t) => ({ x: C.x + A.x * Math.cos(t) + B.x * Math.sin(t), y: C.y + A.y * Math.cos(t) + B.y * Math.sin(t), z: C.z + A.z * Math.cos(t) + B.z * Math.sin(t) });
  const tan = (t) => ({ x: -A.x * Math.sin(t) + B.x * Math.cos(t), y: -A.y * Math.sin(t) + B.y * Math.cos(t), z: -A.z * Math.sin(t) + B.z * Math.cos(t) });
  const fc = facing(C), fa = facing(A), fb = facing(B), amp = Math.hypot(fa, fb);
  let spans = [[0, 2 * Math.PI]];
  if (amp > 1e-12 && Math.abs(fc) < amp) {
    const ph = Math.atan2(fb, fa), d = Math.acos(-fc / amp);
    spans = [[ph - d, ph + d], [ph + d, ph - d + 2 * Math.PI]];
  }
  return spans.map(([t0, t1]) => {
    const p0 = at(t0), cmds = [["M", sx(p0), sy(p0)]];
    const n = Math.max(1, Math.ceil((t1 - t0) / (Math.PI / 2) - 1e-9));
    for (let i = 0; i < n; i++) {
      const a = t0 + ((t1 - t0) * i) / n, b = t0 + ((t1 - t0) * (i + 1)) / n, k = (4 / 3) * Math.tan((b - a) / 4);
      const p = at(a), r = at(b), u = tan(a), v = tan(b);
      cmds.push(["C", sx(p) + k * dx(u), sy(p) + k * dy(u), sx(r) - k * dx(v), sy(r) - k * dy(v), sx(r), sy(r)]);
    }
    return { front: facing(at((t0 + t1) / 2)) >= 0, cmds };
  });
}

/** A floor grid at height z, R either side of o, seen through P: GRID + 1 lines each way, the outermost marked. */
function floorGrid(P, o, R, z) {
  const grid = [];
  for (let i = 0; i <= GRID; i++) {
    const t = -R + (2 * R * i) / GRID;
    for (const [a, b] of [[{ x: o.x + t, y: o.y - R }, { x: o.x + t, y: o.y + R }], [{ x: o.x - R, y: o.y + t }, { x: o.x + R, y: o.y + t }]]) {
      const p = P(a, z), q = P(b, z);
      grid.push({ x0: p.x, y0: p.y, x1: q.x, y1: q.y, edge: i === 0 || i === GRID });
    }
  }
  return grid;
}

// Per shoal, depth mode: the cutout sphere. The operator floats on the axis of a sphere whose near part is cut away
// (sphere.js has its frame), lifted from its middle toward the opening (YOU_LIFT), so the camera looks down into a bowl
// and in from the side, in perspective: you are nearer than the bowl and drawn larger, its bottom below and beside you,
// a plumb line from you down to it, and your edges run from you back into the bowl to the roles. The hosts' lunes tile
// the inner surface, pole to rim (bowlLunes): each over its sector of the flat view widened to the middle of the gaps
// either side, turned with the camera's spin. A host's roles sit on its lune, spread across its sector by label order,
// in two rows when there are four or more, each kept inside the rim and behind you. Outside seats and anything else
// ring the rim, in the cut's plane. `,` and `.` spin the bowl about its axis; the arrows orbit the camera round it
// (bowlTilt, bowlTurn), so you are seen to stand off the far wall.
/** How far you float from the sphere's middle up its axis toward the opening, in sphere radii: out at the cut, so you
 * read clearly in front of the inner surface (0.28 read as on it; maintainer, 2026-11-12). */
export const YOU_LIFT = 0.5;
/** Your size in the bowl over a role's at the same depth: perspective alone undersold how near you are. */
export const YOU_GAIN = 1.2;
const YOU_CLEAR = 0.06; // a role is moved down the wall until it is this far clear of you on screen, in sphere radii
const ROLE_CLEAR = 0.07; // and this far clear of every role placed before it
const MERIDIAN_SEARCH = 20; // and if moving it down does not clear it, this many places up and down its meridian
const LUNE_SEARCH = 24; // and if none of those does, in the preset's view, this many meridians of its lune
const BOWL_EXT = 1.32; // where outside seats ring the rim, in sphere radii
const BOWL_GAIN = 1.25; // sizes in the bowl: this much larger than flat, times the perspective scale where they are
const BOWL_BEHIND = 0.2; // how far behind you, along the line of sight, a role on the inside sits at least
// sizes shrink with that depth this much faster than perspective alone, so the far wall reads far
const SIZE_DEPTH = 1.6;
/** How far in from the wall a role's middle is, in sphere radii, for its edges' shadows: the edges at a role run just
 * off the wall, so their shadows lie a little beside them, more where the light comes in low. */
export const EDGE_LIFT = 0.07;

/** The bowl's pitch for a camera: how far off its axis, vertically, the camera looks down into it: BOWL_HOME_TILT plus
 * the vertical orbit, in BOWL_ALPHA_RANGE (the same opening view in tilt and iso). */
export const bowlTilt = (cam) => bowlAngles(cam.orbit).alpha;
/** The bowl's turn for a camera: BOWL_HOME_TURN round the screen's vertical, plus the horizontal orbit, in
 * BOWL_BETA_RANGE and held to what the tilt leaves of BOWL_OFF_AXIS_MAX. */
export const bowlTurn = (cam) => bowlAngles(cam.orbit).beta;
/** Where you float in the bowl's frame (camera space, sphere radii): YOU_LIFT from the middle, between the axis up toward
 * the opening and the line toward the camera (YOU_TOWARD of the latter to one of the former). Straight up the axis, a
 * camera far round the side saw you over the near wall, just in front of it; leaning toward the camera, the wall behind
 * you stays the far one at every orbit, and you still sit off the axis, so the plumb line to the bottom shows how far
 * out you float. Orbited far round the side (the axis more than 55 degrees off the line of sight, up to 72: bowlSide),
 * you lean further toward the camera (to 3.5 to 1), so you stay over the opening, not over its rim. */
const YOU_TOWARD = 2, YOU_TOWARD_SIDE = 3.5;
/** How far round the side a frame is seen: 0 with the axis up to 55 degrees off the line of sight, easing to 1 at
 * 72 (sphere.js SIDE_FULL: the old combined limit, so the opening view, 70.6 off, places you as the maintainer saw it
 * there), held on to BOWL_OFF_AXIS_MAX (sphere.js sideOf: the cut's outer wall grows with it too). */
export const bowlSide = (fr) => sideOf(Math.acos(Math.max(-1, Math.min(1, -fr.pole.z))));
export const bowlYou = (fr) => {
  const toward = YOU_TOWARD + (YOU_TOWARD_SIDE - YOU_TOWARD) * bowlSide(fr);
  const x = -fr.pole.x, y = -fr.pole.y, z = toward - fr.pole.z, l = Math.hypot(x, y, z) || 1;
  return { x: (YOU_LIFT * x) / l, y: (YOU_LIFT * y) / l, z: (YOU_LIFT * z) / l };
};

/** The polar angle (from the bowl's pole) of the j-th of m roles at azimuth phi: a fraction of what is seen there from
 * `home` (the preset's frame), so a role keeps its place on the surface as the camera orbits, and never more than is
 * seen there from `fr`, so it stays in view. Orbited far round the side the bottom itself comes near the middle's depth,
 * so what counts as behind you there eases from BOWL_BEHIND behind the middle to BOWL_BEHIND_SIDE in front of it (you,
 * leaning further toward the camera there, are still well in front): otherwise every role near the bottom's side was
 * pressed onto the bottom, and two of them met. */
const BOWL_BEHIND_SIDE = 0.05;
export const bowlBehind = (fr) => -BOWL_BEHIND + (BOWL_BEHIND + BOWL_BEHIND_SIDE) * bowlSide(fr);
export function bowlTheta(fr, phi, j, m, home = fr) {
  const t = bowlReach(home, phi, 0.86, bowlBehind(home)) * (m >= 4 ? (j % 2 ? 0.6 : 0.9) : 0.8);
  return home === fr ? t : Math.min(t, bowlReach(fr, phi, 0.86, bowlBehind(fr)));
}

/**
 * The hosts' lunes, in the bands' order: each band's sector widened to the middle of the gaps either side of it, so
 * neighbors meet and the spans add up to a whole turn (one host: the whole bowl). {...band, a0, a1, s0, s1}: the lune's
 * azimuths and the sector's, both turned by yaw.
 */
export function bowlLunes(bands, yaw = 0) {
  const TAU = 2 * Math.PI, n = bands.length;
  const sorted = [...bands].sort((a, b) => a.a0 - b.a0);
  const gap = (from, to) => ((to - from) % TAU + TAU) % TAU;
  return bands.map((b) => {
    if (n === 1) return { ...b, a0: b.a0 + yaw, a1: b.a0 + yaw + TAU, s0: b.a0 + yaw, s1: b.a1 + yaw };
    const i = sorted.indexOf(b), prev = sorted[(i + n - 1) % n], next = sorted[(i + 1) % n];
    return { ...b, a0: b.a0 - gap(prev.a1, b.a0) / 2 + yaw, a1: b.a1 + gap(b.a1, next.a0) / 2 + yaw, s0: b.a0 + yaw, s1: b.a1 + yaw };
  });
}

function projectBowl(vm, lay, cam0) {
  const c0 = lay.clusters?.[0];
  const Rs = (c0?.r ?? 230) * SPHERE_R;
  const fr = bowlFrame(bowlTilt(cam0), BOWL_DIST, bowlTurn(cam0));
  const pts = [...lay.pos.values()];
  const radius = Math.max(200, Rs * BOWL_EXT, ...pts.map((p) => Math.hypot(p.x, p.y)));
  const cam = { ...cam0, o: { x: 0, y: 0 }, d: DIST * radius };
  const spin = cam.spin ?? cam.yaw;
  const bands = bowlLunes(lay.bands ?? [], spin).map((b) => ({ ...b, bowl: true, depth: -Rs, fog: 1 }));
  const pos = new Map(), depth = new Map(), scaleOf = new Map(), fogOf = new Map(), surfaceOf = new Map();
  // a point of the bowl's world (in sphere radii, the camera's frame) on screen, in perspective. Its paint order goes
  // by its depth along the line of sight and its size by how near it is: you, lifted toward the opening, are nearest
  // and largest (in perspective), every role on the inside behind the middle (BOWL_BEHIND at least) and smaller, faster
  // than perspective alone, the far wall's smallest.
  const put = (id, u) => {
    const s = bowlProject(fr, u);
    pos.set(id, { x: Rs * s.x, y: Rs * s.y });
    depth.set(id, Rs * u.z);
    return u.z >= 0 ? fr.d / (fr.d - u.z) : fr.d / (fr.d - SIZE_DEPTH * u.z);
  };
  // where a point's shadow falls on the inner wall (the ray from the bowl's light through it), when it is seen there:
  // {x, y} on screen and {dx, dy} from the point
  const shadowOf = new Map(), liftZ = new Map();
  const castAt = (id, X) => {
    liftZ.set(id, X.z);
    const w = bowlCast(fr, X);
    if (!bowlKept(fr, w) || bowlSeen(fr, w) > 1) return;
    const s = bowlProject(fr, w), q = bowlProject(fr, X);
    shadowOf.set(id, { x: Rs * s.x, y: Rs * s.y, dx: Rs * (s.x - q.x), dy: Rs * (s.y - q.y) });
  };
  // the operator on the axis, lifted toward the opening: nearest the camera of anything in the bowl
  const you = bowlYou(fr);
  const user = vm.nodes.find((n) => n.kind === "user" && lay.pos.has(n.id));
  if (user) { scaleOf.set(user.id, YOU_GAIN * BOWL_GAIN * put(user.id, you)); castAt(user.id, you); }
  // roles: on their host's lune, spread across its sector in the flat view's order, at the place the preset gives them
  // (so they stay put on the surface as the camera orbits), moved down toward the bottom if you would hide them
  const TAU = 2 * Math.PI;
  const flatAngle = (id) => { const p = lay.pos.get(id); return p && Math.hypot(p.x, p.y) > 1e-6 ? Math.atan2(p.y, p.x) : -Math.PI / 2; };
  const roles = vm.nodes.filter((n) => n.kind === "role" && lay.pos.has(n.id));
  const home = cam.orbit && (cam.orbit.x || cam.orbit.y) ? bowlFrame(bowlTilt({ name: cam.name }), BOWL_DIST, bowlTurn({ name: cam.name })) : fr;
  // Each role's place, seen from frame f: from the preset's frame (`from` null) across its sector, or where it was
  // wanted there (`from`: id -> {phi, wanted}), as far up as f sees; moved down its meridian until it is clear of you
  // and of every role placed before it; if that does not clear it, the clear place nearest where it was wanted, up its
  // meridian too; and in the preset's frame, if none there is clear, the clear place on its lune, seen inside the rim,
  // nearest where it was wanted (2026-11-13: with more of the outside shown, the near wall hides most of a lune on the
  // camera's side, so its roles all came down to the little of it seen near the bottom, and met there). Placed in the
  // preset's frame first, so a role keeps its place on the surface as the camera orbits (its azimuth kept: only the
  // preset's frame searches the lune), and moves only along its meridian to stay clear and in view.
  const place = (f, from) => {
    const fYou = bowlYou(f), fYouS = bowlProject(f, fYou), fYouR = user ? ((user.visual?.r ?? 0) * YOU_GAIN * BOWL_GAIN * f.d / (f.d - fYou.z)) / Rs : 0;
    const rolesAt = [], out = new Map(); // the roles placed so far, on screen: {x, y, r} in sphere radii
    for (const b of bands) {
      const rel = (id) => ((flatAngle(id) + spin - b.s0) % TAU + TAU) % TAU;
      const mine = roles.filter((n) => (n.host ?? "?") === b.host).sort((x, y) => rel(x.id) - rel(y.id) || (x.id < y.id ? -1 : 1));
      mine.forEach((n, j) => {
        let phi = from?.get(n.id)?.phi ?? b.s0 + ((b.s1 - b.s0) * (j + 0.5)) / mine.length;
        let theta = from ? Math.min(from.get(n.id).wanted, bowlReach(f, phi, 0.86, bowlBehind(f))) : bowlTheta(f, phi, j, mine.length);
        let u = bowlPoint(f, theta, phi), wanted = theta;
        const want = bowlProject(f, u);
        // its radius on screen there, in sphere radii; clear of you, and of every role placed before it (orbited far round
        // the side, the far wall's reach narrows and two roles near a lune's edge would meet there)
        const rAt = (w) => ((n.visual?.r ?? 0) * BOWL_GAIN * f.d / (f.d - SIZE_DEPTH * w.z)) / Rs;
        // how far clear of you and of every role placed before it a point is, in sphere radii (under 0: they meet)
        const slack = (w) => {
          const s = bowlProject(f, w), r = rAt(w);
          let m = Math.hypot(s.x - fYouS.x, s.y - fYouS.y) - (fYouR + r + YOU_CLEAR);
          for (const q of rolesAt) m = Math.min(m, Math.hypot(s.x - q.x, s.y - q.y) - (q.r + r + ROLE_CLEAR));
          return m;
        };
        for (let i = 0; i < 14 && theta > 0.05 && slack(u) < 0; i++) {
          theta *= 0.85;
          u = bowlPoint(f, theta, phi);
        }
        if (slack(u) < 0) {
          // not clear lower down: the clear place on its meridian nearest where it was wanted, up it too, as far as f sees
          let best = null;
          const top = bowlReach(f, phi, 0.86, bowlBehind(f));
          for (let k = 1; k <= MERIDIAN_SEARCH; k++) {
            const th = (top * k) / MERIDIAN_SEARCH, w = bowlPoint(f, th, phi), sl = slack(w);
            if (sl >= 0 ? !best?.ok || Math.abs(th - wanted) < best.key : !best?.ok && (!best || sl > best.sl)) best = { ok: sl >= 0, key: Math.abs(th - wanted), sl, th, w };
          }
          // and in the preset's frame, if none there is clear, the clear place on its lune nearest where it was wanted
          if (!best.ok && !from) {
            for (let a = 0; a < LUNE_SEARCH; a++) {
              // (inside the lune, a little clear of its edges, so the lune under the role is its host's)
              const ph = b.a0 + (b.a1 - b.a0) * (0.04 + (0.92 * (a + 0.5)) / LUNE_SEARCH), reach = bowlReach(f, ph, 0.86, bowlBehind(f));
              for (let k = 1; k <= 8; k++) {
                const th = (reach * k) / 8, w = bowlPoint(f, th, ph), q = bowlProject(f, w), dist = Math.hypot(q.x - want.x, q.y - want.y);
                if (slack(w) >= 0 && (!best.ok || dist < best.dist)) best = { ok: true, dist, th, ph, w };
              }
            }
            if (best.ok) phi = best.ph;
          }
          ({ th: theta, w: u } = best);
          if (best.ph !== undefined) wanted = theta;
        }
        rolesAt.push({ ...bowlProject(f, u), r: rAt(u) });
        out.set(n.id, { phi, theta, u, host: b.host, wanted });
      });
    }
    return out;
  };
  const at = place(fr, home === fr ? null : place(home, null));
  for (const n of roles) {
    if (!at.has(n.id)) continue;
    const { u, theta, phi, host } = at.get(n.id);
    castAt(n.id, { x: (1 - EDGE_LIFT) * u.x, y: (1 - EDGE_LIFT) * u.y, z: (1 - EDGE_LIFT) * u.z });
    scaleOf.set(n.id, BOWL_GAIN * put(n.id, u));
    fogOf.set(n.id, 0.82 + 0.18 * (1 + u.z));
    surfaceOf.set(n.id, { shoal: vm.view, host, facing: -u.z, theta, phi });
  }
  // everything else on a ring round the rim, in its plane
  if (user) fogOf.set(user.id, 1);
  for (const n of vm.nodes) {
    if (pos.has(n.id) || !lay.pos.has(n.id)) continue;
    put(n.id, bowlRing(fr, flatAngle(n.id) + spin, BOWL_EXT));
    scaleOf.set(n.id, 1);
    fogOf.set(n.id, 1);
  }
  // satellites keep their spokes, turned with their role's place round the middle
  const sats = (lay.sats ?? []).map((s) => {
    const w = pos.get(s.owner), p0 = lay.pos.get(s.owner);
    if (!w || !p0) return s;
    const turn = Math.hypot(w.x, w.y) > 1e-6 && Math.hypot(p0.x, p0.y) > 1e-6 ? Math.atan2(w.y, w.x) - Math.atan2(p0.y, p0.x) : spin;
    const k = scaleOf.get(s.owner) ?? 1, cs = Math.cos(turn), sn = Math.sin(turn);
    const at = (x, y) => ({ x: w.x + k * ((x - p0.x) * cs - (y - p0.y) * sn), y: w.y + k * ((x - p0.x) * sn + (y - p0.y) * cs) });
    const a = at(s.x, s.y), b = at(s.ax, s.ay);
    return { ...s, x: a.x, y: a.y, ax: b.x, ay: b.y };
  });
  // a CI tether ends in the middle of its host's lune
  const anchors = new Map();
  for (const b of bands) {
    const phi = (b.a0 + b.a1) / 2, s = bowlProject(fr, bowlPoint(fr, bowlReach(fr, phi, 0.86, bowlBehind(fr)) * 0.5, phi));
    anchors.set(b.host, { x: Rs * s.x, y: Rs * s.y, band: true });
  }
  const tethers = (lay.tethers ?? []).map((t) => ({ ...t, from: pos.get(t.id) ?? t.from, to: t.to && anchors.has(t.host) ? { ...anchors.get(t.host) } : null }));
  const clusters = c0 ? [{ ...c0, x: 0, y: 0, r: Rs, ry: 1, sphere: false, bowl: { alpha: fr.alpha, beta: fr.beta }, globeGrid: [], s: 1, depth: -Rs, fog: 1, zw: 0 }] : [];
  // the floor: the bowl's own (square to its axis, below it, sphere.js bowlFloor), with a stem from the bowl's bottom
  // down to it and the bowl's shadow there, cast by the bowl's light
  const floor = bowlFloor(fr), sc = (v) => ({ x: Rs * v.x, y: Rs * v.y });
  const grid = floor.lines.map((g) => ({ x0: Rs * g.x0, y0: Rs * g.y0, x1: Rs * g.x1, y1: Rs * g.y1, edge: g.edge }));
  const stems = [];
  if (c0) {
    const a = bowlProject(fr, floor.top), b = bowlProject(fr, floor.foot), sh = floor.shadow;
    stems.push({ id: `c-${c0.id}`, x0: Rs * a.x, y0: Rs * a.y, x1: Rs * b.x, y1: Rs * b.y,
      shadow: sh ? { x: Rs * sh.x, y: Rs * sh.y, a: sc(sh.a), b: sc(sh.b), peak: 0.62, soft: 0.4, h: sh.h, at: sh.at } : null });
  }
  // what an edge's shadow is cast with (scene.js castCurve), as in the global view: each end's depth (you; a role's
  // middle, EDGE_LIFT off the wall), the point seen at a layout point at a depth, held inside the sphere (sphere.js
  // bowlLift), where the light's ray through it leaves the sphere (none from outside it), whether that is on the inside
  // seen through the opening, and where it is on screen. A seat on the ring has no depth here: its edges leave the bowl
  // over the lip, and the part of one near the opening cast a long thin streak across the inside that read as no arrow
  // (its shadow outside falls on the outer wall and the floor, which are not drawn), so they cast none
  const shade = {
    surface: "wall", light: bowlLightAt(fr), frame: fr, R: Rs, depthOf: liftZ,
    lift: (s, z) => bowlLift(fr, { x: s.x / Rs, y: s.y / Rs }, z),
    cast: (X) => (X.x * X.x + X.y * X.y + X.z * X.z < 1 ? bowlCast(fr, X) : null),
    seen: (W) => bowlKept(fr, W) && bowlSeen(fr, W) <= 1,
    screen: (W) => { const q = bowlProject(fr, W); return { x: Rs * q.x, y: Rs * q.y }; },
  };
  const order = [...vm.nodes].sort((a, b) => (depth.get(a.id) ?? 0) - (depth.get(b.id) ?? 0));
  return { ...lay, pos, sats, clusters, bands, anchors, tethers, cam, scaleOf, fogOf, surfaceOf, order, planes: [], grid, stems, shadowOf, shade,
    light: null, bowl: { frame: fr, R: Rs, you, youLight: bowlLightDir(fr, you) } };
}

/** The next preset in the cycle. */
export function nextDepth(name) {
  return DEPTH_CYCLE[(DEPTH_CYCLE.indexOf(name) + 1) % DEPTH_CYCLE.length] ?? "flat";
}

/** The camera that applies: a tilt is dropped to flat at the low and off tiers on a CPU renderer (the design's rule). */
export function depthFor(name, tier, renderer) {
  return renderer === "cpu" && (tier === "low" || tier === "off") ? "flat" : CAMERAS[name] === undefined ? "flat" : name;
}
