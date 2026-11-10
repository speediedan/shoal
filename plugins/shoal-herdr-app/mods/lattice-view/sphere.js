// Smooth, directional globe shading using paths supported by both GPUI and the SVG preview.
// A light contour is a projected small circle, clipped by the visible hemisphere.
// Short cubic arcs keep the smooth lighting without thousands of native line calls per globe.
import { mix, withAlpha } from "./geom.js";

export const LIGHT = { x: -0.46, y: -0.54, z: Math.sqrt(1 - 0.46 ** 2 - 0.54 ** 2) };
const STEPS = 128;
export const lightAt = (x, y) => LIGHT.x * x + LIGHT.y * y + LIGHT.z * Math.sqrt(Math.max(0, 1 - x * x - y * y));

function arc(cmds, point, tangent, start, end) {
  const count = Math.ceil((end - start) / (Math.PI / 2));
  for (let i = 0; i < count; i++) {
    const a = start + (end - start) * i / count, b = start + (end - start) * (i + 1) / count;
    const p = point(a), q = point(b), u = tangent(a), v = tangent(b);
    const k = 4 / 3 * Math.tan((b - a) / 4);
    cmds.push(["C", p.x + k * u.x, p.y + k * u.y, q.x - k * v.x, q.y - k * v.y, q.x, q.y]);
  }
}

function contour(level) {
  const side = Math.hypot(LIGHT.x, LIGHT.y), radius = Math.sqrt(1 - level * level);
  const u = { x: -LIGHT.y / side, y: LIGHT.x / side };
  const v = { x: -LIGHT.z * LIGHT.x / side, y: -LIGHT.z * LIGHT.y / side };
  const point = (a) => ({ x: level * LIGHT.x + radius * (u.x * Math.cos(a) + v.x * Math.sin(a)),
    y: level * LIGHT.y + radius * (u.y * Math.cos(a) + v.y * Math.sin(a)) });
  const tangent = (a) => ({ x: radius * (-u.x * Math.sin(a) + v.x * Math.cos(a)),
    y: radius * (-u.y * Math.sin(a) + v.y * Math.cos(a)) });
  const clipped = level < side;
  const start = clipped ? -Math.asin(level * LIGHT.z / (radius * side)) : -Math.PI / 2;
  const end = clipped ? Math.PI - start : start + 2 * Math.PI;
  const p = point(start), q = point(end), cmds = [["M", p.x, p.y]];
  arc(cmds, point, tangent, start, end);
  if (clipped) {
    const a = Math.atan2(q.y, q.x), b = Math.atan2(p.y, p.x);
    arc(cmds, (t) => ({ x: Math.cos(t), y: Math.sin(t) }), (t) => ({ x: -Math.sin(t), y: Math.cos(t) }),
      a, a + (b - a + 2 * Math.PI) % (2 * Math.PI));
  }
  cmds.push(["Z"]);
  return cmds;
}

const CONTOURS = Array.from({ length: STEPS }, (_, i) => contour(i / STEPS));

/** Opaque color steps keep the shadow side dark; opacity only supplies distance fog. */
export function sphereShade(id, x, y, r, tint, fog = 1) {
  const dark = mix(tint, "#050b17", 0.86), lit = mix(tint, "#ffffff", 0.48);
  const paths = CONTOURS.map((cmds, i) => ({
    t: "path", id: `sphere-lit-${id}-${i}`, fill: true,
    cmds: cmds.map((c) => [c[0], ...c.slice(1).map((n, j) => (j % 2 ? y : x) + r * n)]),
    bg: { kind: "solid", color: withAlpha(mix(dark, lit, (i / (STEPS - 1)) ** 0.85), fog) },
  }));
  return { dark, paths };
}
