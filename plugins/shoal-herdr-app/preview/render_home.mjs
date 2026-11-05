// render_home.mjs: draws the shoal-home splash to SVG under Node, for review where the Herdr app cannot run. It
// runs the mod's own layout, model, sea and logo modules on the bundled fixture, in the same paint order as
// main.js, so the geometry and colors match the mod; text uses whatever fonts the host has, and motion shows as
// one frame. A harness render, not an app screenshot.
//
//   node plugins/shoal-herdr-app/preview/render_home.mjs [--width 1100] [--height 760] [--art false] [--first-render]
//        [--out home.svg]
//
// --first-render draws the fallback logo, as the render before the assets are checked does; --art false the text screen.
//
// Its creature images are referenced by file path, so open the SVG from this machine to rasterize it.

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "mods", "shoal-home");
const M = await import(join(MOD, "model.js"));
const La = await import(join(MOD, "layout.js"));
const O = await import(join(MOD, "ocean.js"));
const Lo = await import(join(MOD, "logo.js"));

const args = process.argv.slice(2);
const opt = (name, d) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : d; };
const W = Number(opt("width", 1100));
const H = Number(opt("height", 760));
const P = M.PALETTE;
const list = M.parseList(readFileSync(join(MOD, "fixtures", "launch-list.json"), "utf8"));
const lines = list.hosts.map((h) => M.cardLines(h));
const L = La.homeLayout({ width: W, height: H }, lines.map((l) => La.cardHeight(l.length)), { art: opt("art", "true") !== "false" });

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const r2 = (v) => Math.round(v * 100) / 100;
const pathD = (cmds) => cmds.map((c) => (c[0] === "Z" ? "Z" : c[0] === "A" ? `A${c[1]} ${c[2]} ${c[3]} ${c[4] ? 1 : 0} ${c[5] ? 1 : 0} ${c[6]} ${c[7]}` : c[0] + c.slice(1).join(" "))).join("");
const defs = [];
let gid = 0;
// GPUI angles: 0 points up, clockwise, across the path's bounds
function gradient(angle, from, to) {
  const id = `g${gid++}`;
  const a = ((angle - 90) * Math.PI) / 180;
  const x = Math.cos(a) / 2, y = Math.sin(a) / 2;
  defs.push(`<linearGradient id="${id}" x1="${r2(0.5 - x)}" y1="${r2(0.5 - y)}" x2="${r2(0.5 + x)}" y2="${r2(0.5 + y)}"><stop offset="0" stop-color="${from}"/><stop offset="1" stop-color="${to}"/></linearGradient>`);
  return `url(#${id})`;
}
const body = [];
const FONT = `font-family="Geist, Inter, Noto Sans, DejaVu Sans, sans-serif"`;
function text(x, y, h, s, size, color, anchor = "start", weight = null, extra = "") {
  body.push(`<text x="${r2(x)}" y="${r2(y + h / 2)}" ${FONT} font-size="${size}" fill="${color}" dominant-baseline="central" text-anchor="${anchor}"${weight ? ` font-weight="${weight}"` : ""}${extra}>${esc(s)}</text>`);
}
function item(it) {
  // one frame of a sway is its rest position: a layer of main.js's seaEl, or the logo body in its own box
  if (it.gradient) {
    const g = gradient(it.gradient.angle, it.gradient.from, it.gradient.to);
    body.push(`<path transform="translate(${it.box.x} ${it.box.y})" d="${pathD(it.cmds)}" fill="${g}" fill-rule="nonzero"/>`);
  } else if (it.fill) {
    body.push(`<path d="${pathD(it.cmds)}" fill="${it.color}" fill-opacity="${it.alpha}" fill-rule="nonzero"/>`);
  } else {
    body.push(`<path d="${pathD(it.cmds)}" fill="none" stroke="${it.color}" stroke-opacity="${it.alpha}" stroke-width="${it.width}" stroke-linecap="round" stroke-linejoin="round"/>`);
  }
}

body.push(`<rect width="${W}" height="${H}" fill="${gradient(165, P.bgTop, P.bgBottom)}"/>`);
body.push(`<rect width="${W}" height="${H}" fill="${gradient(35, P.navy, P.navy)}" fill-opacity="0.35"/>`);
const lg = L.logo;
for (let k = 0; k < 6; k++) {
  const rx = lg.w * (1.25 - k * 0.15), ry = lg.h * (1.5 - k * 0.18);
  body.push(`<ellipse cx="${lg.x + lg.w / 2}" cy="${lg.y + lg.h / 2}" rx="${r2(rx)}" ry="${r2(ry)}" fill="${k < 3 ? P.blue : P.cyan}" fill-opacity="0.022"/>`);
}
const sea = O.oceanScene(L);
for (const it of sea?.items ?? []) item(it);
for (const c of L.creatures) body.push(`<image href="file://${join(MOD, "assets", c.file)}" x="${c.x}" y="${c.y}" width="${c.size}" height="${c.size}"/>`);
const logo = Lo.logoDraw(lg, !args.includes("--first-render"));
if (logo.image) body.push(`<image href="file://${join(MOD, "assets", logo.image.file)}" x="${logo.image.x}" y="${logo.image.y}" width="${logo.image.w}" height="${logo.image.h}" preserveAspectRatio="none"/>`);
for (const it of logo.items) item(it);
for (const it of sea?.front ?? []) item(it);

