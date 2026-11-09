// Smooth, directional globe shading using paths supported by both GPUI and the SVG preview.
// A light contour is convex on the visible hemisphere. Trace it outwards from the highlight,
// stopping at the silhouette or the requested light level, then cache the unit geometry.
import { mix, polygonCmds, withAlpha } from "./geom.js";

export const LIGHT = { x: -0.46, y: -0.54, z: Math.sqrt(1 - 0.46 ** 2 - 0.54 ** 2) };
const STEPS = 128, SIDES = 80;
export const lightAt = (x, y) => LIGHT.x * x + LIGHT.y * y + LIGHT.z * Math.sqrt(Math.max(0, 1 - x * x - y * y));

function contour(level) {
  return Array.from({ length: SIDES }, (_, i) => {
    const a = 2 * Math.PI * i / SIDES, dx = Math.cos(a), dy = Math.sin(a);
    const dot = LIGHT.x * dx + LIGHT.y * dy;
    let lo = 0, hi = -dot + Math.sqrt(dot * dot + LIGHT.z * LIGHT.z);
    if (lightAt(LIGHT.x + hi * dx, LIGHT.y + hi * dy) < level) {
      for (let j = 0; j < 18; j++) {
        const mid = (lo + hi) / 2;
        if (lightAt(LIGHT.x + mid * dx, LIGHT.y + mid * dy) >= level) lo = mid;
        else hi = mid;
      }
      hi = lo;
    }
    return { x: LIGHT.x + hi * dx, y: LIGHT.y + hi * dy };
  });
}

const CONTOURS = Array.from({ length: STEPS }, (_, i) => contour(i / STEPS));

/** Opaque color steps keep the shadow side dark; opacity only supplies distance fog. */
export function sphereShade(id, x, y, r, tint, fog = 1) {
  const dark = mix(tint, "#050b17", 0.86), lit = mix(tint, "#ffffff", 0.48);
  const paths = CONTOURS.map((points, i) => ({
    t: "path", id: `sphere-lit-${id}-${i}`, fill: true,
    cmds: polygonCmds(points.map((p) => [x + r * p.x, y + r * p.y])),
    bg: { kind: "solid", color: withAlpha(mix(dark, lit, (i / (STEPS - 1)) ** 0.85), fog) },
  }));
  return { dark, paths };
}
