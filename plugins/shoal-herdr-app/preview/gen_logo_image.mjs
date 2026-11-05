// gen_logo_image.mjs: renders the SHOAL logo's vector geometry (logo.js logoScene) to the mod's transparent
// assets/logo.png. The mod draws that image, because the logo never changes and its full outlines are too many
// native path calls for one render. The frame and scale are logo.js logoImageFrame, so logoImageBox can place the
// image exactly on the letters; a test reads the pixels back to check that.
//
// Rasterized with a headless browser, CPU only, in a throwaway profile, then recompressed here.
//
//   node plugins/shoal-herdr-app/preview/gen_logo_image.mjs [--chrome <binary>] [--out logo.png] [--svg logo.svg]
//
// Run it after gen_logo_glyphs.mjs, whenever the letters change.

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readPng, writePng } from "./png.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "mods", "shoal-home");
const Lo = await import(join(MOD, "logo.js"));

const args = process.argv.slice(2);
const opt = (name, d) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : d; };
const OUT = opt("out", join(MOD, "assets", Lo.LOGO_IMAGE.file));
const CHROME = opt("chrome", process.env.CHROME ?? "google-chrome");

const frame = Lo.logoImageFrame();
// a box far larger than the word, so the letters are drawn at exactly pxPerEm; then the first letter's pen and the
// baseline are moved onto the frame's origin
const scene = Lo.logoScene({ x: 0, y: 0, w: 100000, h: 100000, font: frame.pxPerEm });
const dx = frame.ox - scene.letters[0].x, dy = frame.oy - scene.baseline;
const pathD = (cmds) => cmds.map((c) => (c[0] === "Z" ? "Z" : c[0] + c.slice(1).join(" "))).join("");

const font = scene.font;
const defs = [
  // the shadows are soft here, as paths in the app could not be
  `<filter id="near" filterUnits="userSpaceOnUse" x="0" y="0" width="${frame.w}" height="${frame.h}"><feGaussianBlur stdDeviation="${font / 90}"/></filter>`,
  `<filter id="far" filterUnits="userSpaceOnUse" x="0" y="0" width="${frame.w}" height="${frame.h}"><feGaussianBlur stdDeviation="${font / 42}"/></filter>`,
];
const body = [];
let shadows = 0;
for (const it of scene.items) {
  if (it.kind === "logo-shadow") {
    // the group's translate would move the filter region too, so the shadows carry their own
    body.push(`<g filter="url(#${shadows++ ? "near" : "far"})"><path transform="translate(${dx} ${dy})" d="${pathD(it.cmds)}" fill="${it.color}" fill-opacity="${it.alpha}"/></g>`);
  } else if (it.gradient) {
    defs.push(`<linearGradient id="body" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${it.gradient.from}"/><stop offset="1" stop-color="${it.gradient.to}"/></linearGradient>`);
    body.push(`<path transform="translate(${it.box.x + dx} ${it.box.y + dy})" d="${pathD(it.cmds)}" fill="url(#body)"/>`);
  } else if (it.fill) {
    body.push(`<path transform="translate(${dx} ${dy})" d="${pathD(it.cmds)}" fill="${it.color}" fill-opacity="${it.alpha}"/>`);
  } else {
    body.push(`<path transform="translate(${dx} ${dy})" d="${pathD(it.cmds)}" fill="none" stroke="${it.color}" stroke-opacity="${it.alpha}" stroke-width="${it.width}" stroke-linejoin="round"/>`);
  }
}
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${frame.w}" height="${frame.h}" viewBox="0 0 ${frame.w} ${frame.h}" style="position:absolute;left:0;top:0"><defs>${defs.join("")}</defs>${body.join("\n")}</svg>\n`;

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
