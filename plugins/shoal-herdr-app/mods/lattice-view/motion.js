// motion.js: the motion budget. Pure: no Herdr or GPUI imports.
//
// Why tiers look like this: while any animated element is drawn, the Herdr app redraws the whole
// window natively, and the cost is per window frame, not per element. Measured on a software
// renderer (lavapipe, 2 threads, a 1280x820 window): about 0.9 core-seconds per frame whatever
// the lattice draws, so one 700 ms blink costs more than a core and a 30 fps loop saturates the
// budget. On a GPU the same frames are cheap. So the tiers differ in how many frames they ask for:
//   full      loops (about 30 fps): breathing halos, rings, ripples, spinners, dots at their own
//             speed, packets
//   balanced  no loops: stepped glyph pulses on needs-you and working nodes and dots on the
//             busiest edges, all on one shared frame interval (220 ms), packets on
//   low       nothing animates by default (needs-you stays loud statically: ring, badge, rail
//             order); `budget.frame_ms` opts back into a slow needs-you blink. Data renders are
//             also spaced out (render_ms) unless what needs you changed.
//   off       nothing animates; data renders spaced further; state stays legible statically
// "auto" starts at balanced, goes to low on a CPU (software) renderer and full on a GPU, and
// drops one tier for the pulse drawer, a small pane, or a window without focus.

export const TIERS = ["off", "low", "balanced", "full"];
export const MOTION_CYCLE = ["auto", "full", "balanced", "low", "off"];

const BASE = {
  full: { tier: "full", loops: true, pulseScope: "all", pulseMode: "loop", maxPulses: 64, maxDots: 240, topEdges: Infinity, frameMs: 0, packets: true, ripples: true, spinners: true, renderMs: 0 },
  balanced: { tier: "balanced", loops: false, pulseScope: "needs+working", pulseMode: "frames", maxPulses: 16, maxDots: 72, topEdges: 6, frameMs: 220, packets: true, ripples: false, spinners: false, renderMs: 0 },
  low: { tier: "low", loops: false, pulseScope: "none", pulseMode: null, maxPulses: 0, maxDots: 0, topEdges: 0, frameMs: 0, packets: false, ripples: false, spinners: false, renderMs: 15000 },
  off: { tier: "off", loops: false, pulseScope: "none", pulseMode: null, maxPulses: 0, maxDots: 0, topEdges: 0, frameMs: 0, packets: false, ripples: false, spinners: false, renderMs: 30000 },
};

const int = (v) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? Math.floor(v) : null);

/**
 * The policy for a tier, with `budget` overrides: {max_dots, max_pulses, frame_ms, packets,
 * render_ms}. In low, frame_ms turns on a needs-you blink at that interval (at least 500 ms).
 */
export function motionPolicy(tier, budget = null) {
  const p = { ...(BASE[tier] ?? BASE.balanced) };
  const b = budget && typeof budget === "object" ? budget : {};
  if (int(b.max_dots) !== null) { p.maxDots = int(b.max_dots); if (p.topEdges === 0 && p.maxDots > 0) p.topEdges = 6; }
  if (int(b.max_pulses) !== null) p.maxPulses = int(b.max_pulses);
  if (int(b.frame_ms) !== null && p.tier !== "full" && p.tier !== "off") {
    p.frameMs = Math.max(p.tier === "low" ? 500 : 100, int(b.frame_ms));
    if (p.tier === "low") { p.pulseMode = "frames"; p.pulseScope = "needs"; if (int(b.max_pulses) === null) p.maxPulses = 8; }
  }
  if (typeof b.packets === "boolean") p.packets = b.packets;
  if (int(b.render_ms) !== null) p.renderMs = int(b.render_ms);
  if (p.maxDots > 0 && !p.frameMs && p.tier !== "full") p.frameMs = 220;
  if (p.tier === "off") { p.maxDots = 0; p.maxPulses = 0; p.packets = false; p.pulseMode = null; p.pulseScope = "none"; }
  p.any = p.loops || p.pulseMode !== null || p.maxDots > 0 || p.packets || p.ripples || p.spinners;
  return p;
}

