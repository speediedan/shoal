// depth.js: the lattice's depth mode (P3b), pure (no Herdr imports, so it tests under Node). GPUI has no 3D, but every
// position is the mod's own: in the global view each node gets a plane (its shoal's; the operator and outside seats on
// a plane above), and the layout is projected before the scene draws it. A camera turns the planes (yaw), tilts them
// back (pitch) and sees them in perspective from a distance, so far planes shrink toward the horizon and near ones grow.
// Per shoal, the view is a cutout sphere instead (projectBowl): you at its middle, each host a lune of its inner surface.
// The scene adds what makes it read as space: a floor grid below everything, stems down to it, a shadow on it for each
// globe and lifted seat (all cast by the one light that shades the globes), and fog on what is far.

import { LIGHT, BOWL_DIST, bowlFrame, bowlPoint, bowlProject, bowlReach, bowlRing } from "./sphere.js";

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
export const DEPTH_CYCLE = ["flat", "tilt", "iso"];
export const PLANE_GAP = 190; // world units between planes, before the tilt
export const YAW_STEP = 15 * DEG;
const DIST = 2.6; // the camera's distance, in layout radii: smaller is stronger perspective
const SCALE_MIN = 0.55, SCALE_MAX = 1.6; // what perspective may do to a size
const FLOOR_BELOW = 0.55; // the floor, below the lowest plane, in plane gaps
const FLOOR_CLEAR = 0.45; // and below the lowest globe's bottom, in plane gaps, so every globe floats above it
const GRID = 10; // floor grid lines each way

/** The one light (sphere.js LIGHT: fixed to the camera, upper left and toward you) as a world direction for `cam`.
 * The globes are shaded from it on screen, so their shadows fall from it in the world. */
export function lightWorld(cam) {
  const yr = LIGHT.y * cam.c + LIGHT.z * cam.s, z = -LIGHT.y * cam.s + LIGHT.z * cam.c;
  return { x: LIGHT.x * cam.cyaw + yr * cam.syaw, y: -LIGHT.x * cam.syaw + yr * cam.cyaw, z };
}

/**
 * The shadow a ball of radius r, its middle at plane point p and height z, casts on the floor at floorZ, seen through P,
 * from the light L (lightWorld). The line from the light through the ball's middle meets the floor at the shadow's
 * middle, so every shadow is offset the same way, by its caster's height; it is r across the light and r / L.z along it
 * (a ball's shadow on a plane, longer the lower the light), its penumbra widening and its core paling as the ball rises.
 * {x, y: its middle on screen, a, b: conjugate semi-axes on screen, peak, soft, h: the caster's height above the floor}.
 */
export function castShadow(P, L, p, z, r, floorZ) {
  const h = Math.max(0, z - floorZ), lz = Math.max(0.05, L.z), t = h / lz;
  const g = { x: p.x - t * L.x, y: p.y - t * L.y };
  const hl = Math.hypot(L.x, L.y);
  const along = hl > 1e-9 ? { x: -L.x / hl, y: -L.y / hl } : { x: 1, y: 0 }, across = { x: -along.y, y: along.x };
  const axis = (d, len) => {
    const q0 = P({ x: g.x - d.x * len, y: g.y - d.y * len }, floorZ), q1 = P({ x: g.x + d.x * len, y: g.y + d.y * len }, floorZ);
    return { x: (q1.x - q0.x) / 2, y: (q1.y - q0.y) / 2 };
  };
  const c = P(g, floorZ), up = h / PLANE_GAP;
  return { x: c.x, y: c.y, a: axis(along, r / lz), b: axis(across, r), peak: 0.8 / (1 + 0.18 * up), soft: Math.min(0.5, 0.2 + 0.06 * up), h };
}

