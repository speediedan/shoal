# Status blocks and row states

## Row states

Each role row carries exactly one state:
- **active:** executing toward the named item right now. A posted block means execution just
  stopped, so a block posted at the end of a turn is never `active` (except from a foreground
  waiter that never sleeps).
  - **Your own subagents running on the item is `active`, not a wait.** The work is yours and
    proceeds without anyone else, and each agent's report re-enters your session, so no waiter or
    target is needed. Record `queue --active <item>` before dispatching them; health reads a turn that
    ends with your agents in flight as still working (a background shell does not count). Work with
    no item yet (proposals the maintainer asked for) still needs one: claim the issue it serves, or
    ask for an offer.
- **waiting (<reason>):** not working, for a named reason: `local CI`, `remote CI`, `shared
  resources: <lease>`, `blocked on <role>: <what>`, `user feedback`, `user auth`. A bare `waiting`
  is not a state.
  - `waiting (user auth)` is valid only while the ask is actually raised (or recorded while the
    maintainer is offline) and no other item is actionable without it.
  - A lane with actionable items is not waiting: it takes the next one.
- **idle:** alive, unblocked, nothing actionable. Idle with a non-empty `next` is a contradiction:
  start the item or record its blocker (`queue --blocked ITEM=<blocker>`); the queue refuses
  `--idle` otherwise.
- **offline:** the session has ended. Its unfinished work is recorded with the branch, worktree,
  base commit, and what is committed, unpushed and remaining.

Record every change with the `queue` write that carries it (or `shoal transition`). That write is
your heartbeat and your wait proof. Never quote a harness's own busy/idle word as your state: it
describes the process, not the queue.

## The per-session status block

Post it after every completed turn. It describes the status you are sleeping in, not the turn's
activity, which goes in your prose and the ledger:

```markdown
| | status | next (max N) |
| --- | --- | --- |
| **Lane** · `<slug> (<slug>-r<N>)` | waiting (remote CI: <what>; watchers <pids>) | [<repo>#<n>](...):fix, ... |
| **Pipelines** | <gate ids and their states> · lease: <status> | (none) |
| **Worktrees** | `<path>` (<branch>, clean/dirty; shared with <role> if shared) | (retire only after merge + prune) |
```

**Rules:**
- **Three rows, always.** A renamed row that swallows the others hides gate status and work
  location at once.
- **Every item is a link with a verb:** `:fix` or `:review`.
- **`next` shows the group's `next_max` items (default 3),** or an explicit `(queue empty: deliberate)`.
- **The Worktrees row names every tree you read or write,** your active one first.
- **A worktree leaves the row only after** its branch merged and `git worktree prune` confirmed
  the removal.
- **Decisions and authorizations never go in the block.** They are prompted asks
  (`asks-and-offline.md`).
