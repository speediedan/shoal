// gen_logo_image.mjs: renders the SHOAL logo to the mod's transparent assets/logo.png: each letter (glyphs.js) a small
// glass aquarium, its water in the light blue to blue gradient, the reef from logo_reef.mjs clipped inside it, a bright
// waterline and specular highlights, on a real soft shadow. The mod draws that image, because all this is far too many
// native path calls for one render. The frame and scale are logo.js logoImageFrame, so logoImageBox can place the image
// exactly on the letters; a test reads the pixels back to check that.
//
// Rasterized with a headless browser, CPU only, in a throwaway profile, then recompressed here.
//
//   node plugins/shoal-herdr-app/preview/gen_logo_image.mjs [--chrome <binary>] [--out logo.png] [--svg logo.svg]

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readPng, writePng } from "./png.mjs";
import { GLINTS, REEFS } from "./logo_reef.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "mods", "shoal-home");
const Lo = await import(join(MOD, "logo.js"));
const Gl = await import(join(MOD, "glyphs.js"));

const args = process.argv.slice(2);
const opt = (name, d) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : d; };
const OUT = opt("out", join(MOD, "assets", Lo.LOGO_IMAGE.file));
const CHROME = opt("chrome", process.env.CHROME ?? "google-chrome");

const frame = Lo.logoImageFrame();
// a box far larger than the word, so the letters are drawn at exactly pxPerEm; their pens are then moved onto the frame
const letters = Lo.logoLetters({ x: 0, y: 0, w: 100000, h: 100000, font: frame.pxPerEm });
const capPx = Gl.CAP_EM * frame.pxPerEm;
const WATER_Y = 0.115; // the waterline, in cap units
const pathD = (cmds) => cmds.map((c) => (c[0] === "Z" ? "Z" : c[0] + c.slice(1).map((v) => Math.round(v * 10000) / 10000).join(" "))).join("");

// the waterline runs on across the word, a slight wave, so the five aquariums share one surface
function surface(ch, x0, x1) {
  const pts = [];
  for (let x = x0; x <= x1 + 1e-6; x += 0.04) pts.push(`${Math.round(x * 1000) / 1000} ${Math.round((WATER_Y + 0.016 * Math.sin(x * 7.2 + ch.charCodeAt(0))) * 10000) / 10000}`);
  return pts;
}

const defs = [
  `<linearGradient id="water" gradientUnits="userSpaceOnUse" x1="0" y1="${WATER_Y}" x2="0" y2="1"><stop offset="0" stop-color="${Lo.LOGO.top}"/><stop offset="0.3" stop-color="#6fd4f7"/><stop offset="1" stop-color="${Lo.LOGO.bottom}"/></linearGradient>`,
  `<linearGradient id="air" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${WATER_Y + 0.03}"><stop offset="0" stop-color="#eefcff"/><stop offset="1" stop-color="#c9f2fb"/></linearGradient>`,
  `<linearGradient id="sand" gradientUnits="userSpaceOnUse" x1="0" y1="0.9" x2="0" y2="1"><stop offset="0" stop-color="#f3dfb4"/><stop offset="1" stop-color="#d2ae7a"/></linearGradient>`,
  `<filter id="wall" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="0.016"/></filter>`,
  // the soft shadow, in the image's own pixels
  `<filter id="near" filterUnits="userSpaceOnUse" x="0" y="0" width="${frame.w}" height="${frame.h}"><feGaussianBlur stdDeviation="${capPx / 70}"/></filter>`,
  `<filter id="far" filterUnits="userSpaceOnUse" x="0" y="0" width="${frame.w}" height="${frame.h}"><feGaussianBlur stdDeviation="${capPx / 28}"/></filter>`,
];

