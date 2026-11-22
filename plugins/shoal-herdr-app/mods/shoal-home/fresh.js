// fresh.js: how live a feed's status is, read from the feed's own freshness block (`fresh`, written by
// `ui_status.freshness` in Python: `as_of`, `stale_after_s`, `stale`, `reasons`). The feed decides; a view only applies
// the one reader rule with its own clock, so a doc kept or re-read after its writer stopped greys too.
// Pure: no Herdr or GPUI imports, so it runs under Node. shoal-home/fresh.js is a verbatim copy: edit here, then copy.

/** The reasons a feed names, as the views say them (ui_status.STALE_REASONS, shorter). */
export const STALE_TEXT = {
  old: "not written since",
  unread: "its host's live state is not read yet",
  "host-old": "its host did not answer the last read",
  starting: "its card is reading the status",
};

/** "40 s", "3 min", "2 h", "4 d". */
export function ageWord(s) {
  if (typeof s !== "number" || !Number.isFinite(s)) return "?";
  if (s < 90) return `${Math.max(0, Math.round(s))} s`;
  if (s < 3570) return `${Math.round(s / 60)} min`;
  if (s < 129600) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

/**
 * The doc's freshness now: {stale, old, ageS, limit, reasons}, or null when the doc carries no `fresh` block (an older
 * feed: not judged, shown as before). Stale when the feed said so (some status not read yet), or when `as_of` is older
 * than the feed's `stale_after_s` by `nowMs` (its writer stopped: a host asleep, a feed unit down).
 */
export function freshness(fresh, nowMs = Date.now()) {
  if (!fresh || typeof fresh !== "object" || Array.isArray(fresh)) return null;
  const t = Date.parse(String(fresh.as_of ?? ""));
  const ageS = Number.isFinite(t) ? Math.max(0, Math.round((nowMs - t) / 1000)) : null;
  const limit = typeof fresh.stale_after_s === "number" && fresh.stale_after_s > 0 ? fresh.stale_after_s : 90;
  const reasons = (Array.isArray(fresh.reasons) ? fresh.reasons : []).map(String).filter(Boolean);
  const old = ageS !== null && ageS > limit;
  if (old && !reasons.includes("old")) reasons.unshift("old");
  return { stale: fresh.stale === true || old, old, ageS, limit, reasons };
}

/**
 * One line for a stale doc ("" when it is live): its age when its writer stopped, else how many roles are not read yet
 * and why. `n`: the roles the doc marks stale (`state.stale`).
 */
export function staleLine(f, n = 0) {
  if (!f || !f.stale) return "";
  if (f.old) return `status ${ageWord(f.ageS)} old: ${STALE_TEXT.old}`;
  const why = f.reasons.filter((r) => r !== "old").map((r) => STALE_TEXT[r] ?? r);
  const who = n > 0 ? `${n} role${n === 1 ? "" : "s"}: ` : "";
  return `${who}${why.join("; ") || "status not live yet"}`;
}

/** A short signature of a freshness, for a view's redraw check (the age only in coarse steps). */
export const freshSignature = (f) => (f ? `${f.stale ? 1 : 0}${f.old ? `o${ageWord(f.ageS)}` : ""}:${f.reasons.join(",")}` : "-");
