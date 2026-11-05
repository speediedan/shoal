// logo_reef.mjs: the reef inside each glass letter, as SVG in the letter's cap units (x from its left, y 0 on the cap
// line, 1 on the baseline), for gen_logo_image.mjs. Drawn procedurally and seeded, so it comes out the same every time.
// The reef keeps to the lower part of each letter and the letter's own strokes, and everything is clipped to the
// letter by the caller.

const n3 = (v) => Math.round(v * 10000) / 10000;

export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A kelp blade rising from (x, y) by `h`, swaying sideways and tapering to a point. */
export function kelp(x, y, h, { w = 0.05, sway = 0.03, phase = 0, color = "#2f9e57", rib = "#8ee08f" } = {}) {
  const n = 14;
  const center = (t) => [x + sway * Math.sin(phase + t * 7.5) * (0.35 + 0.65 * t), y - h * t];
  const half = (t) => (w / 2) * (0.55 + 1.5 * t) * Math.min(1, (1 - t) * 2.6);
  const left = [], right = [], mid = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const [cx, cy] = center(t);
    const dx = center(Math.min(1, t + 0.03))[0] - center(Math.max(0, t - 0.03))[0];
    const k = 1 + dx * 2;
    left.push(`${n3(cx - half(t) * k)} ${n3(cy)}`);
    right.unshift(`${n3(cx + half(t) / k)} ${n3(cy)}`);
    mid.push(`${n3(cx)} ${n3(cy)}`);
  }
  return `<path d="M${left.join("L")}L${right.join("L")}Z" fill="${color}" stroke="${color}" stroke-width="0.004" stroke-linejoin="round"/>`
    + `<path d="M${mid.join("L")}" fill="none" stroke="${rib}" stroke-opacity="0.7" stroke-width="0.007" stroke-linecap="round"/>`;
}

/** A branching coral from (x, y): each branch forks and thins, ending in a rounded tip. */
export function coral(x, y, size, seed, { color = "#ff7a2f", tip = "#ffb36b", spread = 0.62 } = {}) {
  const rand = rng(seed);
  const strokes = [];
  const tips = [];
  const grow = (px, py, angle, len, width, depth) => {
    const a = angle + (rand() - 0.5) * 0.3;
    const qx = px + Math.sin(a) * len, qy = py - Math.cos(a) * len;
    const mx = (px + qx) / 2 + Math.cos(a) * len * 0.14 * (rand() - 0.5), my = (py + qy) / 2;
    strokes.push(`<path d="M${n3(px)} ${n3(py)}Q${n3(mx)} ${n3(my)} ${n3(qx)} ${n3(qy)}" stroke-width="${n3(width)}"/>`);
    if (depth <= 0) { tips.push(`<circle cx="${n3(qx)}" cy="${n3(qy)}" r="${n3(width * 0.62)}"/>`); return; }
    grow(qx, qy, a - spread * (0.7 + 0.4 * rand()), len * 0.72, width * 0.74, depth - 1);
    grow(qx, qy, a + spread * (0.7 + 0.4 * rand()), len * 0.72, width * 0.74, depth - 1);
  };
  grow(x, y, 0, size * 0.42, size * 0.17, 2);
  return `<g fill="none" stroke="${color}" stroke-linecap="round">${strokes.join("")}</g><g fill="${tip}">${tips.join("")}</g>`;
}

/** A low dome of brain coral, resting on (x, y). */
export function mound(x, y, rx, ry, { color = "#ef5a4c", line = "#ffb09a" } = {}) {
  const lines = [];
  for (let i = 1; i <= 3; i++) {
    const k = i / 3.6;
    lines.push(`<path d="M${n3(x - rx * (1 - k * 0.35))} ${n3(y)}C${n3(x - rx * 0.6)} ${n3(y - ry * k * 1.25)} ${n3(x + rx * 0.6)} ${n3(y - ry * k * 1.25)} ${n3(x + rx * (1 - k * 0.35))} ${n3(y)}"/>`);
  }
  return `<path d="M${n3(x - rx)} ${n3(y)}C${n3(x - rx)} ${n3(y - ry * 1.3)} ${n3(x + rx)} ${n3(y - ry * 1.3)} ${n3(x + rx)} ${n3(y)}Z" fill="${color}"/>`
    + `<g fill="none" stroke="${line}" stroke-width="0.009" stroke-linecap="round">${lines.join("")}</g>`;
}

