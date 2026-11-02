// render_svg.mjs: draws the shoal-lattice scene to SVG under Node, for review where the Herdr app
// cannot run. It runs the mod's own model, layout and scene modules on a graph doc (the bundled
// fixture by default), so colors, layers and shapes match the mod; motion shows as one frame.
//
//   node plugins/shoal-herdr-app/preview/render_svg.mjs [--doc graph.json] [--view global|<shoal>]
//        [--pin <node id>] [--legend] [--width 1400] [--height 860] [--out lattice.svg]

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "mods", "shoal-lattice");
const M = await import(join(MOD, "model.js"));
const L = await import(join(MOD, "layout.js"));
const G = await import(join(MOD, "geom.js"));
const S = await import(join(MOD, "scene.js"));

const args = process.argv.slice(2);
const opt = (name, d) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : d; };
const flag = (name) => args.includes(`--${name}`);
const W = Number(opt("width", 1400));
const H = Number(opt("height", 860));
const RAIL = 272;
const doc = JSON.parse(readFileSync(opt("doc", join(MOD, "fixtures", "graph-v1.json")), "utf8"));
const vm = M.buildModel(doc, opt("view", "global"));
const lay = L.layoutGraph(vm);
const CW = W - RAIL;
const pts = [...lay.pos.values()].map((p) => ({ ...p, r: 30 }));
for (const s of lay.sats) pts.push({ x: s.x, y: s.y, r: 8 });
for (const c of lay.clusters) pts.push({ x: c.x, y: c.y, r: c.r + 26 });
for (const a of lay.anchors.values()) pts.push({ x: a.x, y: a.y, r: 40 });
const tf = G.fitTransform(G.boundsOf(pts), { width: CW, height: H }, 30, 1.6);
const pin = opt("pin", null);
const sc = S.buildScene(vm, lay, { width: CW, height: H, tf, selected: pin, motion: "full" });
const P = M.PALETTE;

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const r2 = (v) => Math.round(v * 100) / 100;
const defs = [];
let gid = 0;
function paint(bg) {
  if (!bg || bg.kind === "solid") return bg?.color ?? "#000";
  const id = `g${gid++}`;
  if (bg.kind === "linear") {
    // GPUI angles: 0 points up, clockwise. SVG gradient vector from the opposite edge.
    const a = ((bg.angle - 90) * Math.PI) / 180;
    const x = Math.cos(a) / 2, y = Math.sin(a) / 2;
    defs.push(`<linearGradient id="${id}" x1="${r2(0.5 - x)}" y1="${r2(0.5 - y)}" x2="${r2(0.5 + x)}" y2="${r2(0.5 + y)}"><stop offset="0" stop-color="${bg.from}"/><stop offset="1" stop-color="${bg.to}"/></linearGradient>`);
  } else if (bg.kind === "slash") {
    defs.push(`<pattern id="${id}" width="${bg.interval}" height="${bg.interval}" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="${bg.interval}" stroke="${bg.color}" stroke-width="${bg.width}"/></pattern>`);
  }
  return `url(#${id})`;
}
const font = `font-family="Geist Mono, JetBrains Mono, DejaVu Sans Mono, monospace"`;
function text(x, y, w, h, s, size, color, align = "left", weight = null) {
  const ax = align === "center" ? x + w / 2 : x;
  return `<text x="${r2(ax)}" y="${r2(y + h / 2)}" ${font} font-size="${size}" fill="${color}" dominant-baseline="central" text-anchor="${align === "center" ? "middle" : "start"}"${weight ? ` font-weight="${weight === "bold" ? 700 : 600}"` : ""}>${esc(s)}</text>`;
}

const body = [];
for (const it of sc.items) {
  if (it.t === "path") {
    const d = G.pathD(it.cmds);
    if (it.fill) body.push(`<path d="${d}" fill="${paint(it.bg)}" fill-rule="nonzero"/>`);
    else body.push(`<path d="${d}" fill="none" stroke="${paint(it.bg)}" stroke-width="${it.width}" stroke-linecap="round"${it.dash ? ` stroke-dasharray="${it.dash.join(" ")}"` : ""}/>`);
  } else if (it.t === "box") {
    const loopOp = it.loops?.find((l) => l.prop === "opacity");
    const op = loopOp ? (loopOp.from + loopOp.to) / 2 + 0.1 : it.opacity ?? 1;
    const rx = Math.min(it.radius ?? 0, it.w / 2, it.h / 2);
    body.push(`<rect x="${r2(it.x)}" y="${r2(it.y)}" width="${r2(it.w)}" height="${r2(it.h)}" rx="${r2(rx)}" fill="${it.bg ?? "none"}"${it.border ? ` stroke="${it.border.color}" stroke-width="${it.border.w}"` : ""} opacity="${r2(Math.min(1, op))}"/>`);
  } else if (it.t === "text") {
    body.push(text(it.x, it.y, it.w, it.h, it.frames ? it.frames.list[0] : it.text, it.size, it.color, it.align, it.weight));
  } else if (it.t === "spin") {
    const c = it.size / 2, rr = c - 1;
    body.push(`<path d="M${r2(it.x + c)} ${r2(it.y + 1)}A${r2(rr)} ${r2(rr)} 0 0 1 ${r2(it.x + it.size - 1)} ${r2(it.y + c)}" fill="none" stroke="${it.color}" stroke-width="1.6" stroke-linecap="round"/>`);
  }
}

