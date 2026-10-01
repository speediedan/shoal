# Failure-mode catalog

One entry per failure mode a review has found. Add or update an entry in the same PR as the fix.
Status is `open` (detected by this review only), `guarded` (a check or refusal in code), or
`retired` (it can no longer occur). Fields: **signature** (what the reviewer sees), **detection**,
**fix**, **status**.

## FM-1: Claude liveness read from the wrong field (2026-09-29)
- **Signature:** a Claude role's posture is `active`, nothing has run for hours, and the quorum is
  silent.
- **Detection:** `claude agents --json`, where `state` is `done` while `status` is `busy`.
- **Fix:** liveness maps the turn `state`; `status` reads busy whenever any background task runs.
- **Status:** retired.

## FM-2: candidates hidden behind blocked next items (2026-09-29)
- **Signature:** an idle role with blocked next items, `next` showing no `BOARD` lines, and a queue
  reason "no further eligible items".
- **Detection:** the digest's rows for the role are non-empty.
- **Fix:** blocked items hold no slot; the default reason is checked against the candidates.
- **Status:** retired.

## FM-3: stale free-text holds (2026-09-29)
- **Signature:** idle on holds that cite an ended offline mode, a free lease, a merged branch, or a
  peer dependency its owner has declared clear.
- **Fix:** `blockers` judges blockers by kind; holds expire; idle is refused beside candidates.
- **Status:** guarded.

## FM-4: a maintainer answer only `user` can carry out (2026-09-30)
- **Signature:** an ask dropped as "answered" whose answer is "offer it to X"; the role passes the
  item with `suggest=` to a role that only takes offers, and holds its dependent item forever.
- **Detection:** the role's harness question history.
- **Fix:** the operator makes the offer. Proposed: a pass suggesting an offer-only role becomes a
  pending offer the maintainer confirms in one step.
- **Status:** open.

## FM-5: upstream PR read as resolved when its checks finish (2026-09-30)
- **Signature:** an `upstream:gh-pr:` blocker or `upstream` wait on an open, unmerged PR reads
  resolved and cannot be re-recorded.
- **Fix:** upstream semantics: an open PR is pending whatever its checks say.
- **Status:** guarded.

## FM-6: wait target blanked by a re-declaration (2026-10-01)
- **Signature:** `WAIT ... target=-` immediately after the role wrote a target; the job ended and
  nothing woke the role.
- **Detection:** the wait record's target is empty; the ledger shows a queue write with a target
  followed by a transition without one.
- **Fix:** a re-declaration without `--target` keeps the current target; with `waittargets`, a CI
  wait needs one.
- **Status:** guarded.

## FM-7: detached job waiter (2026-10-01)
- **Signature:** the job ended (rc file present, the waiter's log has its verdict), the waiter
  process is gone, and the role is still waiting.
- **Detection:** the waiter was started with `setsid`/`nohup`/`&` in a harness that does not
  report background exits.
- **Fix:** with `waittargets`, the ledger watcher wakes the role when a `job:`/`lease:` target
  resolves. The protocol says to arm waiters the session owns.
- **Status:** guarded.

## FM-8: finished items parked in next (2026-10-01)
- **Signature:** `next` lists items the role recorded done, behind holds such as "complete, merged,
  nothing further".
- **Fix:** `queue` refuses a finished item in `next`; `next` prints `GONE`.
- **Status:** guarded.

## FM-9: holds renewed into maintainer pages (2026-10-01)
- **Signature:** `hold-long` pages for holds re-recorded several times; the maintainer is paged
  about lane state the lane could have converted.
- **Fix:** `hold-long` wakes the role first (convert it to an ask or a checkable blocker) and pages
  240 minutes later.
- **Status:** guarded.

## FM-10: stale blockers hidden by a local-ci wait (2026-10-01)
- **Signature:** a role chaining job units keeps next-item holds that expired hours ago; no wake.
- **Detection:** `BLOCKER STALE` lines for a role whose posture is `waiting/local-ci`.
- **Fix:** the review tells the role to re-verify them when its job ends. Candidate: carry stale
  blockers into the role's job-end wake.
- **Status:** open.

## FM-11: orphaned ad hoc waiters (2026-10-01)
- **Signature:** a polling loop reparented to init, waiting on a path that no longer exists.
- **Detection:** `ps` for poll loops with parent pid 1.
- **Fix:** waiters go through `shoal job wait` (reapable); the operator removes orphans on
  approval.
- **Status:** open.

## FM-12: uncommitted versioned instruction edit blocks every commit (2026-10-01)
- **Signature:** lanes report their commits blocked by the instruction-sync hook, while the sync
  status reads in sync.
- **Detection:** the versioned file is modified but not committed. The hook stashes unstaged files
  and compares the committed version against already-updated homes.
- **Fix:** commit the versioned edit in the same turn as the push.
- **Status:** open (process).

## FM-13: liveness probe blind during a harness self-update (2026-10-01)
- **Signature:** a transient "native binary not installed" from the harness CLI.
- **Detection:** the probe returns nothing for one tick.
- **Fix:** none needed if the gap is short. A review must not read that silence as health.
- **Status:** open.

## Not a failure: long jobs that look like a faulty monitor (2026-10-01)
A research role's waits ran for hours. Every job's waiter woke the session 3 to 27 seconds after
the unit ended. The durations came from runners yielding to CI and from lease queueing. Check the
end-to-wake history (checks.md, Waiting, step 4) before suspecting the monitor.
