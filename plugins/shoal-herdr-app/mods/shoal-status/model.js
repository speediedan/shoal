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

/**
 * Where usage may be read, in order: the machine of the space in view, the other machines, this computer (null). Not every
 * host keeps the usage cache, and one without it answers with no accounts, so the first that has rows wins.
 */
export function usageMachines(spaces, machineNames) {
  const active = arr(spaces).find((s) => s?.active)?.machine;
  const names = arr(machineNames).filter((n) => typeof n === "string" && n);
  return [...new Set([...(active ? [active] : []), ...names, null])];
}

// What a blocked card wants, as its report names it: the lattice pulse's order (auth, then asks, then grants).
const KIND_RANK = { auth: 0, question: 1, permission: 2 };

/**
 * The agents that need you, in the pulse's order: by what they want (`kindOf(agent)`, the card's report kind), then the
 * one unchanged longest first (`seq` rises with every change), so the key reaches the oldest question first.
 */
export function needsYouOrder(list, kindOf = () => null) {
  const rank = (a) => KIND_RANK[kindOf(a)] ?? 3;
  return arr(list).filter((a) => a?.status === "blocked" && a.pane)
    .sort((x, y) => rank(x) - rank(y) || (x.seq ?? 0) - (y.seq ?? 0) || (x.address < y.address ? -1 : 1));
}

/** The needs-you agent after `current` (an address) in that order, wrapping; the first when `current` is not one. */
export function nextNeedsYou(list, kindOf, current) {
  const order = needsYouOrder(list, kindOf);
  if (!order.length) return null;
  const i = order.findIndex((a) => a.address === current);
  return order[(i + 1) % order.length];
}

/**
 * Where the Lattice button goes without a process: the shown space's own `lattice` tab, else null (the CLI then reopens
 * one, or goes to the global one in the home space). {space, tab} ids.
 */
export function latticeTab(spaces) {
  const cur = arr(spaces).find((s) => s?.active);
  const t = arr(cur?.tabs).find((x) => x?.name === "lattice");
  return cur && t ? { space: cur.id, tab: t.id } : null;
}

/** Where the Home button goes without a process: this computer's home space and its `home` tab (else its first). */
export function homeTab(spaces, name = "SHOAL-home", tab = "home") {
  const sp = arr(spaces).find((s) => s?.name === name && !s?.machine);
  if (!sp) return null;
  const t = arr(sp.tabs).find((x) => x?.name === tab) ?? (tab === "home" ? arr(sp.tabs)[0] : null);
  return t ? { space: sp.id, tab: t.id } : null;
}

/**
 * The shoal of the space in view: a card terminal in it labelled `<shoal id>/<role>` (`shoal.pane`, the space from its
 * `herdr.space` label, prefixed with the machine for a remote one). Null for a space with no shoal card.
 */
export function shoalInView(spaces, terminals) {
  const cur = arr(spaces).find((s) => s?.active);
  if (!cur) return null;
  for (const t of arr(terminals)) {
    const labels = t?.labels ?? {};
    const local = labels["herdr.space"];
    const addr = String(t?.address ?? "");
    const at = addr.lastIndexOf("/");
    const id = at > 0 && local ? `${addr.slice(0, at)}/${local}` : local;
    if (id !== cur.id && local !== cur.id) continue;
    const pane = String(labels["shoal.pane"] ?? "");
    const cut = pane.lastIndexOf("/");
    if (cut > 0 && !pane.startsWith("standalone/")) return pane.slice(0, cut);
  }
  return null;
}
