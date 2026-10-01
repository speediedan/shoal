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

- `shoal show --shoal <id>`: posture, active item, next, and last event per role. Note the offline
  and freeze state, and the tooling sha.
- `shoal next --shoal <id> --slug <role> --peek` per role: `NEXT BLOCKED`, `BLOCKER STALE`, `BOARD`
  candidates, `WAIT`, `WATCH`, `GONE`, and `HELD-UNSCHEDULED` lines.
- `shoal health --spec <spec> --instance a --dry-run`: what the quorum sees right now.
- The health journal since the last review: which ladders climbed, and how many **pages** reached
  the maintainer. Pages are the maintainer-attention cost the review exists to drive down.

Delegate bulk reads (transcripts, journals, job logs) to subagents that return compact reports
with exact timestamps, pids and paths. A transcript dump in the reviewer's context is budget spent
on raw text instead of judgment.

## 2. Prove each posture

Check each role against its posture's checklist in `references/checks.md`. In short:

- **active:** the session is mid-turn (Claude `state: working`, opencode busy) AND making
  progress (recent tool calls, a ledger write, or a running job). A session `done` while its posture
  says `active` ended its turn without a sleep posture.
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

A message is not a fix. Verify each intervention by the role's next queue write (posture, active
item, blockers), and report the effect, not the send.

## 4. Feed it back (the recursive part)

For every INVALID role, decide where the defect lives, and fix it there in the same cycle:

| the defect is in | fix |
| --- | --- |
| the lane's behavior under a correct protocol | shoal-protocol wording, at the point the lane reads it (`next` output, the skill reference) |
| a shoal mechanism (a check that is missing, wrong or bypassable) | a shoal PR that reproduces the measured shape in a hermetic test that fails before the fix; new behavior behind a spec feature |
| this review (it missed something, or flagged a false positive) | this skill: a new or corrected check in `references/checks.md` |

Then add or update the entry in `references/failure-modes.md` (signature, detection, fix, status)
**in the same PR**. That catalog is what makes the next review faster than this one.

A check graduates as it proves itself: from this skill's checklist, to a deterministic report the
CLI prints, to a health condition with a ladder. Each step moves work from the reviewer's tokens
into code. Prefer proposing the next graduation over repeating a manual check a third time.

## 5. Report

One table per review: role, posture, verdict, evidence, action taken, effect observed. Follow it
with:
- the failure modes found (catalog ids, new ones marked);
- the shoal or skill changes proposed or opened;
- the pages and wakes the quorum spent since the last review.

Record the review in the operator workstream's log, and leave nothing in the reviewer's memory
alone.
