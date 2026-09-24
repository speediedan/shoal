# The work loop

## At every boundary

1. **READ** `shoal next --shoal <id> --slug <you>`. Run it unpiped: MAIL and banners print first,
   and this call marks your mail read, so a `next` piped through `head` or `grep` loses messages
   unseen. Use `--peek` only when reading on someone else's behalf.
2. **WRITE ONE** `shoal queue --shoal <id> --slug <you> ...`:
   - `--active <item> --next <a,b,c>`: claims the active item and declares your queue version;
   - `--done <item> --url <evidence>`: records completion with the evidence;
   - `--pass <item>:<class>[:suggest=<role>]`: declines an item at the boundary;
   - `--wait <sub> --holder <who> --target <kind:ref> [--rescan-min N] [--blocked-on <role>:<item>]`;
   - `--idle`: nothing actionable;
   - `--ask "<question>"`, `--answered <n>`, `--drop-ask <n> --drop-reason <why>`: see
     `asks-and-offline.md`.
3. Keep working the active item in the same turn.

## What `next` can tell you, and what to do

| line | action |
| --- | --- |
| `MAIL` | read it; act at this boundary (a maintainer message is printed in full) |
| `USER OFFLINE` | never wait on the maintainer; record asks with `queue --ask`; stack work; take unblocked items |
| `ASKS TO RAISE` | the maintainer is online: follow `asks-and-offline.md` now |
| `DROPPED ASKS` | disclose each to the maintainer in your next question set |
| `OFFER` | an item offered to you, with its priority and note |
| `TAKEN` / `GONE` | someone else claimed or finished an item in your queue: drop it (or `lost-race` on your active item) |
| `HELD-UNSCHEDULED` | schedule the claim, or release it with `--kind handoff` |
| `BLOCKING` | another role waits on your item: schedule it or pass it with a reason |
| `RESCAN EXPIRED` | your wait is unproven: re-verify it, re-raise the ask, or pivot to the next unblocked item |
| `AUX` | you are the residual lane: stay out of the named lanes' work |
| `BOARD` | ranked candidates when your queue is short (score, why, excerpt) |
| `WATCH ... NOT RUNNING` | re-arm your watcher after this boundary |

## Sleep postures

Sleep only on a real wait or an empty queue:
- **Waiting:** arm the waiter first, then `queue --wait <sub> --holder <who> --target <kind:ref>`.
  The substates are `local-ci`, `remote-ci`, `session-blocked`, `user-auth`, `user-design` and
  `queue-empty`. The target lets the health quorum check the wait for you (`job:<name>`,
  `lease:<key>`, `gh-run:<repo>:<id>`, `gh-pr:<repo>#<n>`, `az-build:<id>`).
- **Idle:** `queue --idle`, only when nothing is actionable.

**Every wait expires.** Rescans are capped (4 h for maintainer and peer waits, 3 h for CI). A
`session-blocked` wait names its blocker with `--blocked-on <role>:<item>`. A `user-*` wait exists
only together with a raised, recorded ask.

## Wakes

You are woken by new work while you are idle, a change to your active item, a wait you armed, or a
health wake. Every wake text starts with the offline banner when the maintainer is away. Handle a
watcher notification with status `killed` exactly like a wake: `next`, act, re-arm, and prove the new
pid in the same turn.

## Work that must survive

Long local work (test suites, renders) never runs as a harness background command, because a
harness can reap those. Run it as a job unit (`shoal job run --shoal <id> --slug <you> --name <n> --
<cmd>`) and wait on it with a background `shoal job wait`. Work needing a shared resource runs under
the lease provider (`providers.md`).