// The arrow keys orbit the camera: left and right round the screen's vertical, up and down round its horizontal (up
// raises the camera, so it looks down from higher). `orbit` {x, y} is that turn, in radians, on top of the preset. In
// the global view x adds to the yaw (round the planes' up, as `,` and `.` do) and y to the pitch, kept in PITCH_RANGE.
// Per shoal, x turns the bowl's axis round the screen's vertical (BOWL_HOME_TURN plus x, kept in BOWL_BETA_RANGE) and y
// tips it further down the screen (BOWL_HOME_TILT plus y, kept in BOWL_ALPHA_RANGE), so the bowl never turns its back or
// rolls over; `,` and `.` still spin it round its own axis. The bowl opens at the view the arrows used to reach at their
// down and left limits (10 and -40 degrees, the maintainer's pick, 2026-11-12), and the limits reach 30% further out than
// they did from the old opening view (28 degrees tilt, 30 turn): down to 4.6, left to -61, right to 43.
export const ORBIT_STEP = 8 * DEG;
export const PITCH_RANGE = [24 * DEG, 80 * DEG];
export const BOWL_HOME_TILT = 10 * DEG;
export const BOWL_HOME_TURN = -40 * DEG;
export const BOWL_ALPHA_RANGE = [4.6 * DEG, 50 * DEG];
export const BOWL_BETA_RANGE = [-61 * DEG, 43 * DEG];
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

/**
 * The orbit after an arrow (dx, dy in radians) on preset `name`, kept to what the view shows: per shoal (`bowl`) the
 * bowl's turn and tilt stay in their ranges; globally the pitch does, and the yaw goes round freely. A turn past a limit
 * is dropped, not stored, so the way back is never dead. Flat: no orbit.
 */
export function orbitBy(name, orbit, dx = 0, dy = 0, bowl = false) {
  const c = CAMERAS[name];
  if (!c) return { x: 0, y: 0 };
  let x = (Number.isFinite(orbit?.x) ? orbit.x : 0) + dx, y = (Number.isFinite(orbit?.y) ? orbit.y : 0) + dy;
  if (bowl) {
    x = clampTo(BOWL_HOME_TURN + x, ...BOWL_BETA_RANGE) - BOWL_HOME_TURN;
    y = clampTo(BOWL_HOME_TILT + y, ...BOWL_ALPHA_RANGE) - BOWL_HOME_TILT;
  } else {
    x = Math.atan2(Math.sin(x), Math.cos(x));
    y = clampTo(c.pitch + y, ...PITCH_RANGE) - c.pitch;
  }
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
      const line = (at) => {
        let points = [], front = null;
        for (let j = 0; j <= 96; j++) {
          const u = at(2 * Math.PI * j / 96);
          const near = (u.x * cam.syaw + u.y * cam.cyaw) * cam.s + u.z * cam.c >= 0;
          const v = { x: q.x + R * q.f * (u.x * cam.cyaw - u.y * cam.syaw),
            y: q.y + R * q.f * ((u.x * cam.syaw + u.y * cam.cyaw) * cam.c - u.z * cam.s) };
          if (front !== null && near !== front) {
            globeGrid.push({ front, points: [...points, v] });
            points = [v];
          }
          points.push(v); front = near;
        }
        if (points.length > 1) globeGrid.push({ front, points });
      };
      for (const lat of [-Math.PI / 6, 0, Math.PI / 6]) line((a) => ({ x: Math.cos(lat) * Math.cos(a), y: Math.cos(lat) * Math.sin(a), z: Math.sin(lat) }));
      for (let i = 0; i < 3; i++) {
        const a = i * Math.PI / 3;
        line((b) => ({ x: Math.cos(b) * Math.cos(a), y: Math.cos(b) * Math.sin(a), z: Math.sin(b) }));
      }
    }
    return { ...c, x: q.x, y: q.y, r: c.r * (vm.global ? SPHERE_R : 1) * q.f, ry: vm.global ? 1 : cam.c, sphere: vm.global, globeGrid, s: q.f, depth: q.depth, zw: zc };
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
  const L = lightWorld(cam);
  const stems = [];
  const stem = (id, p, zz, r) => {
    const a = P(p, zz), b = P(p, floorZ);
    stems.push({ id, x0: a.x, y0: a.y, x1: b.x, y1: b.y, r: r * b.f, ry: r * b.f * cam.c, shadow: castShadow(P, L, p, zz, r, floorZ) });
  };
  if (vm.global) for (const c of lay.clusters ?? []) stem(`c-${c.id}`, c, planeZ.get(c.id) ?? 0, c.r * SPHERE_R);
  else if ((lay.bands ?? []).length) for (const b of lay.bands) stem(`b-${b.host}`, { x: 0, y: 0 }, planeZ.get(b.host) ?? 0, 26);
  for (const n of vm.nodes) if (n.kind !== "role" && lay.pos.has(n.id)) stem(`n-${n.id}`, lay.pos.get(n.id), z(n.id), n.kind === "user" ? 30 : 14);
  const order = [...vm.nodes].sort((a, b) => (depth.get(a.id) ?? 0) - (depth.get(b.id) ?? 0));
  return { ...lay, pos, sats, clusters, bands, anchors, tethers, cam, scaleOf, fogOf, surfaceOf, order, planes, grid, stems, floorZ, light: L };
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
const BOWL_EXT = 1.32; // where outside seats ring the rim, in sphere radii
const BOWL_GAIN = 1.25; // sizes in the bowl: this much larger than flat, times the perspective scale where they are
const BOWL_BEHIND = 0.2; // how far behind you, along the line of sight, a role on the inside sits at least
// sizes shrink with that depth this much faster than perspective alone, so the far wall reads far
const SIZE_DEPTH = 1.6;

