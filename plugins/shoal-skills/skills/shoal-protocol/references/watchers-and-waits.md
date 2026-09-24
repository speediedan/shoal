# Watchers and waits

## Your ledger watcher

Your brief gives the exact `shoal ledger-watch` command. Arm it in the way your harness supports:
- a background command that re-invokes you when it exits (Claude Code: the Bash tool with
  `run_in_background`, never the Monitor tool, which expires);
- a detached process that sends its wake to your session id (opencode).

**Rules:**
- **Armed means proven.** Its pid is alive (`ps -p`) and matches the sidecar. Re-arm after every
  wake and prove it in the same turn. A lane that acted on a wake and never re-armed is deaf.
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
the log.

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
