---
name: shoal-health-review
version: 0.1.0
description: >-
  A periodic, read-first health review of a running SHOAL group by its operator (or an agent acting
  for them): prove that every active role is working, every waiting role has a watcher that will
  actually wake it, and every idle role is idle for a reason that is true now. Then restart stalled
  roles through shoal channels, and feed each failure mode back into shoal and into this review.
  Load it for a scheduled review, after an upgrade, when a page repeats, or when asked whether a
  group's roles are really working.
---

# SHOAL health review

The health quorum is token-free and checks what code can check. This review covers the rest: the
judgment a timer cannot make, such as whether a hold is true, whether a waiter will fire, or whether
an idle role's queue is really exhausted. Its second job matters as much: every failure it finds
should leave shoal, or this review, better at preventing or catching it next time.

Load `shoal-operator` too, since its steering rules apply. Read your host map for the group id,
spec path, data root and harness stores. **Never write a role's queue.** Run `next` only with
`--peek`.

## 1. Snapshot (five minutes, all read-only)

- `shoal show --shoal <id>`: status, active item, next, and last event per role. Note the offline
  and freeze state, and the tooling sha.
- `shoal next --shoal <id> --slug <role> --peek` per role: `NEXT BLOCKED`, `BLOCKER STALE`, `BOARD`
  candidates, `WAIT`, `WATCH`, `GONE`, and `HELD-UNSCHEDULED` lines.
- `shoal health --spec <spec> --instance a --dry-run`: what the quorum sees right now.
- The health journal since the last review: which ladders climbed, and how many **pages** reached
  the maintainer. Pages are the maintainer-attention cost the review exists to drive down.

Delegate bulk reads (transcripts, journals, job logs) to subagents that return compact reports
with exact timestamps, pids and paths. A transcript dump in the reviewer's context is budget spent
on raw text instead of judgment.

## 2. Prove each status

Check each role against its status's checklist in `references/checks.md`. In short:

- **active:** the session is mid-turn (Claude `state: working`, opencode busy) AND making
  progress (recent tool calls, a ledger write, or a running job). A session `done` while its status
  says `active` ended its turn without a sleep status.
- **waiting:** the target is checkable and still pending, AND a watcher owned by the session (or
  the ledger watcher's backstop) will fire when it ends. A long job is not a faulty monitor: compare
  each job's end time with the role's next ledger write before concluding anything.
- **idle:** every next-item blocker is true NOW, against live state. A blocker shaped like the
  maintainer has a recorded ask. A dependency is in checkable form. No finished item sits in
  `next`. The board offers nothing that fits the role's brief and that it has not passed.

Classify each role **VALID** or **INVALID**, and give the evidence and the precise failure mode.
Look the failure mode up in `references/failure-modes.md` before calling it new.

## 3. Restart stalled roles, then verify the effect

Use shoal channels only:
- `shoal msg --by user --to <role>` is mail, read at the role's boundary;
- add `--wake` when the role must act now;
- `shoal offer` gives a role specific work.

Each message names:
- the evidence (what is stale, and since when);
- the one action to take;
- the protocol rule the role missed, so it does not repeat it.

A message is not a fix. Verify each intervention by the role's next queue write (status, active
item, blockers), and report the effect, not the send.

## 4. Feed it back (the recursive part)

For every INVALID role, decide where the defect lives, and fix it there in the same cycle:

| the defect is in | fix |
| --- | --- |
| the lane's behavior under a correct protocol | shoal-protocol wording, at the point the lane reads it (`next` output, the skill reference) |
| a shoal mechanism (a check that is missing, wrong or bypassable) | a shoal PR that reproduces the measured shape in a hermetic test that fails before the fix, preferring the simplifications below; new behavior behind a spec feature with a retirement condition |
| this review (it missed something, or flagged a false positive) | this skill: a new or corrected check in `references/checks.md` |

Then add or update the entry in `references/failure-modes.md` (signature, detection, metric, fix,
status) **in the same PR**. That catalog is what makes the next review faster than this one.

### Prefer fewer primitives to more flags

Each failure invites a new flag, a new health condition or a new wait substate. Resist that. Every
one added is another thing a lane can misuse and another thing the next review must check. Before
proposing a fix, try these in order:

1. **Remove or unify a primitive.** Two ways to write the same state (a `queue` wait and a separate
   `transition`) produced a blanked target 117 times in 8 days. Can the state have one writer and
   one shape?
2. **Make the invalid state unwritable.** A refusal at write time, or carrying a value forward,
   removes the state. A detector only notices it after the cost is paid.
3. **Let the system resolve what it can check.** A wait on a checkable target needs no rescan timer,
   validity wake or holder text. Those exist for waits nobody can check, and in one measured week
   they were most of the quorum's wakes.
4. **Only then add a detector or a flag.** Give any new feature flag a retirement condition, such
   as "folded into the default after two clean weeks, or removed". Track the flag count.

Measure the design itself. The number of flags, health conditions, wait substates and blocker kinds
should stay level or fall. If each review adds one, the design is accreting, not converging. Say
so in the report, and propose the simplification rather than the next patch.

A check graduates as it proves itself: from this skill's checklist, to a deterministic report the
CLI prints, to a health condition with a ladder. Each step moves work from the reviewer's tokens
into code. Prefer proposing the next graduation over repeating a manual check a third time.

## 5. Measure (optional, at least weekly)

Quantify every failure mode in the catalog, so that the question "is shoal getting more reliable"
has a numeric answer.

1. Run `scripts/fm_report.py --spec <spec> --data-root <data root> --days 7`. It prints per-day
   counts and hours for each FM id, plus wakes and pages by condition, with a trend column (the later
   half of the window against the earlier half).
2. Compare with the previous review's numbers, which the operator log keeps.
3. A failure mode found without a metric gets one in the same cycle. Prefer a derivation from
   streams shoal already writes; add a new event only when nothing existing can carry it.
   `references/metrics.md` maps each FM to its metric and lists the known gaps.
4. **The escalation rule.** Expect the frequency of known failure modes, and the total pages and
   wakes per active lane-hour, to fall from one review to the next as their fixes land.
   - A fixed failure mode whose metric does not fall means the fix missed.
   - A total that does not fall over two or three reviews, while new failure modes keep appearing,
     means patching is not converging. That calls for an architectural or approach change, not
     another fix. Write it up as such, with the numbers, in the report.

## 6. Report

One table per review: role, status, verdict, evidence, action taken, effect observed. Follow it
with:
- the failure modes found (catalog ids, new ones marked);
- the shoal or skill changes proposed or opened;
- the pages and wakes the quorum spent since the last review;
- with step 5, the FM table, the trend per failure mode, and the design-size count (flags,
  conditions, substates).

Record the review in the operator workstream's log, and leave nothing in the reviewer's memory
alone.