const place = (l) => `translate(${frame.ox + (l.x - letters[0].x)} ${frame.oy - capPx}) scale(${capPx})`;
const outline = (ch) => Gl.GLYPHS[ch].loops.map(pathD).join("");
const shadows = [];
const backs = [];
const bodies = [];
for (const l of letters) {
  const g = Gl.GLYPHS[l.ch];
  const d = outline(l.ch);
  defs.push(`<clipPath id="in-${l.ch}"><path d="${d}" clip-rule="evenodd"/></clipPath>`);
  // the shadows lie below and to the right, each a blurred copy
  shadows.push(`<path transform="translate(${frame.ox + (l.x - letters[0].x) + capPx * 0.035} ${frame.oy - capPx + capPx * 0.05}) scale(${capPx})" d="${d}" fill-rule="evenodd" fill="#020a18"/>`);
  backs.push(`<path transform="${place(l)}" d="${d}" fill="none" stroke="#04213f" stroke-width="0.05" stroke-linejoin="round"/>`);
  const x0 = -0.1, x1 = g.width + 0.1;
  const top = surface(l.ch, x0, x1);
  bodies.push(`<g transform="${place(l)}"><g clip-path="url(#in-${l.ch})">`
    + `<rect x="${x0}" y="-0.05" width="${x1 - x0}" height="1.15" fill="url(#air)"/>`
    + `<path d="M${top.join("L")}L${x1} 1.1L${x0} 1.1Z" fill="url(#water)"/>`
    + REEFS[l.ch]().join("")
    // the waterline: a bright band with a pale line under it
    + `<path d="M${top.join("L")}" fill="none" stroke="#ffffff" stroke-width="0.022" stroke-linejoin="round"/>`
    + `<path transform="translate(0 0.024)" d="M${top.join("L")}" fill="none" stroke="#ffffff" stroke-opacity="0.35" stroke-width="0.012" stroke-linejoin="round"/>`
    // the glass wall: darker inside the edge, a highlight where the light catches it
    + `<path d="${d}" fill="none" stroke="#0a3f86" stroke-opacity="0.55" stroke-width="0.075" filter="url(#wall)"/>`
    + `<path transform="translate(0.016 0.02)" d="${d}" fill="none" stroke="#ffffff" stroke-opacity="0.62" stroke-width="0.014" stroke-linejoin="round"/>`
    + GLINTS[l.ch]().join("")
    + `</g><path d="${d}" fill="none" stroke="${Lo.LOGO.rim}" stroke-width="0.022" stroke-linejoin="round"/></g>`);
}

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${frame.w}" height="${frame.h}" viewBox="0 0 ${frame.w} ${frame.h}" style="position:absolute;left:0;top:0"><defs>${defs.join("")}</defs>`
  + `<g filter="url(#far)" opacity="0.4">${shadows.join("")}</g><g filter="url(#near)" opacity="0.45">${shadows.join("")}</g>`
  + `${backs.join("")}${bodies.join("\n")}</svg>\n`;

const dir = mkdtempSync(join(tmpdir(), "shoal-logo-"));
try {
  const svgPath = opt("svg", join(dir, "logo.svg"));
  const shot = join(dir, "shot.png");
  writeFileSync(svgPath, svg);
  execFileSync(CHROME, ["--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", `--user-data-dir=${join(dir, "profile")}`,
    `--window-size=${frame.w},${frame.h}`, "--default-background-color=00000000", `--screenshot=${shot}`, `file://${svgPath}`], { stdio: "ignore" });
  const img = readPng(readFileSync(shot));
  if (img.width !== frame.w || img.height !== frame.h) throw new Error(`the browser drew ${img.width}x${img.height}, wanted ${frame.w}x${frame.h}`);
  if (img.rgba[3] !== 0) throw new Error("the image's corner is not transparent");
  // a fully transparent pixel keeps no color, which compresses better and shows nothing
  for (let i = 0; i < img.rgba.length; i += 4) if (img.rgba[i + 3] === 0) img.rgba.fill(0, i, i + 3);
  const png = writePng(img);
  writeFileSync(OUT, png);
  console.log(`${OUT}: ${frame.w}x${frame.h}, ${png.length} bytes`);
} finally {
  rmSync(dir, { recursive: true, force: true });
}
