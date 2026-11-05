// shoal-status model: pure, with no Herdr imports, so it tests under Node.

const arr = (v) => (Array.isArray(v) ? v : []);
const SHORT = { "5h": "5h", week: "wk", day: "day", month: "mo" };

/** The bar's color for a used percentage: quiet until it matters. */
export function usageColor(pct) {
  return pct >= 90 ? "#ff4d5e" : pct >= 70 ? "#ffb547" : "#5cc8ff";
}

/**
 * `shoal ui usage --json` as one entry per provider and bucket: {name, stale, error, windows: [{name, pct, color}]}, only
 * the windows with a reading. An entry with neither windows nor an error says nothing and is left out.
 */
export function usageRows(doc) {
  const out = [];
  for (const a of arr(doc?.accounts)) {
    if (!a || typeof a.provider !== "string") continue;
    const rows = arr(a.rows);
    const stale = a.stale === true;
    if (!rows.length && a.error) out.push({ name: a.provider, stale, error: String(a.error), windows: [] });
    for (const r of rows) {
      const windows = [];
      for (const w of arr(r?.windows)) {
        if (typeof w?.used_percent !== "number" || !Number.isFinite(w.used_percent)) continue;
        const pct = Math.max(0, Math.min(100, w.used_percent));
        windows.push({ name: SHORT[w.name] ?? String(w.name ?? "?"), pct, color: usageColor(pct) });
      }
      if (!windows.length) continue;
      const bucket = r?.bucket && r.bucket !== a.provider ? `/${r.bucket}` : "";
      out.push({ name: `${a.provider}${bucket}`, stale, error: "", windows });
    }
  }
  return out;
}

/** Reads the CLI's reply, keeping nothing from a failed or unreadable one: the bar then shows the last good rows. */
export function parseUsage(reply) {
  if (!reply || reply.code !== 0) return null;
  try {
    return usageRows(JSON.parse(reply.stdout));
  } catch (_e) {
    return null;
  }
}

/** Where usage is read: the machine of the space in view, else the first machine, else this computer. */
export function usageMachine(spaces, machineNames) {
  const active = arr(spaces).find((s) => s?.active);
  if (active?.machine) return active.machine;
  return arr(machineNames).find((n) => typeof n === "string" && n) ?? null;
}
