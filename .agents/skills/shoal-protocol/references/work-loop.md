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
   - `--idle`: nothing actionable: an empty queue, or every next item blocked and, for a fill=self
     role, no offer and no workable `BOARD` candidate left (take one first, or pass each with its class);
   - `--blocked ITEM=<blocker>` (repeatable; `ITEM=` clears): why a next item cannot start. The
     blocker is `<role>:<repo#n>` (that role sees `BLOCKING`), `ask[:<n>]`, `user`,
     `upstream:<target>`, `lease:<key>` or `hold:<why>`. Recorded blockers carry forward while the
     item stays in `next`. Where the group verifies blockers, prefer a checkable one (a peer item,
     an upstream target, a lease): it clears itself. An `ask` or `user` blocker needs the ask
     recorded (`--ask`). A `hold` expires and must be re-verified; re-recording it is that check, so
     re-record only after re-checking what it names;
   - `--ask "<question>"`, `--answered <n>`, `--drop-ask <n> --drop-reason <why>`: see
     `asks-and-offline.md`.
3. Keep working the active item in the same turn.

## What `next` can tell you, and what to do

| line | action |
| --- | --- |
| `MAIL` | read it; act at this boundary (a maintainer message is printed in full) |
| `FREEZE ACTIVE` | start no job units, local suites or gated builds; checkpoint, persist your plan_state, push, and sleep |
| `FREEZE LIFTED` | the freeze is over: resume normal work, including anything a freeze message told you to hold |
| `NEXT BLOCKED` | a next item's recorded blocker: re-check it at each boundary and clear it (`--blocked ITEM=`) once gone |
| `BLOCKER STALE` | that blocker has cleared or expired and no longer lets you idle: start the item, or re-check and record what blocks it now |
| `BOARD` | an eligible candidate: when your next items are all blocked, take one rather than idling behind them, or pass it with a reason |
| `RETURN BLOCKED WORK` | fill=self, blocked next items, and an `OFFER` or a workable `BOARD` candidate listed: pass each blocked item back (`--pass ITEM:blocked`, it returns to the pool once unblocked), fill its place (an offer first), and take the first now if nothing is active. An imperfect lane fit beats a short queue: `out-of-lane` is not a reason to leave your queue short |
| `USER OFFLINE` | never wait on the maintainer; record asks with `queue --ask`; stack work; take unblocked items |
| `ASKS TO RAISE` | the maintainer is online: follow `asks-and-offline.md` now |
| `DROPPED ASKS` | disclose each to the maintainer in your next question set |
| `OFFER` | an item offered to you, with its priority and note |
| `TAKEN` / `GONE` | someone else claimed or finished an item in your queue: drop it (or `lost-race` on your active item) |
| `HELD-UNSCHEDULED` | schedule the claim, or release it with `--kind handoff` |
| `UMBRELLA` | your item has open rows (sub-issues): the umbrella licenses no work on them. Claim the row you work first (`queue --claim <row>`, then `--active <row>`); a row another lane holds is theirs |
| `UNDER UMBRELLA` | you hold a row of another lane's active umbrella; that lane was mailed, so keep to your row |
| `PR OPEN` | an open PR already closes the item: if it is not your branch, run `shoal claimed --shoal <id> <item>` and stop before writing code |
| `BLOCKING` | another role waits on your item: schedule it or pass it with a reason |
| `RESCAN EXPIRED` | your wait is unproven: re-verify it, re-raise the ask, or pivot to the next unblocked item |
| `AUX` | you are the residual lane: stay out of the named lanes' work |
| `BOARD` | ranked candidates when your queue is short (score, why, excerpt) |
| `WATCH ... NOT RUNNING` | re-arm your watcher after this boundary |

## Sleep statuses

Sleep only on a real wait or an empty queue:
- **Waiting:** arm the waiter first, then `queue --wait <sub> --holder <who> --target <kind:ref>`.
  The substates are `local-ci`, `remote-ci`, `research-proto`, `research-window`, `session-blocked`,
  `upstream`, `user-auth`, `user-design` and `queue-empty`. The target lets the health quorum check the wait for you
  (`job:<name>`, `lease:<key>`, `gh-run:<owner/repo>:<id>`, `gh-pr:<owner/repo>#<n>`,
  `gh-issue:<owner/repo>#<n>`, `az-build:<id>`, `time:<ISO UTC>`). A malformed target is refused, and so is one that
  is already resolved (a merged PR, a finished run): there is nothing left to wait on.
- **CI or research:** `local-ci` and `remote-ci` are for CI only: a suite, a build or a check that
  validates a change. A run that answers a research question or tries out a prototype (an experiment,
  a sweep, a recovery or ablation run, a benchmark for a design, a PR or issue prototype) is
  `research-proto`, wherever it runs and whatever it waits on (a job, a lease, a reservation, a hosted
  run). Start such runs with `job run --class research` inside a reservation, never as a `ci` job. A
  CI wait on a `--class research` job is refused. Research counted as CI hides where CI time and GPU
  queues actually go.
