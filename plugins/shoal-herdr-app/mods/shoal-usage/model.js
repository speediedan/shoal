// shoal-usage model: pure, with no Herdr imports, so it tests under Node.
//
// The dashboard reads `shoal ui usage --json` (cached subscription meters; `--refresh` reads the providers' account APIs,
// never a model turn). Each window gets its pace: how far through the window we are against how much of it is used, so a
// meter at 40% one hour into five reads as running out before the reset. An optional `breakdown` in the same document
// ({by_shoal, by_role, standalone}: [{name, share | tokens, provider?}]) fills the decomposition panels; until shoal
// collects per-session accounting those panels say so.

const arr = (v) => (Array.isArray(v) ? v : []);
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v) => (typeof v === "string" ? v : v == null ? "" : String(v));

export const WINDOW_LABEL = { "5h": "5-hour", week: "Weekly", day: "Daily", month: "Monthly" };
export const BREAKDOWNS = [
  { key: "by_shoal", title: "By shoal" },
  { key: "by_role", title: "By role" },
  { key: "standalone", title: "Standalone seats" },
];
const MIN_ELAPSED = 0.05; // under 5% into a window, a pace says nothing yet

/** "2h 14m", "3d 4h", "45s": the largest two units. */
export function duration(s) {
  const v = num(s);
  if (v === null || v < 0) return "";
  const units = [["d", 86400], ["h", 3600], ["m", 60], ["s", 1]];
  const out = [];
  let left = Math.round(v);
  for (const [u, n] of units) {
    if (left >= n || (u === "s" && !out.length)) {
      out.push(`${Math.floor(left / n)}${u}`);
      left %= n;
    }
    if (out.length === 2) break;
  }
  return out.join(" ");
}

/** ok | warn | high from the used share and where it is headed. */
export function severity(pct, projected) {
  if (pct >= 90) return "high";
  if (pct >= 70 || (projected !== null && projected >= 100)) return "warn";
  return "ok";
}

/**
 * One window's reading at `now` (unix seconds): used percent, time to reset, how far into the window it is, the percent
 * it reaches at the reset at the current pace, and when it would run out first.
 */
export function windowView(w, now) {
  const pct = Math.max(0, Math.min(100, num(w?.used_percent) ?? 0));
  const resetsAt = num(w?.resets_at);
  const minutes = num(w?.minutes);
  const resetIn = resetsAt !== null ? Math.max(0, resetsAt - now) : null;
  let elapsed = null;
  if (resetIn !== null && minutes) elapsed = Math.max(0, Math.min(1, 1 - resetIn / (minutes * 60)));
  const projected = elapsed !== null && elapsed >= MIN_ELAPSED ? pct / elapsed : null;
  let runsOutIn = null;
  if (projected !== null && projected > 100 && pct > 0 && pct < 100) {
    const rate = pct / (elapsed * minutes * 60); // percent a second
    runsOutIn = (100 - pct) / rate;
  }
  const name = str(w?.name);
  return {
    key: name, label: WINDOW_LABEL[name] ?? name, pct, resetIn, elapsed, projected, runsOutIn,
    severity: severity(pct, projected),
  };
}

/** What a window's sub-line says: the reset, then the pace. */
export function windowNote(v) {
  const bits = [];
  if (v.resetIn !== null) bits.push(`resets in ${duration(v.resetIn)}`);
  if (v.pct >= 100) bits.push("used up");
  else if (v.runsOutIn !== null) bits.push(`runs out in ${duration(v.runsOutIn)} at this pace`);
  else if (v.projected !== null) bits.push(`on pace for ${Math.round(v.projected)}%`);
  return bits.join(" · ");
}

/** The dashboard's cards: one per provider account and bucket, with an error card for an account that has no reading. */
export function cards(doc, now) {
  const out = [];
  for (const a of arr(doc?.accounts)) {
    if (!a || typeof a.provider !== "string") continue;
    const base = { provider: a.provider, stale: a.stale === true, source: str(a.source), ageS: num(a.age_s),
                   error: str(a.error) };
    const rows = arr(a.rows);
    if (!rows.length) {
      out.push({ ...base, id: a.provider, title: a.provider, plan: "", blocked: false, windows: [],
                 error: base.error || "no reading yet" });
      continue;
    }
    for (const r of rows) {
      const bucket = str(r?.bucket);
      const windows = arr(r?.windows).filter((w) => num(w?.used_percent) !== null).map((w) => windowView(w, now));
      out.push({ ...base, id: `${a.provider}/${bucket || "-"}`, title: bucket && bucket !== a.provider ? `${a.provider} · ${bucket}` : a.provider,
                 plan: str(r?.plan), blocked: r?.blocked === true, windows, error: windows.length ? base.error : base.error || "no meters" });
    }
  }
  return out;
}

const RANK = { high: 2, warn: 1, ok: 0 };

/** The meter under the most pressure: the headline. Null when nothing has a reading. */
export function tightest(cardsList) {
  let best = null;
  for (const c of arr(cardsList)) {
    for (const w of arr(c.windows)) {
      const score = RANK[w.severity] * 1000 + Math.max(w.pct, Math.min(w.projected ?? 0, 150) * 0.6);
      if (!best || score > best.score) best = { score, card: c, window: w };
    }
  }
  return best && { card: best.card, window: best.window };
}

/** A decomposition panel's rows, largest first, as shares that sum to 1; null when the document carries none. */
export function breakdownRows(doc, key) {
  const rows = doc?.breakdown?.[key];
  if (!Array.isArray(rows)) return null;
  const vals = rows.map((r) => ({ name: str(r?.name) || "?", provider: str(r?.provider),
                                  v: num(r?.share) ?? num(r?.tokens) ?? 0 })).filter((r) => r.v > 0);
  const total = vals.reduce((s, r) => s + r.v, 0);
  if (!total) return [];
  return vals.map((r) => ({ ...r, share: r.v / total })).sort((a, b) => b.share - a.share);
}

/** The newest reading's age across accounts, for the header. */
export function updatedAge(doc) {
  const ages = arr(doc?.accounts).map((a) => num(a?.age_s)).filter((v) => v !== null);
  return ages.length ? Math.min(...ages) : null;
}

/** The CLI's reply as a document, or null for a failed or unreadable one (the last good one then stays). */
export function parseReply(reply) {
  if (!reply || reply.code !== 0) return null;
  try {
    const doc = JSON.parse(reply.stdout);
    return doc && typeof doc === "object" && Array.isArray(doc.accounts) ? doc : null;
  } catch (_e) {
    return null;
  }
}

/** The argv that reads the meters; `refresh` reads the account APIs now. */
export function usageArgv(source, refresh = false) {
  const base = Array.isArray(source?.argv) && source.argv.length ? source.argv.map(String) : ["shoal", "ui", "usage", "--json"];
  return refresh && !base.includes("--refresh") ? [...base, "--refresh"] : base;
}
