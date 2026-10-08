# Failure-mode catalog

One entry per failure mode a review has found. Add or update an entry in the same PR as the fix.
Status is `open` (detected by this review only), `guarded` (a check or refusal in code), or
`retired` (it can no longer occur). Fields: **signature** (what the reviewer sees), **detection**,
**metric** (references/metrics.md; a failure mode without one gets one), **fix**, **status**.

## FM-1: Claude liveness read from the wrong field (2026-09-29)
- **Signature:** a Claude role's posture is `active`, nothing has run for hours, and the quorum is
  silent.
- **Detection:** `claude agents --json`, where `state` is `done` while `status` is `busy`.
- **Fix:** liveness maps the turn `state`; `status` reads busy whenever any background task runs.
- **Metric:** `FM1:active_silent_h`, `wake:turn-ended-active`
- **Status:** retired.

## FM-2: candidates hidden behind blocked next items (2026-09-29)
- **Signature:** an idle role with blocked next items, `next` showing no `BOARD` lines, and a queue
  reason "no further eligible items".
- **Detection:** the digest's rows for the role are non-empty.
- **Fix:** blocked items hold no slot; the default reason is checked against the candidates.
- **Metric:** `FM2:idle_all_next_blocked_h` (an overstatement until candidate counts are recorded)
- **Status:** retired.

## FM-3: stale free-text holds (2026-09-29)
- **Signature:** idle on holds that cite an ended offline mode, a free lease, a merged branch, or a
  peer dependency its owner has declared clear.
- **Fix:** `blockers` judges blockers by kind; holds expire; idle is refused beside candidates.
- **Metric:** `FM3:idle_on_hold_gt4h_h`
- **Status:** guarded.

## FM-4: a maintainer answer only `user` can carry out (2026-09-30)
- **Signature:** an ask dropped as "answered" whose answer is "offer it to X"; the role passes the
  item with `suggest=` to a role that only takes offers, and holds its dependent item forever.
- **Detection:** the role's harness question history.
- **Fix:** the operator makes the offer. Proposed: a pass suggesting an offer-only role becomes a
  pending offer the maintainer confirms in one step.
- **Metric:** none yet (see metrics.md, gaps)
- **Status:** open.

## FM-5: upstream PR read as resolved when its checks finish (2026-09-30)
- **Signature:** an `upstream:gh-pr:` blocker or `upstream` wait on an open, unmerged PR reads
  resolved and cannot be re-recorded.
- **Fix:** upstream semantics: an open PR is pending whatever its checks say.
- **Metric:** `wake:wait-resolved` (an upper bound)
- **Status:** guarded.

## FM-6: wait target blanked by a re-declaration (2026-10-01)
- **Signature:** `WAIT ... target=-` immediately after the role wrote a target; the job ended and
  nothing woke the role.
- **Detection:** the wait record's target is empty; the ledger shows a queue write with a target
  followed by a transition without one.
- **Fix:** a re-declaration without `--target` keeps the current target; with `waittargets`, a CI
  wait needs one.
- **Metric:** `FM6:target_blanked`
- **Status:** guarded.

