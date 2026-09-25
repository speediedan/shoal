# Status blocks and row states

## Row states

Each role row carries exactly one state:
- **active:** executing toward the named item right now. A posted block means execution just
  stopped, so a block posted at the end of a turn is never `active` (except from a foreground
  waiter that never sleeps).
- **waiting (<reason>):** not working, for a named reason: `local CI`, `remote CI`, `shared
  resources: <lease>`, `blocked on <role>: <what>`, `user feedback`, `user auth`. A bare `waiting`
  is not a state.
  - `waiting (user auth)` is valid only while the ask is actually raised (or recorded while the
    maintainer is offline) and no other item is actionable without it.
  - A lane with actionable items is not waiting: it takes the next one.
- **idle:** alive, unblocked, nothing actionable. Idle with a non-empty `next` is a contradiction:
  start the item or name the blocker.
- **offline:** the session has ended. Its unfinished work is recorded with the branch, worktree,
  base commit, and what is committed, unpushed and remaining.

Record every change with the `queue` write that carries it (or `shoal transition`). That write is
your heartbeat and your wait proof. Never quote a harness's own busy/idle word as your state: it
describes the process, not the queue.

## The per-session status block

Post it after every completed turn. It describes the posture you are sleeping in, not the turn's
activity, which goes in your prose and the ledger:

```markdown
| | status | next (max N) |
| --- | --- | --- |
| **Lane** · `<slug> (<slug>-r<N>)` | waiting (remote CI: <what>; watchers <pids>) | [<repo>#<n>](...):fix, ... |
| **Pipelines** | <gate ids and their states> · lease: <posture> | (none) |
| **Worktrees** | `<path>` (<branch>, clean/dirty; shared with <role> if shared) | (retire only after merge + prune) |
```

**Rules:**
- **Three rows, always.** A renamed row that swallows the others hides gate posture and work
  location at once.
- **Every item is a link with a verb:** `:fix` or `:review`.
- **`next` shows the group's `next_max` items (default 3),** or an explicit `(queue empty: deliberate)`.
- **The Worktrees row names every tree you read or write,** your active one first.
- **A worktree leaves the row only after** its branch merged and `git worktree prune` confirmed
  the removal.
- **Decisions and authorizations never go in the block.** They are prompted asks
  (`asks-and-offline.md`).