/** The bowl's pitch for a camera: how far off its axis, vertically, the camera looks down into it: BOWL_HOME_TILT plus
 * the vertical orbit, in BOWL_ALPHA_RANGE (the same opening view in tilt and iso). */
export const bowlTilt = (cam) => clampTo(BOWL_HOME_TILT + (cam.orbit?.y ?? 0), ...BOWL_ALPHA_RANGE);
/** The bowl's turn for a camera: BOWL_HOME_TURN round the screen's vertical, plus the horizontal orbit, in
 * BOWL_BETA_RANGE. */
export const bowlTurn = (cam) => clampTo(BOWL_HOME_TURN + (cam.orbit?.x ?? 0), ...BOWL_BETA_RANGE);
/** Where you float in the bowl's frame (camera space, sphere radii): YOU_LIFT from the middle, between the axis up toward
 * the opening and the line toward the camera (YOU_TOWARD of the latter to one of the former). Straight up the axis, a
 * camera far round the side saw you over the near wall, just in front of it; leaning toward the camera, the wall behind
 * you stays the far one at every orbit, and you still sit off the axis, so the plumb line to the bottom shows how far
 * out you float. */
const YOU_TOWARD = 2;
export const bowlYou = (fr) => {
  const x = -fr.pole.x, y = -fr.pole.y, z = YOU_TOWARD - fr.pole.z, l = Math.hypot(x, y, z) || 1;
  return { x: (YOU_LIFT * x) / l, y: (YOU_LIFT * y) / l, z: (YOU_LIFT * z) / l };
};

/** The polar angle (from the bowl's pole) of the j-th of m roles at azimuth phi: a fraction of what is seen there from
 * `home` (the preset's frame), so a role keeps its place on the surface as the camera orbits, and never more than is
 * seen there from `fr`, so it stays in view. */