## FM-7: detached job waiter (2026-10-01)
- **Signature:** the job ended (rc file present, the waiter's log has its verdict), the waiter
  process is gone, and the role is still waiting.
- **Detection:** the waiter was started with `setsid`/`nohup`/`&` in a harness that does not
  report background exits.
- **Fix:** with `waittargets`, the ledger watcher wakes the role when a `job:`/`lease:` target
  resolves. The protocol says to arm waiters the session owns.
- **Metric:** `FM7:no_verdict_and_gap`
- **Status:** guarded.

## FM-8: finished items parked in next (2026-10-01)
- **Signature:** `next` lists items the role recorded done, behind holds such as "complete, merged,
  nothing further".
- **Fix:** `queue` refuses a finished item in `next`; `next` prints `GONE`.
- **Metric:** `FM8:done_items_in_next`, `FM8:done_in_next_h`
- **Status:** guarded.

## FM-9: holds renewed into maintainer pages (2026-10-01)
- **Signature:** `hold-long` pages for holds re-recorded several times; the maintainer is paged
  about lane state the lane could have converted.
- **Fix:** `hold-long` wakes the role first (convert it to an ask or a checkable blocker) and pages
  240 minutes later.
- **Metric:** `FM9:hold_renewals`, `page:hold-long`
- **Status:** guarded.

## FM-10: stale blockers hidden by a local-ci wait (2026-10-01)
- **Signature:** a role chaining job units keeps next-item holds that expired hours ago; no wake.
- **Detection:** `BLOCKER STALE` lines for a role whose posture is `waiting/local-ci`.
- **Fix:** the review tells the role to re-verify them when its job ends. Candidate: carry stale
  blockers into the role's job-end wake.
- **Metric:** `FM10:localci_with_stale_hold_h`
- **Status:** open.

## FM-11: orphaned ad hoc waiters (2026-10-01)
- **Signature:** a polling loop reparented to init, waiting on a path that no longer exists.
- **Detection:** `ps` for poll loops with parent pid 1. The first review found two: a log poller on a
  retired path (removed on approval), and a log-tail loop about 11 days old.
- **Fix:** waiters go through `shoal job wait` (reapable); the operator removes orphans on
  approval.
- **Metric:** none (a snapshot check)
- **Status:** open.

## FM-12: uncommitted versioned instruction edit blocks every commit (2026-10-01)
- **Signature:** lanes report their commits blocked by the instruction-sync hook, while the sync
  status reads in sync.
- **Detection:** the versioned file is modified but not committed. The hook stashes unstaged files
  and compares the committed version against already-updated homes.
- **Fix:** commit the versioned edit in the same turn as the push.
- **Metric:** none (a snapshot check)
- **Status:** open (process).

## FM-13: liveness probe blind during a harness self-update (2026-10-01)
- **Signature:** a transient "native binary not installed" from the harness CLI.
- **Detection:** the probe returns nothing for one tick.
- **Fix:** none needed if the gap is short. A review must not read that silence as health.
- **Metric:** none (a probe-error event is proposed)
- **Status:** open.

## FM-14: background service restart reaps every Claude worker (2026-10-02)
- **Signature:** every Claude role and the wake relay stop together. `claude agents --json` stops
  listing them, and `--all` lists them `stopped` (detail "ended while the background service was
  off") with no `pid`. relay-dead pages, and every watcher-dead wake reports `no-adapter`.
- **Detection:** `~/.claude/daemon.log` holds `upgrade self-respawn failed to spawn` followed by
  `adopt: ... dead=N`; each job's `exit-cause` reads `ptyhost_orphan_watchdog`. A milder cousin:
  the service retires a session idle at its prompt for 8 h (`bg retire <id>: idle-prompt`).
- **Measured:** 2026-10-02T21:27Z, 5 workers reaped, the relay among them. Six days of relay-dead
  pages and 18 pathless wakes followed until the maintainer re-attached the sessions by hand.
- **Fix:** `features.health.revive` (shoal). The quorum relaunches a dead relay before paging,
  ahead of the role wakes it will carry, and respawns a role whose worker is gone
  (`claude respawn`, conversation kept) before queueing its wake for the relay. No new condition:
  the existing watcher-dead and relay-dead ladders gain the repair step. To keep a role down, park
  it or mark it `offline`; with revive on, a role that merely has a dead session is revived.
  Retire the flag (fold it into the default) after two clean weeks on a shoal that runs it.
- **Metric:** `FM14:wakes_no_path` (the failure), `FM14:respawns` and `FM14:relay_relaunches`
  (the repair firing).
- **Status:** fixed in shoal. Left off in the shoal that hit it, by decision (a role there is held down
  by a dead session); on for the next shoal.

## Not a failure: long jobs that look like a faulty monitor (2026-10-01)
A research role's waits ran for hours. Every job's waiter woke the session 3 to 27 seconds after
the unit ended. The durations came from runners yielding to CI and from lease queueing. Check the
end-to-wake history (checks.md, Waiting, step 4) before suspecting the monitor.
