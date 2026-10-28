# Checks per posture

Every check is read-only. `<id>` is the group id, `<role>` a role slug, and `<state>` the per-group
state directory (`/tmp/<id>/` by default). Harness stores and the data root come from your host
map.

## Active

1. **Is the session mid-turn?**
   - Claude: `claude agents --json`, the row's `state` field (`working`, `blocked`, `done`). Never
     read `status`: it reads `busy` while any background task runs, including the role's own
     watcher.
   - opencode: the server's session status (`status:idle` or busy).
2. **Is it making progress?**
   - When it last wrote to the ledger (`shoal show`, last event).
   - The tail of its transcript: a recent tool call, and no long-pending tool call or question.
   - Any job units it started: `systemctl --user list-units 'shoal-<id>-<role>-*'`.
3. **Failure signatures:**
   - `active` with the turn `done`: the turn ended without a sleep posture. The quorum should catch
     this as `turn-ended-active`; if it did not, the liveness probe is wrong.
   - `working` for hours with no tool progress: stuck mid-turn. Read what it is blocked on.

## Waiting

1. **The wait record:** `<state>/wait-<role>` (sub, holder, target, rescan) must agree with the
   ledger state event.
   - A wait with no target cannot resolve. Was the target lost (a re-declaration without one), or
     never given?
2. **The target is pending:** `next --peek` prints `WAIT TARGET RESOLVED` or `UNVERIFIABLE` when it
   is not. For `job:<name>`, check the unit (`systemctl --user show`), its rc file and its log
   progress. For GPU or host-memory work, check that it runs under the lease.
3. **A watcher will fire when it ends:**
   - The waiter process is alive.
   - Its command waits on the same name as the target.
   - It has no expired timeout, and it is not piped through something that can block.
   - Its parent chain reaches the session (a background command the harness reports back on).
   - A `setsid`/`nohup` waiter in a harness that does not report background exits wakes nobody;
     with `waittargets`, the ledger watcher's target check is the backstop.
4. **History, before calling a monitor faulty:** for the role's recent jobs, compare each unit's end
   time with the session's wake (the task notification in the transcript) and its next ledger
   write.
   - A wake within seconds, followed by minutes of work, is healthy.
   - A long gap with no wake is the fault.
   - Long jobs (a runner that yields to CI, or lease queueing) are not faults.
5. **Stale blockers hidden by the wait:** a `local-ci` wait exempts the role from `parked-work`, so
   its next items' expired holds stay quiet while it chains jobs. List them and tell the role to
   re-verify them when the job ends.

## Idle

1. **Each blocker is true now:**
   - `<role>:<repo#n>`: is that item still open and in the named role's queue?
   - `upstream:<target>`: is the upstream issue or PR still open? An open PR is pending whatever its
     checks say.
   - `ask:`/`user:`: is an ask recorded in the queue, and has the maintainer answered elsewhere?
     (Read the role's harness question history.)
   - `hold:<why>`: re-check the claim it makes. A hold that names the maintainer is an ask
     nobody raised. A hold that names a peer item is a role blocker in disguise. A hold on a
     finished item is a finished item.
2. **Finished items:** any item in `next` that is done (by anyone) or closed must be dropped.
3. **Exhausted?** Compare the digest's rows for the role, plus your own read of its brief, against
   the board.
   - List unclaimed, unblocked items that fit the role and that it has not passed.
   - A role that only takes offers (`fill=assigned`) is legitimately idle with no offers. Offering
     it work is the maintainer's call: propose, do not offer unasked.
4. **A maintainer answer the role could not carry out** (for example "offer this to X", which only
   `user` can do): carry it out as the operator, and record it.

## Cross-cutting

- **Pages and wakes since the last review:** count them by condition. A condition that keeps
  climbing to a page while the role renews the same state is a mechanism gap, not a lane failure.
- **Orphaned waiters:** waiter processes whose parent is init, or that poll paths that no longer
  exist. List them with their start times; remove them only with the operator's approval.
- **Instruction drift:** a versioned instruction edit left uncommitted makes the sync hook fail
  every other lane's commit. Run the sync `status` across all harnesses.
- **Instrument availability:** a harness CLI that fails (for example during a self-update) blinds
  the liveness probe for that tick. Note it; do not read silence as health.