// header
const hd = S.headerText(vm);
body.push(text(16, 12, 300, 14, `SHOAL LATTICE  › ${hd.where}`, 11, P.ink, "left", "semibold"));
body.push(text(16, 28, 600, 12, `${hd.counts} · l: legend`, 9.5, P.dim));

// pinned card
if (pin && sc.nodeScreen.has(pin)) {
  const c = M.cardLines(vm, pin);
  const at = sc.nodeScreen.get(pin);
  const lines = [...c.rows.map(([k, v]) => [k, v]), ...c.list.map((l) => ["", l]), ...c.links.map((l) => ["", l.text])];
  const ch = 34 + lines.length * 16 + (c.command ? 20 : 0);
  let x = at.x + at.r + 16;
  if (x + 288 > CW - 8) x = at.x - at.r - 304;
  const y = Math.max(8, Math.min(at.y - 20, H - ch - 8));
  body.push(`<rect x="${r2(x)}" y="${r2(y)}" width="288" height="${ch}" rx="8" fill="${G.withAlpha(P.panelSolid, 0.95)}" stroke="${G.withAlpha(c.color ?? P.ink, 0.7)}"/>`);
  body.push(`<circle cx="${r2(x + 14)}" cy="${r2(y + 17)}" r="4.5" fill="${c.color ?? P.ink}"/>`);
  body.push(text(x + 24, y + 10, 200, 14, `${c.title}  ${c.subtitle ?? ""}`, 12, P.ink, "left", "semibold"));
  lines.forEach(([k, v], i) => {
    if (k) body.push(text(x + 10, y + 30 + i * 16, 100, 14, k, 10.5, P.dim));
    body.push(text(x + (k ? 116 : 10), y + 30 + i * 16, 170, 14, v, 10.5, k ? P.ink : P.teal));
  });
  if (c.command) body.push(text(x + 10, y + ch - 22, 270, 14, `$ ${c.command}`, 10, P.steel2));
}

// legend
if (flag("legend")) {
  const lg = M.legendEntries(vm);
  const lh = 30 + lg.states.length * 16;
  const lx = 16, ly = H - lh - 16;
  body.push(`<rect x="${lx}" y="${ly}" width="420" height="${lh}" rx="8" fill="${G.withAlpha(P.panelSolid, 0.94)}" stroke="${G.withAlpha(P.steel1, 0.6)}"/>`);
  body.push(text(lx + 10, ly + 8, 200, 12, "LEGEND (l to close)", 9.5, P.steel2, "left", "semibold"));
  lg.states.forEach((s, i) => {
    body.push(`<circle cx="${lx + 16}" cy="${ly + 30 + i * 16}" r="4" fill="${s.style === "outline" || s.style === "dashed" || s.style === "dotted" ? "none" : s.color}" stroke="${s.color}"/>`);
    body.push(text(lx + 26, ly + 24 + i * 16, 14, 12, s.badge ?? "", 9.5, s.color));
    body.push(text(lx + 44, ly + 24 + i * 16, 200, 12, s.label, 9.5, s.needsYou ? P.ink : P.dim));
  });
  lg.edges.forEach((e, i) => {
    body.push(`<line x1="${lx + 250}" y1="${ly + 30 + i * 16}" x2="${lx + 274}" y2="${ly + 30 + i * 16}" stroke="${e.color}" stroke-width="1.6"${e.dash ? ` stroke-dasharray="${e.dash.join(" ")}"` : ""}/>`);
    body.push(text(lx + 282, ly + 24 + i * 16, 120, 12, e.cls, 9.5, P.dim));
  });
  lg.hosts.forEach((h, i) => {
    const yy = ly + 30 + (lg.edges.length + 1 + i) * 16;
    body.push(`<circle cx="${lx + 262}" cy="${yy}" r="3.5" fill="${h.color}"/>`);
    body.push(text(lx + 282, yy - 6, 120, 12, h.name, 9.5, P.dim));
  });
}