export function bowlTheta(fr, phi, j, m, home = fr) {
  const t = bowlReach(home, phi, 0.86, -BOWL_BEHIND) * (m >= 4 ? (j % 2 ? 0.6 : 0.9) : 0.8);
  return home === fr ? t : Math.min(t, bowlReach(fr, phi, 0.86, -BOWL_BEHIND));
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
  const P = (p, zz) => project(cam, p, zz);
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
  // the operator on the axis, lifted toward the opening: nearest the camera of anything in the bowl
  const you = bowlYou(fr), youS = bowlProject(fr, you);
  const user = vm.nodes.find((n) => n.kind === "user" && lay.pos.has(n.id));
  if (user) scaleOf.set(user.id, YOU_GAIN * BOWL_GAIN * put(user.id, you));
  const youR = user ? ((user.visual?.r ?? 0) * scaleOf.get(user.id)) / Rs : 0;
  // roles: on their host's lune, spread across its sector in the flat view's order, at the place the preset gives them
  // (so they stay put on the surface as the camera orbits), moved down toward the bottom if you would hide them
  const TAU = 2 * Math.PI;
  const flatAngle = (id) => { const p = lay.pos.get(id); return p && Math.hypot(p.x, p.y) > 1e-6 ? Math.atan2(p.y, p.x) : -Math.PI / 2; };
  const roles = vm.nodes.filter((n) => n.kind === "role" && lay.pos.has(n.id));
  const home = cam.orbit && (cam.orbit.x || cam.orbit.y) ? bowlFrame(bowlTilt({ name: cam.name }), BOWL_DIST, bowlTurn({ name: cam.name })) : fr;
  for (const b of bands) {
    const rel = (id) => ((flatAngle(id) + spin - b.s0) % TAU + TAU) % TAU;
    const mine = roles.filter((n) => (n.host ?? "?") === b.host).sort((x, y) => rel(x.id) - rel(y.id) || (x.id < y.id ? -1 : 1));
    mine.forEach((n, j) => {
      const phi = b.s0 + ((b.s1 - b.s0) * (j + 0.5)) / mine.length;
      let theta = bowlTheta(fr, phi, j, mine.length, home), u = bowlPoint(fr, theta, phi);
      const clear = youR + ((n.visual?.r ?? 0) * BOWL_GAIN * fr.d / (fr.d - SIZE_DEPTH * u.z)) / Rs + YOU_CLEAR;
      for (let i = 0; i < 12 && theta > 0.05; i++) {
        const s = bowlProject(fr, u);
        if (Math.hypot(s.x - youS.x, s.y - youS.y) >= clear) break;
        theta *= 0.85;
        u = bowlPoint(fr, theta, phi);
      }
      scaleOf.set(n.id, BOWL_GAIN * put(n.id, u));
      fogOf.set(n.id, 0.82 + 0.18 * (1 + u.z));
      surfaceOf.set(n.id, { shoal: vm.view, host: b.host, facing: -u.z, theta, phi });
    });
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
    const phi = (b.a0 + b.a1) / 2, s = bowlProject(fr, bowlPoint(fr, bowlReach(fr, phi, 0.86, -BOWL_BEHIND) * 0.5, phi));
    anchors.set(b.host, { x: Rs * s.x, y: Rs * s.y, band: true });
  }
  const tethers = (lay.tethers ?? []).map((t) => ({ ...t, from: pos.get(t.id) ?? t.from, to: t.to && anchors.has(t.host) ? { ...anchors.get(t.host) } : null }));
  const clusters = c0 ? [{ ...c0, x: 0, y: 0, r: Rs, ry: 1, sphere: false, bowl: { alpha: fr.alpha, beta: fr.beta }, globeGrid: [], s: 1, depth: -Rs, fog: 1, zw: 0 }] : [];
  const floorZ = -Rs * 1.2 - 0.3 * PLANE_GAP;
  const grid = floorGrid(P, cam.o, radius * 1.15, floorZ);
  const L = lightWorld(cam);
  const stems = [];
  if (c0) {
    const a = P({ x: 0, y: 0 }, 0), b = P({ x: 0, y: 0 }, floorZ);
    stems.push({ id: `c-${c0.id}`, x0: a.x, y0: a.y, x1: b.x, y1: b.y, r: Rs * b.f, ry: Rs * b.f * cam.c, shadow: castShadow(P, L, { x: 0, y: 0 }, 0, Rs, floorZ) });
  }
  const order = [...vm.nodes].sort((a, b) => (depth.get(a.id) ?? 0) - (depth.get(b.id) ?? 0));
  return { ...lay, pos, sats, clusters, bands, anchors, tethers, cam, scaleOf, fogOf, surfaceOf, order, planes: [], grid, stems, floorZ, light: L, bowl: { frame: fr, R: Rs, you } };
}

/** The next preset in the cycle. */
export function nextDepth(name) {
  return DEPTH_CYCLE[(DEPTH_CYCLE.indexOf(name) + 1) % DEPTH_CYCLE.length] ?? "flat";
}

/** The camera that applies: a tilt is dropped to flat at the low and off tiers on a CPU renderer (the design's rule). */
export function depthFor(name, tier, renderer) {
  return renderer === "cpu" && (tier === "low" || tier === "off") ? "flat" : CAMERAS[name] === undefined ? "flat" : name;
}