/** A scallop shell, its hinge at (x, y), fanning upward. */
export function shell(x, y, s, { color = "#ffe6d0", rib = "#d98c6a", tilt = 0 } = {}) {
  const ribs = [];
  for (let i = -2; i <= 2; i++) {
    const a = (i / 2) * 0.82;
    ribs.push(`<path d="M0 0L${n3(Math.sin(a) * s * 0.96)} ${n3(-Math.cos(a) * s * 0.96)}"/>`);
  }
  return `<g transform="translate(${n3(x)} ${n3(y)}) rotate(${tilt})"><path d="M0 0L${n3(-s * 0.76)} ${n3(-s * 0.68)}Q${n3(-s * 0.5)} ${n3(-s * 1.14)} 0 ${n3(-s * 1.08)}Q${n3(s * 0.5)} ${n3(-s * 1.14)} ${n3(s * 0.76)} ${n3(-s * 0.68)}Z" fill="${color}" stroke="${rib}" stroke-width="0.006" stroke-linejoin="round"/>`
    + `<g fill="none" stroke="${rib}" stroke-width="0.006" stroke-linecap="round">${ribs.join("")}</g></g>`;
}

const FISH = {
  clown: { body: "#ff8c1f", fin: "#f26a10", band: "#ffffff", edge: "#3a1c0a" },
  tang: { body: "#ffd83d", fin: "#f2b418", band: "#1b3f9c", edge: "#7a5a08" },
};

/** A small striped fish, `len` long, centered on (x, y), facing `dir` (1 right, -1 left). */
export function fish(x, y, len, dir, kind = "clown") {
  const p = FISH[kind];
  const a = len / 2, b = len * 0.27;
  const id = `fish-${Math.round(x * 1000)}-${Math.round(y * 1000)}`;
  const bands = [-0.28, 0.2].map((f) => `<rect x="${n3(a * f - len * 0.055)}" y="${n3(-b * 1.2)}" width="${n3(len * 0.11)}" height="${n3(b * 2.4)}" fill="${p.band}"/>`).join("");
  return `<g transform="translate(${n3(x)} ${n3(y)}) scale(${dir} 1)">`
    + `<clipPath id="${id}"><ellipse cx="0" cy="0" rx="${n3(a)}" ry="${n3(b)}"/></clipPath>`
    + `<path d="M${n3(-a * 0.82)} 0L${n3(-a * 1.42)} ${n3(-b * 0.92)}Q${n3(-a * 1.2)} 0 ${n3(-a * 1.42)} ${n3(b * 0.92)}Z" fill="${p.fin}"/>`
    + `<path d="M${n3(-a * 0.25)} ${n3(-b * 0.9)}Q${n3(a * 0.05)} ${n3(-b * 1.75)} ${n3(a * 0.38)} ${n3(-b * 0.75)}Z" fill="${p.fin}"/>`
    + `<ellipse cx="0" cy="0" rx="${n3(a)}" ry="${n3(b)}" fill="${p.body}"/>`
    + `<g clip-path="url(#${id})">${bands}</g>`
    + `<ellipse cx="0" cy="0" rx="${n3(a)}" ry="${n3(b)}" fill="none" stroke="${p.edge}" stroke-width="0.005"/>`
    + `<circle cx="${n3(a * 0.6)}" cy="${n3(-b * 0.22)}" r="${n3(b * 0.27)}" fill="#fff"/><circle cx="${n3(a * 0.65)}" cy="${n3(-b * 0.22)}" r="${n3(b * 0.15)}" fill="#10131c"/></g>`;
}

/** A bubble: a clear ring with a small highlight. */
export function bubble(x, y, r) {
  return `<circle cx="${n3(x)}" cy="${n3(y)}" r="${n3(r)}" fill="#ffffff" fill-opacity="0.16" stroke="#f2fdff" stroke-opacity="0.9" stroke-width="${n3(Math.max(0.005, r * 0.22))}"/>`
    + `<circle cx="${n3(x - r * 0.32)}" cy="${n3(y - r * 0.34)}" r="${n3(r * 0.24)}" fill="#ffffff"/>`;
}

/** A sandy floor across the letter's width, its top a low wave around `top`, down past the baseline. */
export function sand(x0, x1, top, seed) {
  const rand = rng(seed);
  const n = 8;
  const pts = [];
  for (let i = 0; i <= n; i++) pts.push(`${n3(x0 + ((x1 - x0) * i) / n)} ${n3(top + 0.012 * Math.sin(i * 1.6 + seed) + (rand() - 0.5) * 0.008)}`);
  const specks = [];
  for (let i = 0; i < 26; i++) specks.push(`<circle cx="${n3(x0 + (x1 - x0) * rand())}" cy="${n3(top + 0.02 + (1.02 - top) * rand())}" r="${n3(0.003 + 0.004 * rand())}"/>`);
  return `<path d="M${pts.join("L")}L${n3(x1)} 1.1L${n3(x0)} 1.1Z" fill="url(#sand)"/><g fill="#a97f4c" fill-opacity="0.55">${specks.join("")}</g>`;
}