- **A research window:** research that is scheduled but not running yet (an overnight GPU window, a
  reservation to open) is `research-window` on `--target time:<ISO UTC>`, or on the queued `job:` or
  `lease:` it waits for. Never cover a window with a timer job (`job run` sleeping until a time) as a
  `local-ci` wait: it is not CI, and shoal wakes you itself when the window opens.
- **Which substate:** `session-blocked` is for another role in this shoal (`--blocked-on
  <role>:<item>`, never yourself). Your item waiting on something outside the shoal, such as an
  upstream issue or release, is `upstream` with a `gh-issue:` or `gh-pr:` target.
- **A wait covers the active item only.** If a next item is workable, work it rather than parking
  it behind the wait. A wait other than `local-ci` (a `research-proto` wait included) with a next item that has no recorded blocker is
  refused at the write, exactly like idle (`features.waitgate`): pop the item, or record its blocker.
- **Idle:** `queue --idle`, only when nothing is actionable: an empty queue, or every next item
  with a recorded blocker (`--blocked`). Otherwise the write is refused. A blocker that later goes
  stale shows in `next` as `BLOCKER STALE` and raises the health condition `parked-work`.
  A GPU or cpu-heavy lease held by someone else is a wait (`--wait local-ci --target lease:<key>`),
  not a reason to idle.

**Every wait expires, but shoal re-proves it for you.** Rescans are capped (4 h for maintainer and peer
waits and research, 3 h for CI, 12 h for `upstream`). While a wait's target is still pending, or a user
wait's ask is still open with no answer, a passed rescan does not wake you: `next` shows `RESCAN PASSED
..., proven by shoal`, and you are woken when the target resolves, the maintainer answers, or once a
day. Do not re-verify a proven wait. A `session-blocked` wait names its blocker with `--blocked-on <role>:<item>`.
`next` prints `WAIT TARGET RESOLVED` or `WAIT TARGET UNVERIFIABLE` when the target no longer
holds the wait: act on it the same turn. A `user-*` wait exists
only together with a raised, recorded ask.

## Bounded context

Every wake re-reads your whole context, so a long-lived session gets more expensive to wake. Keep your
start doc restart-ready at every boundary: a fresh session must be able to continue from it alone
(current state, decisions taken, next steps), with its `Last updated: <UTC>` line current. When `next`
or a wake prints `CONTEXT <n>k (limit <m>k)` in a shoal whose context restart is `auto`, the quorum hands
you to a fresh session at your next quiet boundary (idle or waiting, between turns) on the doc as it
stands, with no further step: bring the doc up to date before that turn ends. A doc that fails its
contract is not handed over; you are woken (`context-doc`) to fix it. In `page` mode, end the turn with
`queue --idle --restart-ready` (or your wait plus `--restart-ready`) instead. The flag is refused for a doc not
refreshed since the advisory or missing a required section; `shoal doc-check <doc>` says which (the
contract: shoal `docs/start-doc.md`). It is also refused for a doc naming a job folder still on this host,
since that folder goes when your incarnation is pruned: keep anything the doc names somewhere durable, such
as `tools/` beside the doc, not under `$CLAUDE_JOB_DIR`. The operator, or the quorum in `auto` mode, restarts you on that doc. Never restart
yourself, and never mark `--restart-ready` with an item active.

At that checkpoint, keep your start doc focused on current guidance: append completed details and obsolete decisions
to your append-only log, refresh the header, and append the session entry and snapshot. Work only in your own role
directory and commit by exact path. `queue --restart-ready` adds bounded, read-only lifecycle advice (header, dangling
references, untracked files and archive candidates), recorded in its queue event. Standalone sessions can run
`shoal lifecycle checkpoint <start-doc>` explicitly. In this advisory rollout, unresolved advice and scan timeouts
never stop a context restart, and no files are moved automatically. Routine model distillation and automatic archival
wait for measured pass costs on one or two roles. In propose mode list candidates for review or keep them with a
`keep: <reason>`; notify an owner whose live document references a file before proposing its move.

## Wakes

You are woken by new work while you are idle, a change to your active item, a wait you armed, or a
health wake. Every wake text starts with the offline banner when the maintainer is away. Handle a
watcher notification with status `killed` exactly like a wake: `next`, act, re-arm, and prove the new
pid in the same turn (two separate commands, `cat` the sidecar then `ps -p <that number>`, never
`$(...)`; see `watchers-and-waits.md`).

## Work that must survive

Long local work (test suites, renders) never runs as a harness background command, because a
harness can reap those. Run it as a job unit (`shoal job run --shoal <id> --slug <you> --name <n> --
<cmd>`) and wait on it with a background `shoal job wait`. Work needing a shared resource runs under
the lease provider (`providers.md`).
