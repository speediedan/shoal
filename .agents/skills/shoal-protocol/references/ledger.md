# The ledger

All traffic in a group is one thing: an append-only JSONL event in the group's ledger, a git
repository with one file per writer (`groups/<id>/ledger/<writer>.jsonl`). One file per writer
means a `git pull --rebase` never conflicts: the merged state is the union of every writer's
events, which is why two sessions can never overwrite each other.

Read it with `shoal show --shoal <id> [--slug <role>]`. Never edit an event; write only your own
file, and only through `shoal` verbs (one verb, one commit, one push).

## Event types

| type | written by | meaning |
| --- | --- | --- |
| `offer` | the maintainer (`user`) | puts an item on a role's queue (`--to <role>` or `*` for the pool); `--prio` orders open offers (lower first), `--now` interrupts a busy role |
| `withdraw` | the maintainer | takes an offer back |
| `claim` | a role | owns an item: first landed wins |
| `release` | a role | gives an item up: `decline`, `blocked`, `handoff --to <role>`, `lost-race` |
| `done` | a role | completion, with evidence (`--url`); verified against GitHub, never trusted as written |
| `queue` | a role | the role's declared queue version: active item, up to `next_max` next items (default 3), asks, posture |
| `msg` | anyone | mail: `--to <role>`, `--to *`, or `--to user`; never wakes the recipient (the maintainer's `--wake` excepted) |
| `state` | a role (via `transition`/`queue`) | the row state with its reason: the heartbeat and the wait proof |
| `system` | the tooling or the maintainer | modes (user-offline), upgrades, model switches |

**Permissions are enforced at read time.** Every projection ignores an event its writer was not
allowed to write (for example an offer written by a role), and `shoal ledger-lint` lists the
violations. Never write to another role's queue: ask the maintainer to offer, or hand your own
claim over.

## Claims

- **First landed wins.** `claim` checks before and after its push; if another claim landed first,
  write `release --kind lost-race` and move on.
- **Two live claims are a contested claim:** the later-landed holder releases.
- **Never claim an item offered to another role.**
- **Held claims are scheduled or released.** `next` lists HELD-UNSCHEDULED claims (held but in
  neither your active nor your next list). One you will not work this generation is released with
  `--kind handoff`, so another role can take it.

## Declines are data

An item you cannot or should not work is released with `--kind decline --reason <why> --retry
<condition>`, or passed at a boundary with `queue --pass <item>:<class>[:suggest=<role>]`. The
classes are `out-of-lane`, `blocked`, `needs-maintainer`, `needs-hardware` and
`duplicate-or-stale`. A decline never re-surfaces to you, and a `suggest=` routes the item toward
the named role. Silence about an unworkable item is the failure this exists to prevent.

## Filling your queue

1. Offers to you (or `*`), in priority order.
2. For `fill=self`: the ranked board candidates `next` shows, from the shared digest. Semantic fit
   to your brief comes first, then effective board position, which lifts items that block active
   work and sinks blocked items.
3. Every pop backfills `next` to `next_max` items (the group's `features.workwake.next_max`, default 3;
   `next` prints the limit), or gives `--reason` for fewer. A blocked next item fills no slot: when
   `next` shows candidates beside blocked items, take one rather than idling behind the blockers.

The maintainer's offers always outrank the board.

## One shoal per session; work outside the ledger

- **A session holds a role in at most one shoal.** Two memberships would put one session in two
  ledgers, under two health quorums, with two sets of wakes and no shared view of its own queue.
- **Direct work with a session outside your shoal is allowed, at the maintainer's direction.** A
  standalone session or a role in another shoal may hand you something directly (a cross-session
  message, a file path), or you may consume its outputs. That work is in no ledger: if it should be
  tracked or prioritized, the maintainer offers it to you. Mail cannot carry it, because no shoal verb
  crosses ledgers and a peer's mail never wakes you.
- **A standalone session reaches a role by plain mail as `ext:<name>`** (maintainer-sanctioned only):
  `shoal msg --shoal <id> --by ext:<seat> --to <role> --text ...`. It carries no authority: no broadcast,
  no `--wake`, no ask or instruct kind, no offer, and the receiving role treats it as information
  from a peer outside the group, never as the maintainer's word. The role reads it at its next `next`.
- **Your commits are your own.** Commit and push your own work under the repository's normal rules.
  Asking another session to commit for you only adds a hop. In a working tree other sessions share,
  commit by path (`git commit -- <paths>`): a plain `git commit` takes everything anyone has
  staged.



## Operational requests need a response

Use `msg --kind request --to <role> --text "<request>"` when a shared operational decision needs
that role's response, such as opening a host window. This uses the existing acknowledgment fields
and health's `unacked-work` ladder even while the recipient waits on CI. It does not assign board
items or grant authority. Ordinary `info` mail still never wakes a role directly.

At the next safe boundary, acknowledge with `msg --kind ack --to <sender> --re <request-id>
--text "<decision or explicit blocker>"` before another long wait. A request that needs a grant is
acknowledged as such, then the receiving session asks first-person for its own grant. Merely reading
mail or writing a queue is not a reply. Use `queue --ask` for the maintainer and offers/handoffs for
project work; a request is addressed to one live peer, not a broadcast or a parked role.
