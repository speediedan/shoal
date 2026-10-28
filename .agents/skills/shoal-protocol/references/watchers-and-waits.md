# Watchers and waits

## Your ledger watcher

Your brief gives the exact `shoal ledger-watch` command. Arm it in the way your harness supports:
- Claude Code in a shoal with a wake relay: a detached watcher (`--wake relay`, started once with
  `setsid nohup ... &`, not as a background command). The relay delivers each wake to you as a
  message between your turns, and the watcher keeps running: no re-arm after a wake, and no time limit;
- Claude Code without a relay: a background command that re-invokes you when it exits (the Bash tool
  with `run_in_background` and `timeout` 7200000, the 2 h maximum, never the Monitor tool, which
  expires). Every kill costs you a turn; a kill is a wake: re-arm;
- a detached process that sends its wake to your session id (opencode). It sends only between your
  turns and keeps running after each wake, so it needs no re-arm; arming it again while it runs prints
  `ALREADY ARMED` and changes nothing.

**Rules:**
- **Armed means proven.** Its pid is alive (`ps -p`) and matches the sidecar. Re-arm after every
  wake and prove it in the same turn. A lane that acted on a wake and never re-armed is deaf.
- **Prove a pid with two separate commands.** First read it: `cat /tmp/<id>/ledger-watch-<slug>.state`
  (the `pid=` line) or `cat /tmp/<id>/relay-watch.pid`. Then run `ps -p <that number>`. Never use
  `$(...)` command substitution (`ps -p $(cat ...)`): it always needs a permission prompt in a
  harness with manual permissions, so the proof stalls on the maintainer.
- **An exit is not a kill.** `ledger-watch` exits by design on every wake it delivers, and logs a
  verdict line when it does. Tally kills only from notifications with status `killed`
  (`shoal kill-audit`).
- **Re-arm after a tool upgrade.** A running watcher keeps the code it loaded at spawn;
  `shoal doctor` flags STALE-CODE.
- **Record it in your plan_state** under `## Running monitors`: target, exact re-arm command,
  owning session. A successor cannot re-arm what was never written down.

## Waiting on CI, a lease, a job, or anything slow

Every wait has a watcher of its own that wakes you: a job waiter (`shoal job wait`), a CI waiter,
or a lease waiter. Each has one of two shapes:
- **self-wake**, for long waits: it notifies your session and exits;
- **foreground**, for short waits: a blocking poll inside the turn.

A detached process that only writes a log is not a watcher, because an idle session never reads
the log. That includes `setsid nohup shoal job wait ... &` in a harness whose background commands
do not report back: it finishes, writes its verdict to a file, and wakes nobody.

**Write the wait in one `queue` call, with a checkable target** (`--wait local-ci --holder "job
<name>" --target job:<name>`). Do not follow it with a separate `shoal transition`: re-declaring a
wait without `--target` used to erase the target, and nothing could then tell the wait had ended.
Where the group sets `waittargets`, a CI wait without a target is refused, and your ledger watcher
also wakes you when a `job:` or `lease:` target resolves, as a backstop for a waiter that failed.

**A finished item leaves `next`.** Record it `--done` and drop it from `--next` in the same write; it
is never parked behind a hold. `queue` refuses a finished item in `next`.

**Waiter predicates must be total:**
- Watch terminal states at the level jobs actually fail at (matrix legs, steps), not only a
  rollup that can stay pending while legs fail underneath it.
- Name `cancelled` explicitly: a cancelled run is tolerated only if it is not on the current head
  (check the sha).
- An empty query result is an ERROR, never green.
- Read your predicate back when you arm it.

Pass a checkable `--target` on your `queue --wait`, so the health quorum can resolve the wait
itself and wake you if the target finished while your waiter died.

## The health quorum (what watches you)

Two health timers, run by the operator, evaluate every role on a schedule. They look for
unacknowledged work, resolved or expired waits, dead watchers, a turn that ended while `active`,
blocking items left unscheduled, and a dead relay. They wake you through your harness's adapter,
and page the maintainer when a wake has no effect. You never need a self-check timer of your own.