// rail
const rail = [];
let ry = 16;
const line = (s, size, color, weight, x = 12) => { rail.push(text(CW + x, ry, RAIL - 24, size + 4, s, size, color, "left", weight)); ry += size + 8; };
rail.push(`<rect x="${CW}" y="0" width="${RAIL}" height="${H}" fill="${P.panelSolid}"/><line x1="${CW}" y1="0" x2="${CW}" y2="${H}" stroke="${P.steel0}"/>`);
line(vm.global ? "all shoals" : M.shortShoal(vm.view), 12, P.ink, "semibold");
line(`${doc.host ?? ""} · ${doc.ts ?? ""}`, 10, P.dim);
ry += 6;
line(`NEEDS YOU  ${vm.needsYou.length}`, 9.5, P.steel2, "semibold");
for (const r of vm.needsYou) {
  rail.push(`<rect x="${CW + 12}" y="${ry - 3}" width="${RAIL - 24}" height="34" rx="5" fill="${G.withAlpha(r.color, r.rank === 0 ? 0.16 : 0.08)}"/>`);
  rail.push(`<circle cx="${CW + 26}" cy="${ry + 14}" r="8" fill="${r.color}"/>`);
  rail.push(text(CW + 18, ry + 8, 16, 12, r.badge ?? "!", 9, "#0b0b10", "center", "bold"));
  rail.push(text(CW + 40, ry, 200, 14, `${r.label}  ${vm.global && r.shoal ? M.shortShoal(r.shoal) : ""}`, 10.5, P.ink, "left", "semibold"));
  rail.push(text(CW + 40, ry + 15, 210, 12, `${r.text} · ${M.fmtAge(r.since_s)}`.slice(0, 34), 9.5, P.dim));
  ry += 40;
}
ry += 6;
line("SHOALS", 9.5, P.steel2, "semibold");
for (const s of vm.shoalsInView) {
  rail.push(`<rect x="${CW + 12}" y="${ry - 4}" width="${RAIL - 24}" height="70" rx="6" fill="${G.withAlpha(s.tint, 0.06)}" stroke="${G.withAlpha(s.tint, 0.22)}"/>`);
  rail.push(`<circle cx="${CW + 26}" cy="${ry + 8}" r="4" fill="${s.urgentColor}"/>`);
  rail.push(text(CW + 36, ry + 1, 120, 14, `${s.short}  ${s.gen}`, 10.5, s.tint, "left", "semibold"));
  if (s.needsYou) rail.push(`<rect x="${CW + RAIL - 40}" y="${ry + 1}" width="16" height="14" rx="7" fill="${P.magenta}"/>` + text(CW + RAIL - 40, ry + 1, 16, 14, s.needsYou, 9.5, "#0b0b10", "center", "bold"));
  rail.push(text(CW + 22, ry + 20, 230, 13, `done ${s.done24}/24h ${s.done7}/7d   ${s.msgsPerH} msg/h`, 10, P.dim));
  rail.push(`<rect x="${CW + 22}" y="${ry + 42}" width="80" height="4" rx="2" fill="${G.withAlpha(P.steel1, 0.6)}"/><rect x="${CW + 22}" y="${ry + 42}" width="${Math.round(80 * (s.workShare ?? 0))}" height="4" rx="2" fill="${P.teal}"/>`);
  rail.push(text(CW + 110, ry + 37, 120, 13, s.workShare == null ? "work ?" : `work ${Math.round(s.workShare * 100)}%`, 10, P.dim));
  ry += 80;
}
line("HOSTS", 9.5, P.steel2, "semibold");
for (const h of M.hostSummaries(vm)) {
  rail.push(`<circle cx="${CW + 22}" cy="${ry + 7}" r="4" fill="${h.color}"/>`);
  rail.push(text(CW + 32, ry, 140, 14, `${h.name}${h.home ? " ⌂" : ""}`, 10.5, h.color, "left", "semibold"));
  rail.push(text(CW + 190, ry, 70, 14, `${h.roles} roles`, 10, P.dim));
  ry += 16;
  rail.push(text(CW + 32, ry, 230, 13, `work ${h.working} · wait ${h.waiting} · ${h.msgsPerH}/h${h.jobsIn ? ` · CI in ${h.jobsIn}` : ""}`, 10, P.dim));
  ry += 22;
}

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><defs>${defs.join("")}</defs><rect width="${W}" height="${H}" fill="${P.bgBottom}"/><g>${body.join("\n")}</g><g>${rail.join("\n")}</g></svg>\n`;
const out = opt("out", join(tmpdir(), `shoal-lattice-${vm.view}.svg`));
writeFileSync(out, svg);
console.log(out);
