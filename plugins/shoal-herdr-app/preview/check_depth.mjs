// Check the loaded depth scene, not the asynchronous loading screen. Requires a display and
// herdr-app on PATH. Run on an isolated software display when sharing a development host.
// node plugins/shoal-herdr-app/preview/check_depth.mjs [--runs 12] [--depth iso|tilt] [--yaw degrees] [--doc graph.json]
import { cpSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const mod = join(dirname(fileURLToPath(import.meta.url)), "..", "mods", "lattice-view");
const args = process.argv.slice(2);
const opt = (name, fallback) => { const i = args.indexOf(`--${name}`); return i < 0 ? fallback : args[i + 1]; };
const runs = Number(opt("runs", 12)), depth = opt("depth", "iso"), yaw = Number(opt("yaw", 0));
if (!Number.isInteger(runs) || runs < 1 || runs > 100 || !["iso", "tilt"].includes(depth) || !Number.isFinite(yaw)) {
  throw new Error("use --runs 1..100, --depth iso|tilt and a finite --yaw in degrees");
}
const doc = JSON.parse(readFileSync(opt("doc", join(mod, "fixtures", "graph-v1.json")), "utf8"));
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
    this.depth = ${JSON.stringify(depth)};
    this.yawTurn = ${yaw * Math.PI / 180};
    this.accept(fixture, cx);
  }
  load(_cx) {}
  probeRenderer(_cx) { this.renderer = "cpu"; }
}
`);
  for (let i = 0; i < runs; i++) {
    const result = spawnSync("herdr-app", ["check", scratch], { encoding: "utf8", timeout: 30000 });
    if (result.error || result.status !== 0) {
      process.stderr.write(result.stdout ?? "");
      process.stderr.write(result.stderr ?? "");
      throw result.error ?? new Error(`loaded ${depth} render failed on run ${i + 1}`);
    }
  }
  console.log(`Loaded ${depth} scene: ${runs}/${runs} native checks passed (yaw ${yaw})`);
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