// One letter's reef, hand placed on its strokes: legibility first, so the bold shapes stay mostly open water.
const GREEN = { color: "#2f9e57", rib: "#8ee08f" };
const DEEP = { color: "#1f7d5c", rib: "#62c9a0" };
export const REEFS = {
  S: () => [
    bubble(0.13, 0.27, 0.022), bubble(0.2, 0.2, 0.014), bubble(0.11, 0.2, 0.01),
    fish(0.38, 0.5, 0.2, -1, "tang"),
    sand(-0.1, 0.9, 0.93, 3),
    kelp(0.57, 0.95, 0.2, { ...GREEN, w: 0.04, sway: 0.02, phase: 1 }),
    coral(0.28, 0.97, 0.15, 11),
    shell(0.45, 0.975, 0.045, { tilt: 12 }),
  ],
  H: () => [
    kelp(0.09, 0.97, 0.56, { ...DEEP, w: 0.05, sway: 0.028, phase: 0.6 }),
    kelp(0.21, 0.97, 0.36, { ...GREEN, w: 0.048, sway: 0.024, phase: 2.4 }),
    fish(0.75, 0.53, 0.19, -1, "clown"),
    bubble(0.8, 0.31, 0.024), bubble(0.74, 0.22, 0.015), bubble(0.84, 0.17, 0.011),
    sand(-0.02, 1.0, 0.93, 5),
    coral(0.79, 0.97, 0.21, 23),
    shell(0.15, 0.985, 0.05, { tilt: -14 }),
  ],
  O: () => [
    kelp(0.12, 0.92, 0.46, { ...DEEP, w: 0.05, sway: 0.026, phase: 2 }),
    kelp(0.21, 0.97, 0.3, { ...GREEN, w: 0.046, sway: 0.02, phase: 0.2 }),
    bubble(0.15, 0.29, 0.018), bubble(0.26, 0.17, 0.012),
    fish(0.84, 0.46, 0.17, -1, "clown"),
    sand(-0.02, 1.02, 0.91, 7),
    coral(0.76, 0.95, 0.19, 37, { color: "#ff5d3f", tip: "#ffa38a" }),
    mound(0.46, 0.965, 0.1, 0.05),
  ],
  A: () => [
    bubble(0.53, 0.14, 0.018), bubble(0.47, 0.2, 0.011),
    fish(0.31, 0.68, 0.17, -1, "tang"),
    kelp(0.84, 0.97, 0.4, { ...DEEP, w: 0.05, sway: 0.024, phase: 1.2 }),
    sand(-0.02, 1.06, 0.93, 9),
    coral(0.16, 0.97, 0.19, 41),
    shell(0.92, 0.985, 0.045, { tilt: 10 }),
  ],
  L: () => [
    bubble(0.18, 0.2, 0.022), bubble(0.11, 0.3, 0.014), bubble(0.2, 0.33, 0.01),
    kelp(0.1, 0.92, 0.42, { ...GREEN, w: 0.05, sway: 0.024, phase: 3 }),
    sand(-0.02, 0.74, 0.93, 13),
    fish(0.46, 0.81, 0.19, 1, "clown"),
    coral(0.22, 0.97, 0.15, 53, { color: "#ff5d3f", tip: "#ffa38a" }),
    shell(0.64, 0.985, 0.05, { tilt: 8 }),
  ],
};

// Specular glints on the glass: a long soft streak and a short one where the light would catch each letter's upper left.
const streak = (d, w, a = 0.78) => `<path d="${d}" fill="none" stroke="#ffffff" stroke-opacity="${a}" stroke-width="${w}" stroke-linecap="round"/>`;
export const GLINTS = {
  S: () => [streak("M0.1 0.36C0.08 0.26 0.13 0.14 0.25 0.09", 0.022), streak("M0.31 0.075L0.37 0.065", 0.018, 0.65)],
  H: () => [streak("M0.055 0.5L0.055 0.17", 0.022), streak("M0.055 0.09L0.055 0.12", 0.018, 0.65), streak("M0.695 0.34L0.695 0.17", 0.02), streak("M0.695 0.09L0.695 0.12", 0.018, 0.65)],
  O: () => [streak("M0.055 0.58C0.04 0.38 0.12 0.2 0.27 0.09", 0.024), streak("M0.33 0.065L0.39 0.05", 0.018, 0.65)],
  A: () => [streak("M0.1 0.8L0.3 0.25", 0.022), streak("M0.33 0.17L0.35 0.11", 0.018, 0.65)],
  L: () => [streak("M0.055 0.62L0.055 0.17", 0.022), streak("M0.055 0.09L0.055 0.12", 0.018, 0.65)],
};
