// shoal-status model: pure, with no Herdr imports, so it tests under Node.

const arr = (v) => (Array.isArray(v) ? v : []);
const SHORT = { "5h": "5h", week: "wk", day: "day", month: "mo" };

/** The bar's color for a used percentage: quiet until it matters. */
export function usageColor(pct) {
  return pct >= 90 ? "#ff4d5e" : pct >= 70 ? "#ffb547" : "#5cc8ff";
}

/**
 * A reading older than this (seconds) is not drawn as a value: twelve polls (subscription_usage.POLL_S), four times the
 * age at which it turns stale and is dimmed (STALE_S). A host whose cache no feed refreshes keeps its last reading for
 * days, and the bar showed those percentages while the other machines' answers were on their way (measured 2026-11-26:
 * the app's own computer held a claude reading 6 days old and a claude-2 one 8 days old).
 */
export const USAGE_MAX_AGE_S = 3600;

/**
 * `shoal ui usage --json` as one entry per provider and bucket: {name, stale, error, windows: [{name, pct, color,
 * resetsAt}]}, only the windows with a reading. An entry with neither windows nor an error says nothing and is left out.
 * An account whose reading is older than `maxAge` (its `age_s`) keeps its entries with no windows (`old`: drawn as
 * waiting, never as its old percentages), so it counts as unread and a fresher machine's answer wins.
 */
export function usageRows(doc, maxAge = USAGE_MAX_AGE_S) {
  const out = [];
  for (const a of arr(doc?.accounts)) {
    if (!a || typeof a.provider !== "string") continue;
    const rows = arr(a.rows);
    const stale = a.stale === true;
    const age = typeof a.age_s === "number" && Number.isFinite(a.age_s) ? a.age_s : null;
    const old = age !== null && age > maxAge;
    if (!rows.length && a.error) out.push({ name: a.provider, stale, error: String(a.error), windows: [] });
    for (const r of rows) {
      const windows = [];
      for (const w of arr(r?.windows)) {
        if (typeof w?.used_percent !== "number" || !Number.isFinite(w.used_percent)) continue;
        const pct = Math.max(0, Math.min(100, w.used_percent));
        const resetsAt = typeof w.resets_at === "number" && Number.isFinite(w.resets_at) ? w.resets_at : null;
        windows.push({ name: SHORT[w.name] ?? String(w.name ?? "?"), pct, color: usageColor(pct), resetsAt });
      }
      if (!windows.length) continue;
      const bucket = r?.bucket && r.bucket !== a.provider ? `/${r.bucket}` : "";
      const name = `${a.provider}${bucket}`;
      if (old) {
        out.push({ name, stale: true, old: true, windows: [],
                   error: `${name}: last read ${untilText(age)} ago${a.error ? ` (${a.error})` : ""}; waiting for a fresh reading` });
        continue;
      }
      out.push({ name, stale, error: "", windows });
    }
  }
  return out;
}

/** Seconds as its two largest units ("2h 14m", "3d 4h"); "" when unknown. */
export function untilText(s) {
  if (typeof s !== "number" || !Number.isFinite(s) || s < 0) return "";
  const out = [];
  let left = Math.round(s);
  for (const [u, n] of [["d", 86400], ["h", 3600], ["m", 60]]) {
    if (left >= n) {
      out.push(`${Math.floor(left / n)}${u}`);
      left %= n;
    }
    if (out.length === 2) break;
  }
  return out.join(" ") || "<1m";
}

/**
 * A meter's hover: each window's time to its reset (the bars already show the percentages), at `now` in unix seconds.
 * A window whose reset is not reported says so.
 */
export function usageTip(u, now) {
  if (u?.error) return String(u.error);
  const parts = arr(u?.windows).map((w) => {
    const t = w.resetsAt !== null && w.resetsAt !== undefined ? untilText(Math.max(0, w.resetsAt - now)) : "";
    return `${w.name} resets in ${t || "?"}`;
  });
  return `${u?.name ?? "?"}: ${parts.join(", ")}${u?.stale ? " (stale)" : ""}`;
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
 * How good one machine's usage rows are: [entries with a reading, of them not stale]. A host that keeps no live meter (no
 * UI feed refreshes its cache) answers with rows too, stale or `not collected yet` errors: 2026-11-18 the bar showed
 * `claude ?` while the space in view was on a machine whose cache was 22 h old.
 */
export function usageScore(rows) {
  const read = arr(rows).filter((u) => !u?.error && arr(u?.windows).length);
  return [read.length, read.filter((u) => !u.stale).length];
}

/** Every row read and fresh: nothing another machine could improve on. */
export function usageComplete(rows) {
  const [read, fresh] = usageScore(rows);
  return arr(rows).length > 0 && read === arr(rows).length && fresh === read;
}

/** The best of several machines' rows (each null for no answer): most entries read, then most fresh; the earlier on a tie. */
export function bestUsage(candidates) {
  let best = null, bs = [-1, -1];
  for (const rows of arr(candidates)) {
    if (!arr(rows).length) continue;
    const s = usageScore(rows);
    if (s[0] > bs[0] || (s[0] === bs[0] && s[1] > bs[1])) [best, bs] = [rows, s];
  }
  return best;
}

/** How long after the bar starts a pass that found no complete answer is tried again soon (USAGE_RETRY_MS): the other
 * machines' connections are still coming up, so the first pass hears only this computer. */
export const USAGE_WARM_MS = 120000;
export const USAGE_RETRY_MS = 5000;

/**
 * What the bar shows after a pass (`best`, bestUsage of the answers; `prev`, what it showed, null while it has shown no
 * reading): the best rows; else the last good ones; else, while it is warming up (`warm`), still null (drawn as
 * refreshing, never as a value), and after that nothing.
 */
export function usageShown(prev, best, warm) {
  if (best) return best;
  if (prev) return prev;
  return warm ? null : [];
}

/** Whether a pass is tried again soon: nothing complete yet, inside the warm-up (`sinceMs` since the bar started). */
export function usageRetry(best, sinceMs) {
  return !usageComplete(best) && sinceMs < USAGE_WARM_MS;
}

/**
 * Where usage may be read: the machine of the space in view, the other machines, this computer (null). Not every host
 * keeps a live usage cache, so all are asked at once (the first complete answer, usageComplete, is drawn as it lands) and
 * the best answer wins (bestUsage; on a tie the earlier in this order).
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
