// Check the loaded depth scene, not the asynchronous loading screen. Requires a display and
// herdr-app on PATH. Run on an isolated software display when sharing a development host.
// node plugins/shoal-herdr-app/preview/check_depth.mjs [--runs 12] [--depth iso|tilt] [--yaw degrees] [--doc graph.json]
//      [--shoal <id>] [--hover <id>] [--orbit h,v]
// --shoal opens drilled into that shoal (the cutout sphere); --hover renders with that hover id (a node, an edge, or
// globe:<shoal> for the pointer on a globe), so the more opaque focused globe is checked too. --orbit is the arrows'
// orbit in degrees (h: right is positive, v: up is positive), kept to the view's range as the mod keeps it.
import { cpSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const mod = join(dirname(fileURLToPath(import.meta.url)), "..", "mods", "lattice-view");
const args = process.argv.slice(2);
const opt = (name, fallback) => { const i = args.indexOf(`--${name}`); return i < 0 ? fallback : args[i + 1]; };
const runs = Number(opt("runs", 12)), depth = opt("depth", "iso"), yaw = Number(opt("yaw", 0));
const shoal = opt("shoal", null), hover = opt("hover", null);
const orbit = String(opt("orbit", "0,0")).split(",").map(Number);
if (!Number.isInteger(runs) || runs < 1 || runs > 100 || !["iso", "tilt"].includes(depth) || !Number.isFinite(yaw)
  || orbit.length !== 2 || !orbit.every(Number.isFinite)) {
  throw new Error("use --runs 1..100, --depth iso|tilt, a finite --yaw and --orbit h,v in degrees");
}
const doc = JSON.parse(readFileSync(opt("doc", join(mod, "fixtures", "graph-v1.json")), "utf8"));
if (shoal && !(doc.shoals ?? []).some((s) => s.id === shoal)) throw new Error(`--shoal ${shoal} is not in the doc`);
const scratch = mkdtempSync(join(tmpdir(), "shoal-depth-check-"));
try {
  cpSync(mod, scratch, { recursive: true });
  renameSync(join(scratch, "main.js"), join(scratch, "lattice.js"));
  writeFileSync(join(scratch, "main.js"), `
import Lattice from "./lattice.js";
const fixture = ${JSON.stringify(doc)};
export default class extends Lattice {
  init(p, cx) {
    super.init(p, cx);
    ${shoal ? `this.homeShoal = this.view = ${JSON.stringify(shoal)}; // init reads props(), so set the home here` : ""}
    this.depth = ${JSON.stringify(depth)};
    this.yawTurn = ${yaw * Math.PI / 180};
    this.orbit = { x: ${orbit[0] * Math.PI / 180}, y: ${orbit[1] * Math.PI / 180} }; // seen() keeps it in range
    this.accept(fixture, cx);
    this.hover = ${JSON.stringify(hover)};
  }
  load(_cx) {}
  probeRenderer(_cx) { this.renderer = "cpu"; }
  render(cx) {
    const out = super.render(cx);
    // a check that fell back to another view or to flat would pass without drawing what it names
    if (!this.lay?.cam || this.view !== ${JSON.stringify(shoal ?? "global")} || (${!!shoal} && !this.lay.bowl)) {
      throw new Error("check_depth: rendered " + this.view + " " + (this.depthNow ?? "flat") + ", not the scene asked for");
    }
    return out;
  }
}
`);
  const scene = `${depth}${shoal ? ` ${shoal}` : ""}${hover ? ` hover ${hover}` : ""}`;
  for (let i = 0; i < runs; i++) {
    const result = spawnSync("herdr-app", ["check", scratch], { encoding: "utf8", timeout: 30000 });
    if (result.error || result.status !== 0) {
      process.stderr.write(result.stdout ?? "");
      process.stderr.write(result.stderr ?? "");
      throw result.error ?? new Error(`loaded ${scene} render failed on run ${i + 1}`);
    }
  }
  console.log(`Loaded ${scene} scene: ${runs}/${runs} native checks passed (yaw ${yaw}, orbit ${orbit.join(",")})`);
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
