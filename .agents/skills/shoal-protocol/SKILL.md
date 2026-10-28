---
name: shoal-protocol
version: 0.1.0
description: >-
  The working protocol for a session that holds a role in a SHOAL group (Stigmergic
  Heterogeneous Open Agent Lattice): agents in any harness coordinating only through a shared,
  append-only ledger and a priority-ordered backlog. Load it when your brief says you are a role in
  a SHOAL group, before your first `shoal next`, and whenever you are unsure how to wait, ask,
  hand off or report.
---

# SHOAL protocol

A SHOAL group is a set of role sessions, possibly in different harnesses (Claude Code, opencode,
Codex), owned by one or more people, working one backlog. **Nobody coordinates.** Sessions never
message each other directly to get work done. Each one reads the ledger, acts, and writes one event
per decision. That is stigmergy: coordination through the traces each agent leaves.

## The five rules that matter most

1. **Two verbs per boundary.** At every boundary (a wake, or finishing or blocking an item), run
   `shoal next --shoal <id> --slug <you>` UNPIPED, then write exactly one
   `shoal queue --shoal <id> --slug <you> ...`. Then keep working the active item in the same
   turn. Details: `references/work-loop.md`.
2. **Every turn ends in a sleep posture.** The last write of a turn is `queue --wait <sub>
   --holder <who> --target <kind:ref>` (with the waiter armed first), `queue --active <next>` (and
   keep working), or `queue --idle`. A turn that stops while `active` gets woken to continue.
   **Idle means an empty queue, or every next item has a recorded blocker** (`queue --blocked
   ITEM=<blocker>`); `queue --idle` is refused otherwise, and so is a wait other than `local-ci`
   with such an item (stack rather than stall). A `CONTEXT` line in `next` asks you to refresh your
   start doc and end with `--restart-ready` (`references/work-loop.md`, "Bounded context"). A blocker `next` prints as `BLOCKER STALE`
   no longer counts, and where `next` shows `BOARD` candidates beside blocked items, take one or pass
   it with a reason. A busy lease is a wait on `lease:<key>`, never idle. A freeze binds only while `next` prints `FREEZE ACTIVE`; a message announcing one
   binds nothing once `next` stops showing it.
3. **Only work wakes you.** New work while idle, a change to your active item, a wait you armed, or
   a health wake. Mail never wakes you; read it at the boundary. To get a peer to do something, the
   maintainer offers it as work, or you hand your claim over with `release --kind handoff --to
   <role>`. A maintainer answer that delegates your items to a peer is carried out the same way
   (claim each item, then hand it over), never by mail, and the ask is not done until the handoff
   or the maintainer's offer is in the ledger. `queue` refuses a blocker on a role that cannot
   take the item. Never drive another session's terminal (no multiplexer command that types into a
   pane or prompts an agent, such as `herdr pane send-text` or `herdr agent prompt`): every wake goes
   through shoal, or the health quorum and the failure-mode metrics never see it.
4. **The session that needs an authorization asks for it, first-person, as a prompted question.**
   Build the question sets with `shoal asks --question-json` (live PR labels, at most four questions
   per set), record the ask and a user-auth wait BEFORE the question tool (it blocks your turn), and
   never raise a question while `next` says USER OFFLINE. Details: `references/asks-and-offline.md`.
5. **Your watcher is your ears.** Arm `shoal ledger-watch` as instructed in your brief, prove its pid
   every time you re-arm (two separate commands: `cat` the sidecar, then `ps -p <that number>`;
   never `$(...)` command substitution), and treat a `killed` notification as a wake. Details:
   `references/watchers-and-waits.md`.
6. **Every completed turn ends with your status block**, posted after the last `queue` write. It
   is the maintainer's only per-lane view, and a turn without one reads as a stall. Three rows,
   always; the Lane row states the posture you sleep in (never `active` once you stop):

   ```markdown
   | | status | next (max N) |
   | --- | --- | --- |
   | **Lane** · `<slug> (<slug>-r<N>)` | waiting (<reason>; watchers <pids>) | [<repo>#<n>](<url>):fix, ... |
   | **Pipelines** | <gate ids and states> · lease: <posture> | (none) |
   | **Worktrees** | `<path>` (<branch>, clean/dirty) | (retire only after merge + prune) |
   ```

   Rules and examples: `references/status-and-states.md`.

## References

| file | read it when |
| --- | --- |
| `references/ledger.md` | you need the event types, who may write what, claims, declines, handoffs, or you work with a session outside your shoal |
| `references/work-loop.md` | you are at a boundary, choosing a posture, or popping the next item |
| `references/watchers-and-waits.md` | you are about to wait on CI, a lease, a job, or anything slow |
| `references/asks-and-offline.md` | you need the maintainer's decision, or `next` shows ASKS / USER OFFLINE |
| `references/status-and-states.md` | you post a status block, or report your state |
| `references/handoffs.md` | your session is ending, or a lane is being folded into another |
| `references/providers.md` | a wait target or resource lease behaves unexpectedly |

## Vocabulary

- **role / lane:** a named job (`slug`) held by one session at a time. The session tag is
  `<slug>-r<N>`; your row label is `<slug> (<slug>-r<N>)`, copied from `next`.
- **fill:** `self` (you draw from offers, then from the ranked board), `assigned` (offers only),
  `none` (you never claim).
- **incarnation / generation:** one restart of the group. Sessions change; the ledger and the
  role slugs persist.
- **item:** `<repo>#<n>:<verb>`, with verb `fix` or `review`.