if (L.tagline) {
  const words = ["Stigmergic", "Heterogeneous", "Open", "Agent", "Lattice"];
  const spans = words.map((w) => `<tspan font-weight="600" fill="${P.amber}" text-decoration="underline">${w[0]}</tspan>${w.slice(1)}`).join(`<tspan fill-opacity="0.45">  ·  </tspan>`);
  body.push(`<text x="${W / 2}" y="${L.tagline.y + L.tagline.h / 2}" ${FONT} font-size="${L.tagline.size}" fill="${P.amber}" fill-opacity="0.86" dominant-baseline="central" text-anchor="middle">${spans}</text>`);
}
text(W / 2, L.ask.y, L.ask.h, "which host serves the view?", L.ask.size, P.ink, "middle", 600);
const DOTS = M.DOT;
list.hosts.forEach((h, i) => {
  const b = L.cards[i];
  if (!b) return;
  const selected = h.name === list.last;
  if (selected) body.push(`<rect x="${b.x - 5}" y="${b.y - 5}" width="${b.w + 10}" height="${b.h + 10}" rx="14" fill="${P.cyan}" fill-opacity="0.05" stroke="${P.cyan}" stroke-opacity="0.6" stroke-width="2"/>`);
  const op = M.canOpen(h) ? 1 : 0.5;
  body.push(`<g opacity="${op}"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="10" fill="${P.panelSolid}" fill-opacity="0.92" stroke="${selected ? P.ink : P.steel1}" stroke-opacity="0.75"/>`);
  const hy = b.y + La.CARD.pad;
  body.push(`<circle cx="${b.x + La.CARD.pad + 7}" cy="${hy + La.CARD.head / 2}" r="5" fill="${DOTS[M.hostState(h)]}"/>`);
  text(b.x + La.CARD.pad + 22, hy, La.CARD.head, h.name, 14.5, P.ink, "start", 600);
  const ny = M.hostNeedsYou(h);
  if (ny > 0) {
    body.push(`<rect x="${b.x + b.w - 112}" y="${hy + 4}" width="76" height="16" rx="8" fill="${P.magenta}"/>`);
    text(b.x + b.w - 74, hy + 4, 16, `${ny} need you`, 10, "#0b0b10", "middle", 700);
  }
  text(b.x + b.w - La.CARD.pad - 8, hy, La.CARD.head, String(i + 1), 11, selected ? P.ink : P.faint);
  lines[i].forEach((ln, j) => {
    const ly = hy + La.CARD.head + 4 + j * La.CARD.line;
    const lh = La.CARD.line;
    const x = b.x + La.CARD.pad;
    if (ln.kind === "shoal") {
      body.push(`<circle cx="${x + 3}" cy="${ly + lh / 2}" r="3" fill="${ln.tint}"/>`);
      const need = ln.needsYou > 0 ? `<tspan font-weight="700" fill="${P.magenta}">${ln.needsYou} need you  </tspan>` : "";
      body.push(`<text x="${x + 12}" y="${ly + lh / 2}" ${FONT} font-size="11" fill="${P.dim}" dominant-baseline="central" xml:space="preserve"><tspan font-weight="600" fill="${ln.tint}">${esc(ln.tag)}  </tspan>${need}${esc(M.shoalCounts(ln))}</text>`);
    } else {
      const color = ln.kind === "pin" ? P.amber : ln.kind === "note" ? (ln.tone === "down" ? P.red : P.faint) : P.dim;
      text(x, ly, lh, ln.text.slice(0, 48), ln.kind === "sub" ? 10.5 : 11, color);
    }
  });
  body.push("</g>");
});
text(W / 2, L.foot.y, L.foot.h, "click a host to open · ↑↓ or j k move · 1-9 jump · Enter open · r refresh", 10.5, P.faint, "middle");
const rb = L.refresh;
body.push(`<rect x="${rb.x}" y="${rb.y}" width="${rb.w}" height="${rb.h}" rx="6" fill="${P.steel0}" fill-opacity="0.5" stroke="${P.steel1}" stroke-opacity="0.6"/>`);
text(rb.x + rb.w / 2, rb.y, rb.h, "↻ refresh", 11, P.dim, "middle");

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="position:absolute;left:0;top:0"><defs>${defs.join("")}</defs>${body.join("\n")}</svg>\n`;
const out = opt("out", join(tmpdir(), "shoal-home.svg"));
writeFileSync(out, svg);
console.log(out);
