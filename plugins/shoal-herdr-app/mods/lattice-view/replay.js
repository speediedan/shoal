// replay.js: the lattice's 24 h replay, pure (no Herdr imports, so it tests under Node). The feed's `--replay` section
// holds the day's events by `ts`; the view plays them back as packets on the live picture, a day in REPLAY_DAY_S.

export const REPLAY_BINS = 96; // a quarter hour each over a day
export const REPLAY_DAY_S = 96; // seconds of playback for a whole day: a quarter hour a second
export const REPLAY_TICK_MS = 500; // each tick redraws the picture: twice a second is enough for a day in REPLAY_DAY_S
export const REPLAY_BURST = 12; // packets one tick may start; a busier quarter hour shows its count, not a flood

const epoch = (ts) => {
  const t = Date.parse(ts);
  return Number.isFinite(t) ? t / 1000 : null;
};

/**
 * A doc's replay section as {from, to, span, events (each with `t`, epoch seconds, in order), bins (counts per slot),
 * max, dropped}, or null when it has none. `from` and `to` are epoch seconds.
 */
export function replayModel(doc) {
  const r = doc?.replay;
  if (!r || typeof r !== "object") return null;
  const from = epoch(r.from), to = epoch(r.to);
  if (from === null || to === null || to <= from) return null;
  const events = (Array.isArray(r.events) ? r.events : [])
    .map((e) => (e && typeof e === "object" ? { ...e, t: epoch(e.ts) } : null))
    .filter((e) => e && e.t !== null && e.t >= from && e.t <= to)
    .sort((a, b) => a.t - b.t || (a.seq ?? 0) - (b.seq ?? 0));
  const span = to - from;
  const bins = new Array(REPLAY_BINS).fill(0);
  for (const e of events) bins[Math.min(REPLAY_BINS - 1, Math.floor(((e.t - from) / span) * REPLAY_BINS))] += 1;
  return { from, to, span, events, bins, max: Math.max(1, ...bins), dropped: Number(r.dropped) || 0 };
}

/** The events with t0 < t <= t1, at most REPLAY_BURST of them (the latest), and how many there were. */
export function replayStep(rm, t0, t1) {
  const all = rm.events.filter((e) => e.t > t0 && e.t <= t1);
  return { events: all.slice(-REPLAY_BURST), n: all.length };
}

/** Replay seconds one tick advances: a whole day in REPLAY_DAY_S. */
export function replayAdvance(rm) {
  return (rm.span / REPLAY_DAY_S) * (REPLAY_TICK_MS / 1000);
}

/** The time a slot starts at, and the slot a time falls in. */
export function binStart(rm, i) {
  return rm.from + (Math.max(0, Math.min(REPLAY_BINS - 1, i)) / REPLAY_BINS) * rm.span;
}
export function binOf(rm, t) {
  return Math.max(0, Math.min(REPLAY_BINS - 1, Math.floor(((t - rm.from) / rm.span) * REPLAY_BINS)));
}

/** `HH:MMZ` for a replay time. */
export function clock(t) {
  const d = new Date(t * 1000);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}Z`;
}

/** A replay section from a doc's own events (the bundled fixture has no feed to ask): the day up to its `ts`. */
export function replayOfDoc(doc) {
  const to = epoch(doc?.ts);
  if (to === null) return null;
  return { from: new Date((to - 86400) * 1000).toISOString(), to: doc.ts, events: Array.isArray(doc.events) ? doc.events : [] };
}