const drop = (tier, n = 1) => TIERS[Math.max(0, TIERS.indexOf(tier) - n)];

/**
 * The tier to draw at. `requested`: a tier name or "auto". Context: {renderer: "cpu" | "gpu" |
 * null, pulse, small, focused}. An explicit tier is honored as given; only "auto" adapts.
 */
export function resolveTier(requested, ctx = {}) {
  if (TIERS.includes(requested)) return { tier: requested, auto: false, why: "set" };
  let tier = ctx.renderer === "cpu" ? "low" : ctx.renderer === "gpu" ? "full" : "balanced";
  const why = [ctx.renderer ? `${ctx.renderer} renderer` : "renderer unknown"];
  if (ctx.pulse || ctx.small) { tier = drop(tier); why.push(ctx.pulse ? "pulse" : "small pane"); }
  if (ctx.focused === false) { tier = drop(tier); why.push("window not focused"); }
  return { tier, auto: true, why: why.join(", ") };
}

const SOFTWARE = /(lvp_icd|lavapipe|llvmpipe|softpipe|swiftshader|libvulkan_lvp)/i;
const HARDWARE = /(nvidia|radeon|amd_icd|intel_icd|intel_hasvk|nouveau|freedreno|panfrost|asahi|MoltenVK)/i;

/**
 * A renderer hint from the app process's environment (NUL or newline separated, as
 * /proc/self/environ) and, optionally, its loaded libraries (/proc/self/maps). "cpu" when a
 * software Vulkan or GL driver is selected or loaded, "gpu" when a hardware one is, else null.
 * SHOAL_LATTICE_RENDERER=cpu|gpu in the app's environment overrides both.
 */
export function rendererFromProcess(environ = "", maps = "") {
  const env = String(environ ?? "");
  const vars = new Map(env.split(/[\0\n]/).filter(Boolean).map((kv) => { const i = kv.indexOf("="); return [kv.slice(0, i), kv.slice(i + 1)]; }));
  if (vars.get("SHOAL_LATTICE_RENDERER") === "cpu" || vars.get("LIBGL_ALWAYS_SOFTWARE") === "1") return "cpu";
  if (vars.get("SHOAL_LATTICE_RENDERER") === "gpu") return "gpu";
  const icd = [vars.get("VK_ICD_FILENAMES"), vars.get("VK_DRIVER_FILES"), vars.get("GALLIUM_DRIVER")].filter(Boolean).join(":");
  if (SOFTWARE.test(icd)) return "cpu";
  if (HARDWARE.test(icd)) return "gpu";
  const m = String(maps ?? "");
  if (/libvulkan_lvp|swiftshader|libvk_swiftshader/i.test(m)) return "cpu";
  if (/libnvidia-|libvulkan_radeon|libvulkan_intel|MoltenVK/i.test(m)) return "gpu";
  return null;
}

/** The edges that may carry marching dots under a policy: busiest first, at most topEdges. */
export function flowEdges(edges, policy) {
  if (!policy.maxDots || !policy.topEdges) return new Set();
  const flow = edges.filter((e) => e.flow).sort((a, b) => b.rate - a.rate || (a.id < b.id ? -1 : 1));
  return new Set(flow.slice(0, policy.topEdges).map((e) => e.id));
}

/**
 * Should a data poll redraw? Redraw when what is shown changed, at once if what needs you
 * changed, else no sooner than `renderMs` after the last redraw (low and off space them out).
 */
export function shouldRender({ sig, urgentSig, lastSig, lastUrgentSig, sinceMs, renderMs }) {
  if (sig === lastSig) return false;
  if (urgentSig !== lastUrgentSig) return true;
  return !renderMs || sinceMs >= renderMs;
}
