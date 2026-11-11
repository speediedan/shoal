# SHOAL architecture

This is a map of the components as the code implements them now. Commands are in
[`operator-cheatsheet.md`](operator-cheatsheet.md); the host contracts are in
[`providers.md`](providers.md); the reasoning behind the design is in
[`decisions.md`](decisions.md) and the failure modes each guard answers are in
[`guards.md`](guards.md). When this document and the code disagree, the code wins and this
document is wrong: fix it in the same change.

## Design drivers

Every mechanism below exists to serve one of these, in this order.

1. **Minimize cross-session communication and session wake-ups.** A wake is a model forward pass
   over a long context, so it is the dominant cost of running a group. Communication is therefore
   asynchronous and indirect:
   - sessions leave traces in a shared ledger instead of messaging each other;
   - a session is woken only when there is work for it, and mail never wakes it;
   - each boundary is one read and one write;
   - the health timers spend no model tokens unless they have to wake someone.
2. **Retain contextual efficiency.** Work is routed to the role whose accumulated context already
   fits it, so each session spends its context on one coherent area instead of re-learning a
   different one per item.
3. **Stay neutral across harnesses and models.** The ledger, the backlog and a small set of CLI
   verbs are the only shared surface. Anything specific to one agent ecosystem sits behind one
   interface, with one implementation per ecosystem.
4. **Recover without a coordinator.** No session holds the plan. Stalls, dead watchers and stale
   waits are detected from ledger and host evidence, and repaired by the timers.

**Scope today.** The design supports a swarm of sessions working efficiently through an ordered,
multi-repository body of work for **one maintainer**. Depending on how useful that proves, a future
incarnation may be refactored to accommodate shoals to which **several users contribute sessions**
while targeting one shared multi-repository work set; what that would need is in
[`limitations.md`](limitations.md), and the direction is sketched in [`roadmap.md`](roadmap.md).

## 1. Shape

A SHOAL group is a set of **semantically aligned roles**. A role is a stable area of expertise,
described by a short brief, that work is routed to so its context stays efficient. Each role is
held today by one live session in any supported agent harness (Claude Code and opencode today;
Codex is planned). The mapping is 1:1 for now, but it need not stay so: a future version could let
several sessions share one role's contextual expertise. The roles cooperate only through **one
append-only event ledger** and **one priority-ordered backlog**. Nobody coordinates: there is no
coordinator role, no queue file per role and no direct messaging between sessions. Each session
reads the traces the others left, acts, and writes one event per decision (stigmergy).

- **Each role owns its queue**: one active item and up to N next items (`features.workwake.next_max`,
  default 3), declared by the role itself as `queue` events in its own ledger file.
- **A session wakes only for work**: new work while it is idle, a change to its active item, the
  resolution of a wait it armed, or an intervention from the health quorum.
- **Per boundary a role makes one read (`shoal next`) and one write (`shoal queue`)**, then keeps
  working the active item.
- **The health quorum** is a pair of timers that use no model tokens unless they wake someone.
- **Telemetry** records every posture change and every wake; reports are built from it.

An item is `<repo>#<n>:<verb>`, with verb `fix` or `review`. The backlog is currently a GitHub
Projects v2 board; its order is the priority, and SHOAL never reorders it. The board is one source
implementation (`shoal/sources.py`). A planned design step is to accept any ordered set of tasks
that meets a defined source protocol (stable item ids, an order, open or closed state, and a body
to match against), so a group can work from other trackers or a plain file.

## 2. Components

| component | code | enabled by | job |
| --- | --- | --- | --- |
| CLI | `shoal/cli.py` (`shoal`) | always | every verb; subcommands are defined by the modules below |
| host config | `shoal/config.py`, `config/host.toml` | always | every host-specific value (section 8) |
| spec | `shoals/<id>/shoal.json` in the ledger repository (`shoal/roles.py`, `shoal/layout.py`) | always | roles, sources, repos, `features`, generation history |
| ledger | `shoals/<id>/ledger/<writer>.jsonl` (`shoal/ledger.py`, `shoal/ledger_cli.py`) | always | all traffic between roles; one writer per file |
| role start doc | each role's `plan_state.md` | always | mission, current state, next steps, `grants-needed`, a `Last updated` stamp, a short header (the contract: [`start-doc.md`](start-doc.md); `shoal doc-check`, `shoal doc-head`) |
| lifecycle settings | `shoal/lifecycle.py` (`lifecycle show`), `<repository>/.shoal/lifecycle.toml` | always | a repository's role-document layout, tiers, header cap, cadences and hooks; hooks only for repositories in the host's `[lifecycle] trusted_repos` ([`lifecycle.md`](lifecycle.md)) |
| archive | `shoal/archive.py` (`archive`, `--restore`, `--cold`) | always | reference-aware warm archival of role documents: refuses roots and live-referenced files, one commit of exactly its paths, an append-only manifest per directory, undone when the commit does not land ([`archive.md`](archive.md)) |
| references | `shoal/refs.py` (`refs`) | always | read-only: the start-doc roots live specs and standalone sessions name, and what names each file of a role directory (classes, archive candidates, untracked and dangling) ([`refs.md`](refs.md)) |
| standalone registry | `shoal/standalone.py` | always | the per-host list of saved standalone sessions (`ui-standalone.json`); the viewer re-exports it |
| briefs | `shoal/briefs.py` | always | the first prompt of every session, identical in shape for every role |
| work loop | `shoal/worker.py` (`next`, `queue`, `asks`, `offline`, `freeze`) | `workwake` | one read and one write per boundary |
| session watcher | `shoal/ledger_watch.py` (`ledger-watch`) | always (work-only waking with `workwake`) | wakes its own session |
| health quorum | `shoal/health.py` (`health`, timers `health-a` and `health-b`) | `health` | detect and recover stalled roles, dead watchers, stale waits |
| session wake | `Backend.wake` in `shoal/backends.py`, one implementation per harness | always | put a wake in front of one role's session (section 7) |
| job units | `shoal/jobs.py` (`job run`, `wait`, `status`, `stop`) | always (brief guidance with `jobs`) | long local work outside the harness |
| pool placement | `shoal/pool.py` (pure planner), `shoal/pool_cli.py` (`pool probe`, `pool status`, `pool plan`, `job run --place`) | always (queue transfers with `placement`) | place CI jobs across this host and its `[hosts]` routes by predicted queue wait (section 6) |
| board digest | `shoal/digest.py`, `shoal/sources.py` (`digest`, `triage`, `context`) | `digest` | one board read per group, ranked per role |
| semantic matcher | `shoal/matcher.py`, run by the digest | `matcher` (`live: false` is shadow) | a model scores items against each lane's brief |
| residual lane | a role with `aux: true`, `aux-init`, `aux-launch` (`shoal/aux.py`) | optional role | work no active lane covers |
| wait-target providers | `shoal/providers.py` | always | resolve `--target <kind>:<ref>` to pending, resolved or unknown |
| harness adapters | `shoal/backends.py` | always | create, message and probe sessions per harness |
| incarnation pruning | `shoal/prune.py` (`prune-incarnations`), `Backend.prune` per harness | by hand; automatic with `prune` | remove old role incarnations from each harness's session listing, behind guards (section 7) |
| sensors | `shoal/watchers.py` (`watch lanes`, `watch pipeline`) | always | harness server respawn, CI pipeline starvation |
| heartbeat daemon | `shoal/heartbeat.py` | when `health` is off | stall detection without the quorum |
| live upgrade | `shoal/upgrade.py` (`upgrade`) | always | roll new code and feature edits into a running group |
| telemetry and reports | `shoal/telemetry/` (`transition`, `report`, reconcile) | always | events, ground truth, the review artifact |
| host instruments | `shoal/psi_watch.py`, `shoal/kill_audit.py` (`kill-audit`) | run by the operator | memory-pressure fires and harness reaps, joined |
| timers | `shoal/systemd.py` (`install-systemd`, `uninstall-systemd`) | per spec | systemd `--user` units named `shoal-<id>-<job>` |
| preflight | `shoal/doctor.py` (`doctor`) | always | host checks, and group checks with `--spec` |
| state primitive | `shoal/state.py` (`transition`) | always | heartbeat, wait proof and telemetry in one command |
| operator UI | `shoal/ui/` (`ui status`, `ui line`, `view`, `ui open`, `ui feed`, `ui menu`), `plugins/shoal-herdr/` | run by the operator | a read-only status contract for every shoal on a host, a viewer that never resumes, and the Herdr adapter (section 12) |

**Where state lives.**

- **Ledger repository** (a git repository every host clones, `ledger_repo` / `ledger_dir`): the
  spec (`shoal.json`) and the ledger, under `shoals/<id>/`. `ledger-init` (on a new or an existing
  ledger) makes its `.gitignore` name the clone's flock file `.shoal-lock` and `*.tmp`, keeping
  existing lines and never duplicating one, and commits the change: an untracked lock file makes
  every rebase, and so every pull, refuse.
- **Data root** (`data_root`, never committed), per group under `<data_root>/<id>/`: `contexts/`,
  `roles/<slug>/plan_state.md` (generated start docs for the residual lane),
  `matcher/`, `briefs/`, `telemetry/`, `reports/`, `jobs/`, `board_digest.json` and, in shadow
  mode, `board_digest.semantic.json`.
- **Scratch** in `/tmp/<id>/`: heartbeats, wait proofs, watcher sidecars, timer ticks, wake
  requests, and the UI's cached health conditions (`ui-conditions.json`).
- **Operator UI state** (per host, never committed): saved standalone sessions in
  `~/.config/shoal/ui-standalone.json` (an entry closed in the viewer is marked `dismissed` and stays closed), UI-originated actions in `<data_root>/ui-actions.jsonl`.
- **Matcher key** (optional paid fallback only): `SHOAL_MATCHER_API_KEY`, from the environment
  or a gitignored `shoal/.env`.

**Row labels.** Wherever a role is shown, its label is the slug with the session tag in
parentheses, `<slug> (<slug>-r<N>)`. The slug is the stable identity; the tag says which
generation holds it.

## 3. The ledger

- **Nine event types:** `offer withdraw claim release done msg state system queue`. Each is one
  CLI verb and one commit. A role writes only `<slug>.jsonl`; `user.jsonl` belongs to the
  maintainer and `shoal.jsonl` to the tooling. `git pull --rebase` never conflicts because no two
  writers share a file.
- **Permissions are read-time.** Every projection ignores a line its writer was not allowed to
  write, and `ledger-lint` reports it (E1). Only `user` and the system writer may `offer` or
  `withdraw`; a role-written offer is a violation. A `queue` event outside its role's own file,
  or a mode change by anyone but `user` or the system writer (`shoal`), is a violation.
- **Claims are first-landed-wins.** `claim` checks before and after its push; a loser writes
  `release --kind lost-race`.
- **Declines are data.** `release --kind decline --reason .. --retry <condition>`; the item never
  re-surfaces to that role.
- **Handoffs.** `release --kind handoff --to <role> [--prio N]` turns the holder's claim into an
  implicit offer to that role. It is the only way a role gives another role work.
- **Withdrawals never end a claim.** `withdraw` takes back an open or claimed offer; a claim on it
  stays live until its holder releases it, and the holder's watcher wakes it with `WITHDRAWN`.
- **Transfers.** `shoal transfer --item X --to <role>` moves an item between roles. An item that is
  only offered moves at once (its offers withdrawn, a new offer to the target). A held item moves
  through its holder: a woken `request` asks it to hand the item off, and a `system` event
  `transfer` (`item from to prio req note`) records it. `View.transfers()` reads each one as
  `pending`, `done` (the handoff to the target landed), `failed` (released another way) or
  `superseded` (a later transfer, or `done`).
- **Completion is verified against GitHub** by reconcile, never taken from `done` events.

## 4. The work loop and wakes

**The queue stream.** A role's latest `queue` event (`active`, `next` of up to `next_max`, `asks`,
`reason`, `posture`) is its queue and its posture. The maintainer influences it with `offer`
(`--prio`, and `--now` to interrupt a busy role) and `withdraw`. shoal never writes a role's
queue: `shoal queue-order --to <role> --next a,b,c` checks each item (held, offered, or a board item
for a self-filler; never another role's, done, or the active item) and sends a woken `request`
carrying the `order`, which `next` shows first as `QUEUE-ORDER`. The role applies it with its own
`queue --next` and acks; `View.queue_orders()` reads each order as `pending`, `applied` or
`superseded`, and a replaced order leaves the `unacked-work` ladder. Both verbs print one
`REFUSED <item>: <reason>` line per refused item and write nothing when any is refused.

**Two verbs per boundary.**

- `shoal next` reads, in one call: the queue version, open offers, digest candidates when `next`
  is short, the integrity of the active and next items (taken, done elsewhere), unread mail, wait
  status, and whether the role's watcher is alive.
- `shoal queue` writes one ledger commit: `done`, claims (the active item is claimed if not held),
  passes with a reason class and an optional `suggest=<role>`, asks, the posture, and the new
  queue version. Heartbeat, wait proof and telemetry are written locally in the same call.
- Every pop backfills `next` to `next_max` (default 3), from offers by priority and then digest
  candidates, or records why fewer. `next` shows candidates whenever the queue has an open slot: a
  next item with a recorded blocker holds no slot, and a role with no active item has one more.
  Without `--reason`, a short queue's default reason is checked against the candidates (`short
  queue: N eligible candidate(s) not taken` when there are any). A `fill=self` role with no active item and every
  next item blocked also gets `TAKE BOARD WORK` when an offer or a workable candidate is listed: a blocked queue is
  not a reason to idle, so it takes an offer first, else the first candidate, and keeps the blocked items in `next`
  with their blockers.

**Every turn ends in a sleep posture:** `queue --wait <sub> --holder <who> --target <kind:ref>`
with the waiter armed first, `queue --idle`, or `queue --active <item>` and keep working. Idle is
refused while any next item lacks a recorded blocker (`queue --blocked ITEM=<blocker>`, carried
forward while the item stays in `next`; a role blocker shows its holder `BLOCKING`), because an
idle role is woken by nothing else.

**Verified blockers** (`blockers`, `true` or `{hold_ttl_min, peer_ttl_min, hold_page_min}`, defaults
240, 720 and 720 minutes). A recorded blocker is checked, not trusted: each carries metadata in the
queue event (`next_blocked_meta`: `since` it was last recorded, `first` it blocked the item, `n`
re-records), and `shoal/blockers.py` judges it by kind. `<role>:<repo#n>` clears once that item is
done; `upstream:<target>` and `lease:<key>` clear when their provider says the target is no longer
pending (an upstream `gh-pr` target stays pending while the PR is open, whatever its checks say); `ask:`/`user:` holds only while the queue records an ask, and is refused without one;
`hold:<why>` is unverifiable, so it expires after `hold_ttl_min`, and role and provider blockers
after `peer_ttl_min`. A stale (cleared or expired) blocker parks nothing: `next` prints `BLOCKER
STALE`, the idle gate and `parked-work` treat its item as free, `blocker-resolved` wakes the role once if a role or provider blocker cleared, and recording a blocker that has
already cleared is refused. So is a `<role>:<repo#n>` blocker naming a `fill=assigned` or `fill=none`
role that holds no offer, handoff or claim for the item: nothing could ever end that wait (FM-4). Re-recording a blocker is its re-verification. A hold kept past
`hold_page_min` wakes the role to convert it, then pages the maintainer 240 minutes later if it still stands (`hold-long`). With the feature on, `queue --idle` is also
refused while `next` would offer strong-fit candidates, until each is taken, listed or passed with
a reason class, and a role's watcher wakes it for board candidates when every next item is blocked,
not only when `next` is empty. Measured 2026-09-29: two lanes idled 2.5 and 11.5 hours behind
blockers that were all stale, beside 2 and 8 eligible candidates. With
`continuation`, a role whose posture is `active` while its session is proven between turns is
woken to continue after `after_min` (default 5).

**Wakes are work-only.** A role's `ledger-watch` wakes it for: an offer to it or to `*` while it is
idle, or a `--now` offer at any time; board candidates when it is idle with nothing queued; a lost
claim on, or withdrawal of, its active item; a health wake request. **Msgs are mail**: `next`
prints them, and they wake nobody unless `user` sends one with `--wake`.

**User-offline mode.** `offline on` is a `system` event from `user`. `next` then tells every role
never to wait on the maintainer, to record each blocking ask with `queue --ask`, to stack PRs where
work depends on an unmerged one, and to keep working. Roles waiting on the maintainer are woken
once to pivot, and maintainer pages are deferred. `offline off` prints every recorded ask per role,
with a likely-stale hint where the named item is merged, closed or done, wakes each role holding
asks to re-verify, drop or raise them as prompted question sets (at most four questions per set),
and delivers the deferred pages as one summary.

**Signed standing grants** (`features.grants`). A `grant` event from `user` carries an ssh
signature (`ssh-keygen -Y sign -n shoal-grant`, maintainer's key) over its canonical form;
readers verify against the host's `allowed_signers` and ignore anything unverified
(`ledger-lint` E4). Grants are keyed by role (or `"*"` for the shoal), never by session, so they
survive restarts and ecosystem changes; briefs and `next` print the verified set, and a
grants-needed item marked `[grant: <kind-or-id>]` is dropped from the restart ask when a live
grant matches. Revocation is a signed event naming the grant; expiry needs none.

**Freeze mode.** `freeze on|off` is the same kind of `system` event, for a restart or maintenance
window. While it is on, `next` prints `FREEZE ACTIVE` (checkpoint, persist the plan_state, push,
sleep), `job run` refuses to start a unit, idle is allowed with a full queue, and `parked-work` is
suppressed. For 24 hours after `off`, `next` prints `FREEZE LIFTED`. Both edges wake every role. A
freeze is a mode rather than mail because a freeze message has no end: measured 2026-09-27, two
lanes kept honoring one for a day after the window closed, idle on full queues.

**Host maintenance windows** (`shoal/maintenance.py`). A freeze stops a whole shoal; a planned reboot, reset or hang
test touches one host, and it cannot be guarded by a lease or a pool reservation, because each one is a held lock that
dies with the boot it protects. Measured 2026-11-07: a watchdog test's GPU hold ended at its first reboot, every lane
pre-check then passed (lease, pool, CI agent, gates, freeze), and a two-card job started 13 s before the planned reset.
`shoal maintenance on --server S --until <ISO|+90m> --note <why> [--wake]` writes two records:

- **a marker on the host itself** (`<data root>/_pool/maintenance.json`), which its reboots do not end. There, `pool
  reserve` refuses, the pool probe carries it so `pool status` and placement read the host as ineligible, and `job run`
  started on that host refuses;
- **a `maintenance:<server>` mode event, by `user`, in every shoal ledger of the clone it runs from.** `next` prints
  `HOST MAINTENANCE` on every host, `job run --on/--place` naming the host refuses, and health holds that host's
  `host-silent` page.

A window ends at `maintenance off` or at `--until`, whichever comes first, so a forgotten `off` does not hold a host for
good. A remote host's marker is set through its route, and the command refuses when that host cannot be reached. What a
window does not stop: work already running, and anything that does not go through shoal (the CI agent's own jobs, a raw
lease): a test still holds the host's leases, and takes them again after each reboot.

**Wait discipline** (`waitdiscipline`), enforced by `queue --wait` and `transition`:

- a `user-auth`, `user-design` or `user-feedback` wait is refused unless the queue records the ask;
- a `session-blocked` wait is refused without `--blocked-on <role>[:<repo#n>]` naming another
  role, and the blocker sees `BLOCKING` in its `next`. A wait on something outside the shoal is
  `upstream`, which needs a checkable target (`gh-issue:` or `gh-pr:`) and gets no periodic
  validity wakes: the quorum wakes the role when the target resolves;
- every wait target is normalized (`repo#n` or `repo:n` becomes `owner/repo#n`) and checked when
  written: a malformed target is refused, because nothing could ever check it, and so is one that
  is already resolved. `next` flags a target that has since resolved or cannot be checked, and
  notes when next items are parked behind a wait that covers only the active item;
- a `transition` that re-declares the current wait without `--target` keeps its target;
- with `waittargets`, a `local-ci`, `remote-ci` or `research-proto` wait needs a checkable target, and the role's
  `ledger-watch` wakes it when a `job:` or `lease:` target resolves. The wake comes on the second
  tick that sees the target resolved, so a session's own waiter acts first. Measured 2026-10-01: a
  detached job waiter reached no session, and the role slept for an hour past its job's end;
- `queue` refuses a finished item in `next` (`done` by any role), and `next` names it `GONE`;
- rescans are capped per substate (maintainer and peer waits 240 minutes, CI 180, research 240, `upstream` 720,
  `queue-empty` 360; overridable with `rescan_cap_min` in the spec);
- a passed rescan wakes the model only when shoal cannot prove the wait itself (`state.wait_proven`, used by
  health's `wait-expired`, `heartbeat` and the `next` line). A wait on a target is proven while its provider answers
  pending; a user wait while its ask is open in the latest queue and the maintainer has not written to the role
  since (the answer, or the return from offline, wakes it). Unknown is never proven, and nothing is proven past
  `proven_max_min` (default 1440) since the wait was written, so a sound wait still gets one look a day. `next` says
  `RESCAN PASSED ..., proven by shoal` instead of `RESCAN EXPIRED`. Recorded blockers follow the same rule
  (`blockers.check`): another role's open, takeable item and an upstream or lease target its provider answers
  pending stay live up to `proven_max_min`, rather than expiring after `peer_ttl_min`; a `hold:` still expires on
  its own age. Measured 2026-11-10: 178 rescans passed in 21 days, 87 on CI or research waits with a pending target
  and 77 on user waits with the ask open, each a model turn that wrote the same wait again;
- held claims in neither the active nor the next list are flagged `HELD-UNSCHEDULED`.

## 5. Work selection

**The board digest** (`digest`) is the only board read: a timer every 10 minutes plus
`digest --refresh` on demand, writing one file per group. For each open item it holds position,
repository, labels, a body excerpt, open blockers, parent and linked PRs, and per role a score
with its reasons and a rank that lets board order break ties. A pass with `suggest=<role>` lifts
the item for that role; a role never sees an item it passed on; items every self-filling role
passed are listed once as unowned. Watchers and `next` read the digest, never GitHub.

**Working context.** Each role's spec `context` holds a `brief` (two to four sentences, the input
to the semantic matcher), optional `affinity` weights, and `never`, the only hard exclusion.
Contexts are edited as files in `<data_root>/<id>/contexts/<slug>.json` and loaded with
`context load` or `context set`.

**The semantic matcher** (`matcher`) scores every open item 0 to 3 against each lane's brief and
classes it `workable`, `needs-maintainer`, `blocked` or `parked`. Results are cached per item
content and lane brief, so a quiet board costs no model call. Model routes are tried cheapest first:
free models through `opencode run`, then the configured subscription fallbacks (`go_fallbacks`),
then an OpenAI-compatible paid fallback (`paid_fallback`, keyed by `SHOAL_MATCHER_API_KEY`). While a
paid route is active the free tier is re-probed every `free_recheck_min`; a switch is warned,
recorded as a ledger `system` event and paged at most once a day. With `live: false` the semantic
ranking is written beside the live digest and `digest --compare` measures it against the ledger's
own history; `upgrade --enable matcher.live` switches without a restart. `private_repos:
"withhold"` keeps private-repository items out of model calls.

**Keyword ranking, the fallback.** While the matcher is off, or in shadow mode, the live digest
ranks by the role's optional affinity weights (repos, labels, keywords, path prefixes), with board
order breaking ties. A role with no weights ranks by board order alone. With the matcher live,
affinity weights are not used.

**Effective priority** (with the matcher) is the order the digest ranks by: an item that blocks an
active role's work is lifted above it, an item named by a peer's `--blocked-on` wait goes to the
top, and an item with an open blocker sinks below every workable item.

**The residual lane** is an optional `fill=self` role with `aux: true` and no brief. It takes free
workable items that no active lane fits strongly (fit 2 or more), unblockers first, and `next`
names the lanes it stays out of.

**Fill modes.** `fill=self` roles pop the board when their queue is empty; `fill=assigned` roles
work only offers to them or to `*`; `fill=none` roles never claim.

## 6. Watchers and where work runs

| type | runs as | wakes | detected dead by |
| --- | --- | --- | --- |
| `ledger-watch` | setsid-detached (Claude: `--wake direct`; opencode: `--wake send:<own id>`; muse/codex: `--wake exec:<own id>`), or an in-session background command (Claude under `direct_wake: dead`: `--wake exit`) | its own session, for work only | sidecar pid; health `watcher-dead` |
| wait waiter (CI, lease, job) | an in-session background command, head-pinned, with verdict lines | its own session | a missing verdict; health `wait-resolved`, `wait-validity` |
| job unit and `job wait` | a `systemd-run --user` transient unit with a `MemoryHigh` cap | its own session, through the waiter | `JOB-VERDICT` line; unit state; on another host, `HOST-REBOOTED` when its boot id changed under the job |
| `health-a`, `health-b` | systemd user timers | roles (through adapters), the maintainer | each checks the other's tick |
| lanes, pipeline, digest, reconcile, weekly report | systemd user timers | the health adapters (sensor fires) | `/tmp/<id>/tick-<job>` older than three intervals |

Every watcher wakes only its owner (or routes through the health adapters), logs a verdict line on
every designed exit, and names every terminal state in its predicate: a cancelled run is tolerated
only when superseded, and an empty query is an error, never green.

**Placement.** Anything whose loss costs work runs as a job unit (`job run ... -- <cmd>`) followed
by a `job wait` background waiter, because a harness may reap background commands and a job unit
survives that. With `--on user@host` (or a `[hosts.<server>]` name, whose route's ssh options then apply) the same unit runs on another host over ssh (the unit, log and rc
file under `~/shoal-jobs` there; the local meta records the host and `wait`, `status` and `stop` follow it
over ssh; the remote cwd, checkout, venv and any lease wrapping are the caller's to have in place: this
moves compute, not state), which is how a serialized local resource (`cpu-heavy`, the one big GPU) is
relieved by an idle second host without moving any role. Watchers stay small and idempotent: re-arming
never duplicates a side effect. A
`killed` notification is itself a wake: the role runs `next`, acts, re-arms and proves the new pid.

### Pool placement (`job run --place`, `shoal pool`)

The pool is this host plus every `[hosts.<server>]` route (section 8), less any host left out with
`pool = false`: on a route, that host is never probed from this side (a route's `cli` can point at a
pre-release checkout on that host for a trial before rollout: operator cheat-sheet); at the top level of a host's own
config, the host is never a candidate anywhere (its probe answers `pool: false` at once, and it takes
no reservations). `pool status` names the hosts outside the pool. `job run --place auto|<host>`
chooses where a job runs by predicted queue wait, stages it there and wraps it in that host's lease
helper; without `--place` a job runs exactly as above. `shoal/pool.py` is the pure planner
(unit-tested on synthetic N-host fixtures); `shoal/pool_cli.py` does the I/O.

- **Probe.** `shoal pool probe --json` describes one host: the lease helper's `--status --json` (the
  provider contract), the newest resource sample's devices (vendor, VRAM, an arch tag such as `sm_89` or
  `gfx1100`) and memory (a live read when no sample is fresh), cores, `ci_envs`, `worktree_root`, the
  lease helper's path, its holders' observed durations from the resource stream, and the probe time.
  Home runs it in process and over each route's ssh (20 s timeout, cached 20 s). A failed or stale
  probe (older than 120 s by home's receive time, so a skewed remote clock does not age it) makes the
  host ineligible, never an error; its reason is one line (a shoal too old to know `pool probe` reads
  `shoal on <host> has no pool probe (update its checkout)`).
- **Demand.** `--needs cpu|gpu[:N][:vendor][:vramGiB]` (a GPU job with no vendor keeps its home host's
  vendor; above one GPU is the whole host), `--cpu-heavy`, `--memory-gib`, `--ci-env NAME`,
  `--repo PATH [--ref SHA]` (default the checkout's HEAD), `--est MIN` (the duration prior),
  `--priority`, and `--place <host>` to pin. A host other than home needs a SHA on a remote-tracking
  branch and a clean tree (or an explicit `--ref`); otherwise only home is eligible.
- **Estimates.** p50 and p90 per (repo, normalized job name, host), learned from
  `<data_root>/_pool/history.jsonl` (one record per finished placed run, grant to exit) and shrunk toward
  `--est`; a running job's remaining time is `max(p50 - elapsed, tail)`, the tail growing once elapsed
  passes p90. Holders SHOAL cannot move (a CI agent) are estimated from their own observed lease
  episodes on that host.
- **Plan.** The host's lease queue is modelled as the helper runs it: FIFO tickets taking everything
  they want at once, a later ticket never passing an earlier one it overlaps. Pending jobs are list
  scheduled in (priority, submit time) order, each to the compatible host with the earliest predicted
  finish (staging, 120 s by default, added off home); home is kept when it finishes within the margin
  (180 s) of the best. The objective is the sum of predicted waits, plus staging per transfer.
- **Execution.** Off home, the target fetches the repository's origin into a mirror under its
  `worktree_root` and adds a detached worktree at the SHA; a SHA the remote does not have is refused
  (source is never copied). On home the job is staged the same way when it named its commit with
  `--ref`, so it tests the same commit wherever it lands; without `--ref` it runs in the `--cwd` or
  `--repo` checkout as it is. A successful run's staged worktree is removed at the verdict; a failed
  one's is kept for inspection. The command runs as an ordinary job unit (`--on` for another host) inside
  `<lease helper> <scope> --`: `--lease cpu-heavy` for CPU work that asked for it (a bare `--cpu-heavy`
  would also take every GPU), `--gpus 1 [--min-vram G]` or `--gpus all` for GPU work, with
  `GPU_LEASE_PROJECT=<unit>` so the job's waiter and hold are recognizable. Inside the lease, before
  the command, the job takes its start claim (a directory: a move that took it first cancels this
  start), writes a grant-time marker, exports `SHOAL_JOB_OUT` (a per-job output dir), the CI
  environment (`SHOAL_CI_ENV`, `SHOAL_ENV_PYTHON`, its bin dir on `PATH`) and, for CPU work, empty
  `CUDA_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES` and `HIP_VISIBLE_DEVICES`.
- **Record.** The job meta keeps the placement (hosts considered with their predicted starts, the
  choice, reason, SHA, lease scope, demand, submit time) and `lease_granted`, the marker's ISO UTC
  time, so time before it is lease wait and time after it is the job's own. `job wait` records the
  grant when it sees it, and at the verdict copies a remote job's `SHOAL_JOB_OUT` back to
  `<data_root>/<id>/jobs/<unit>.out`, appends the run to the history and removes a successful run's
  worktree. Each placement is a `placed` telemetry event.
- **Job class and runtime budget.** `job run --class ci|research` (default `ci`, placed or not). A
  `ci` job has a runtime budget, `features.placement.ci_budget_min` (default 90; 0 turns it off),
  counted from `lease_granted`, or from the unit's start when no lease is used. Past it, `job wait`
  prints `JOB-OVERRUN`, emits an `overrun` event and pages once per run: a ledger `health` msg to the
  owning role and the user page path (`health.page_user`). It never kills the job. The planner uses the
  budget as a soft ceiling on a CI job's predicted end (within the budget the prediction ends by the
  budget plus the tail floor; past it the ordinary overrun tail applies). A `research` job has no budget.
- **Research waits.** A role waiting on a research or prototyping run declares `research-proto`, not
  `local-ci` or `remote-ci`. It is followed like a CI wait (a checkable target, the watcher's wake when a
  `job:` or `lease:` target ends, validity wakes, a 240-minute rescan cap), but role time counts it as
  `wait:research-proto` in its own `research` group, whatever it waits on (a lease included), so CI time
  and CI efficiency leave it out. A CI wait on a `--class research` job is refused; a `research-proto`
  wait on a `ci` job gets a note to run research as `--class research`. Like `remote-ci`, it does not
  excuse parked next items.
- **Research windows.** Research that is scheduled but not yet running (an overnight GPU window, a reservation
  to open) is `research-window`, on a `time:<ISO UTC>` target (the `time` provider: pending until then) or on the
  queued `job:` or `lease:` it waits for. It needs a target, it is counted in role time's `research` group beside
  `research-proto`, and a `time:` target on any other substate is refused. It replaces the timer job (a `job run`
  sleeping until the window) that read as `local-ci` and was re-verified every 3 h (measured 2026-11-10: 4 to 6 turns
  a night for one role).
- **Research reservations** (`shoal/pool_reserve.py`). `shoal pool reserve [--host H] --resources
  gpu@<uuid>,...|gpus:N|cpu-heavy --for <shoal>/<role> --until <ISO|job:NAME> [--note]` takes the
  leases on H with the helper's background-holder mode (`--hold --pidfile`): one hold per device (a
  named device is the helper's only candidate, through `GPU_LEASE_DEVICES`; `gpus:N` takes N best-fit
  devices) and `cpu-heavy` as its own hold, so one GPU can stay with research while the rest of the
  host stays in the CI pool. Each reservation is recorded in H's `<data_root>/_pool/reservations.json`,
  reported by `pool probe` and `pool status`, and is a `reserved` event. `shoal pool release <id>
  [--host H]` releases it, and so does `--until` (a time passed, or the owner's job of that name ran to
  an exit code on H), checked by every probe and every `resources watch` sample; a reservation whose
  holder died is dropped. The planner takes reserved devices and a reserved `cpu-heavy` out of what a
  CI job may use: a host whose fitting GPUs are all reserved is ineligible, a whole-host request is
  ineligible on a host with any reservation, a CI job wrapped for such a host only sees the unreserved
  fitting devices (`GPU_LEASE_DEVICES`), and a queued job whose host a reservation took is moved when
  another host can run it. CI never queues behind a research hold. A `research` job placed with
  `--place` must run inside a reservation its role owns (`--reservation <id>` picks one of several),
  on that host, with no lease of its own and only the reservation's devices visible
  (`CUDA_VISIBLE_DEVICES` for NVIDIA, `ROCR_VISIBLE_DEVICES` for AMD); without one it is refused, and
  it never moves.
- **Queue transfer** (`features.placement`, off by default; `--place` works without it). While a
  placed job is still queued for its lease, its `job wait` (one per job holds a supervisor lock; re-armed
  copies only wait) re-plans every `interval_s` with this host's other queued placed jobs. On advice
  (another fresh, compatible host would start it sooner by more than the margin, staging included, and
  it has moved fewer than `max_moves` times) it takes the start claim where the job waits (so it can
  never start there), stops that unit (TERM ends the waiting lease request, which removes its ticket),
  restages and resubmits on the target with the original submit time kept, records a `moved` event and
  follows the job there. A job whose claim is already taken is running and never moves. `shoal pool
  status` shows each host's capacity, holders, queue and predicted drain; `shoal pool plan` shows the
  plan and the transfers it would advise, and never moves a job itself.

## 7. Health quorum (`health`)

Two identical timers run `shoal health`, `health-a` at `*:0/5` and `health-b` at `*:2/5`, sharing
one state file under a lock so whichever runs first acts. Each run ticks and checks its peer's
tick, and pages the maintainer once if the peer is silent past 15 minutes. It replaces the
heartbeat daemon.

A condition instance is keyed by its evidence (offer id, dead pid, target, last boundary). Each
run takes at most one step per instance, and a cleared condition resets its ladder.

| condition | evidence | ladder (minutes) |
| --- | --- | --- |
| `unacked-work` | an unacknowledged operational request in any posture, an offer to an idle role, a `--now` offer, or an outstanding wake request | wake 10, wake 30, page 60 |
| `wait-resolved` | a checkable wait target is no longer pending | wake 15, page 60 |
| `wait-validity` | any CI, lease or job wait (not `upstream`, and under wait discipline not one whose target is confirmed pending), every `validity_wake_min` (default 90); the ladder is keyed by the target, so re-declaring the same wait keeps escalating | wake, wake, page, one interval apart |
| `watcher-dead` | the `ledger-watch` pid is gone and the session is between turns, and (`lazy_rearm`) the role is active or has work its watcher would deliver | wake 10 (a detached watcher: re-armed by the quorum instead), page 40 |
| `turn-ended-active` (`continuation`) | posture `active`, session proven between turns | wake at `after_min`, wake 30 later, page 90 later |
| `checkout-stuck` (when `watch_checkouts` lists it) | a shared checkout has an operation left in progress, a local file differing from one its upstream changed, or commits on both sides; behind alone is not stuck | page 60 |
| `stale-active` (when `continuation` is off) | posture `active`, no ledger write, session between turns | wake 90, page 180 |
| `wait-expired` (`waitdiscipline`) | a waiting posture past its capped rescan | wake 0, wake 60, page 120 |
| `blocking-unscheduled` (`waitdiscipline`) | a peer's `session-blocked` wait names this role's item and the item is not in its queue | wake 30, page 120 |
| `parked-work` | idle, or waiting on anything but `local-ci`, while a next item has no recorded blocker, or (`blockers`) only a stale one (not `fill=none`, not during a freeze); keyed by the items | wake 30, wake 90, page 180 |
| `blocker-resolved` (`blockers`) | as `parked-work`, for a next item whose `<role>:<repo#n>`, `upstream:` or `lease:` blocker cleared (not one that expired, not an `ask:`); keyed by item, blocker and when it was recorded. Not held while offline, since it needs only the role. The wake asks the role to look (mail, the item's notes) before starting: a cleared blocker is not by itself a go | wake at sighting, once; `parked-work` escalates if the role does nothing |
| `asks-on-return` | the maintainer came back (`user-offline` off, 3 min to 12 h ago) and a role holding asks has not written since; keyed by the return. `offline off` wakes only the roles on the host it ran on, so each host's quorum wakes its own; not held by the settle window | wake, once a return |
| `hold-long` (`blockers`) | a `hold:` blocker kept past `hold_page_min` since it first blocked the item; keyed by item and first time | wake at sighting (convert it to an ask or a checkable blocker), page 240 later |

A role waiting on the maintainer is woken only by `wait-expired`, and the maintainer gets one daily
digest page listing each role's open ask.

**Parked roles.** A role with no `session` in the spec (parked or retired) raises no condition, is
left out of the daily digest, and reads `parked (no session)` in the heartbeat classification;
`doctor` reports its heartbeat and `ledger-watch` rows as INFO, never WARN or FAIL, whatever a
leftover sidecar says. `shoal park` makes a role so on purpose (`park.py`): it stops the session, ends its
watchers with SIGKILL (no exit path can deliver a wake to the stopped session) and records `parked` {at, by,
note, until} beside the empty session, which `show` prints. A full restart keeps a parked role parked; naming
it (`restart --only`, `unpark`) relaunches it and clears the record. `shoal remove-role` takes a role out for good (stops its session and watchers, prunes its incarnations
on every harness, withdraws the offers addressed to it so those items can be filled again, releases its claims so they stay
gone if the slug ever returns, removes it from the spec, writes
`role-removed`; the ledger keeps its history), and `shoal remove-session`
does the same for a saved standalone session. A retry after the harness already removed it (a Claude job gone from
`claude agents` and from its jobs directory) succeeds and forgets the entry; a refusal names the worktree that keeps
a Claude job. `shoal handoff` hands a live role to a fresh session
in place, optionally on another model or provider (the quorum's `context_restart` run by hand, under its gate: see
[`handover.md`](handover.md)). `shoal set-fill --only <role> --fill self|assigned|none` changes how a role gets its work
without a restart: it saves the spec, records a `role-fill` event, and wakes the role with what the new fill means, since
its brief keeps the old fill until its next restart. A running watcher reloads the spec when the file changes, so a
self-filling role's board wakes follow at its next tick. The aux lane always fills itself. A `parked.until` wait target is checked
by the quorum each run, outside a freeze: once its provider reads it resolved (unknown never counts), the role
is restarted, at most once per 30 minutes.

**Timers in `doctor`.** Each `shoal-<id>-*.timer` is judged by its own `ActiveState`: `active`
(sub-state `waiting`, `running` or `elapsed`) is good, so a timer whose service is activating or
has just run never reads inactive. An inactive or failed timer is a FAIL; an active but disabled
one is a WARN.

**Direct session wake.** Every wake the quorum or a sensor decides goes through one interface,
`Backend.wake(spec, role, text)` in `shoal/backends.py`, which returns the channel it used. Each
harness implements it in its own way:

| harness | watcher alive | watcher dead |
| --- | --- | --- |
| opencode | a direct send to the session | the same |
| Claude Code | a wake-request file `/tmp/<id>/wake-<slug>`, delivered by the role's own `ledger-watch` and acknowledged by its next `queue` write | a direct wake from the quorum (below) |

Claude needs a wake path of its own because a headless send to a running Claude session forks it
instead of waking it. A harness with no implementation returns `no-adapter`, and the condition
escalates to a page. Adding a harness means adding its `wake`, not changing the quorum.

**Direct Claude wake (`health.direct_wake`), the only Claude wake path.** A Claude session with **no
worker** is woken with no other session's turn: `claude --bg --resume <session> "<text>"` continues it
under the same id, with its saved options, and the text as its prompt (verified 2026-10-20; the prompt
cache is mostly kept). It forks a copy only while a worker is running, and shoal stops and removes any
copy that appears. Modes:

| `direct_wake` | a session with no worker | a live session idle at its prompt, nothing in flight | anything else (mid-turn, at a question, background work in flight) |
| --- | --- | --- | --- |
| `idle` (default) | resumed directly | stopped, then resumed directly | deferred: delivered at a later tick (work in flight wakes the session itself when it ends) |
| `dead` | resumed directly | deferred (a live worker is never stopped) | deferred |

Under `idle` a Claude watcher is detached (`--wake direct`) and delivers each wake through the same
path, so it outlives its worker usefully: a session the background service retired after 8 h idle is
**dormant**, not dead, and its next work resumes it. Under `dead` the watcher is an in-session
background command (`--wake exit`) whose exit wakes the live session. Direct wake is always on: any
other value, including the retired `off`, reads as `idle`. `--wake relay` (the retired wake relay's
mode) is read as `direct`, so a watcher armed before the retirement, or one that reloads itself in
place after an upgrade, keeps delivering with no re-arm. The wake relay (a `wake-relay` Claude role
whose `relay-watch` delivered wakes as cross-session messages, two turns per wake) is retired
([`decisions.md`](decisions.md)); a spec that still lists its role, `features.relay`,
`health.relay_fallback` or `health.revive` loads: the role is dropped at load and the keys are ignored
(a workerless session is resumed on its next wake, so `revive`'s respawn has nothing left to do). To
keep a role down, park it or mark it `offline`: a dead session alone is woken by its next work.

### When roles wake, and what that costs

A role spends tokens only on a turn, and every turn re-reads its context: cheaply from cache when warm,
as a cache rebuild the size of the context when cold. So the measure that matters is **target turns**.
Free: ledger mail without `--wake`, ledger reads, `status`, the UI, pages, watchers polling. A turn: a
watcher's work wake (an offer, a handoff, a user `--wake` message, a resolved wait target, board
candidates for a self-fill role), the role's own background command ending, a health wake, a
cross-session message, a maintainer prompt. A direct wake is one turn (the role's own).

The quorum's wakes follow these rules (all under `features.health`):

| key | default | effect |
| --- | --- | --- |
| `lazy_rearm` | on | a dead watcher on a role that is not active and has nothing its watcher would wake it for raises no `watcher-dead`: its next real work raises its own condition |
| `rearm` | on | `watcher-dead` on a **detached** watcher (Claude `direct`, opencode `send:`, muse/codex `exec:`) is answered by the quorum starting the watcher itself, no wake; only an in-session (`exit`) watcher needs its role woken. A watcher re-armed within 15 min that is dead again climbs the ladder. Each arm counts fetch failures from zero, so a re-arm during an outage gets its full five attempts |
| `autopark` (a top-level feature) | off | a `fill=assigned` role holding nothing and quiet for `after_min` (default 60; `true`, or `{"after_min": 60, "exempt": ["<role>"]}`) is parked by the quorum `--until offer:<role>`, and the next offer to it relaunches it from its start doc. So an empty-queue episode costs at most that grace of `wait:assignment`, and parked time is excluded. It stays out for a queue, offer, claim or ask, a posture other than idle, a message or session activity newer than the grace (direct chat writes nothing to the ledger), a turn, a `blocked` Claude row (a request may sit in its job's `needs`), an attached client or interactive session (`claude attach` for Claude, `session_held` for the others), a running job, a condition raised this run, an incomplete start doc, or a freeze. At most one park per quorum run. Only an offer relaunches: a `msg --wake` to a parked role does not. A park or relaunch changes the shoal's viewable roles, so the app rebuilds its roles tabs |
| `watch_checkouts` | off | paths of shared checkouts this host's quorum watches for `checkout-stuck`, each fetched per tick. List them on one spec, or each quorum pages about the same checkout. A path that is not a checkout on a host is skipped there |
| `offline_hold` | `parked-work`, `hold-long`, `blocking-unscheduled` | these do not wake while the maintainer is offline (the step waits; pages were already deferred) |
| `rewake_min` | 720 | `parked-work` and `hold-long` wake once per item set or hold in this window; a re-opened instance on the same evidence is skipped |
| `online_settle_min` | 20 | no health wake for this long after the maintainer comes back (`offline off` already woke every role holding asks) |
| `wake_gap_min` | 30 | at least this long between two health wakes of one role |
| `wake_off` | none | conditions that never wake, only page (for example `["parked-work", "hold-long"]`) |
| `stale_upkeep` | on | respawn an idle Claude worker that runs an older Claude Code than the installed one (no turn); report a busy one once per version. An old worker's updater keeps reinstalling the current version, and each reinstall restarts the background service and drops attached clients ([`handover.md`](handover.md), "Related upkeep") |

And every run sends **at most one wake per role**, carrying every condition then due.

**The wait gate (`features.waitgate`, default on).** `queue --wait` other than `local-ci` is refused
while a next item has no current blocker, exactly like `queue --idle`. Refused mid-turn, it costs
nothing; caught later by `parked-work` it cost a cold wake. `parked-work` remains for a recorded
blocker that goes stale afterwards, and with `wake_off` it pages instead of waking (a blocker that
cleared still wakes the role, through `blocker-resolved`, unless that is listed too). `waitgate: false`
restores the old write.

### Bounding context

A cold wake costs about the size of the session's context, so a long-lived lane is handed to a fresh
session at a quiet boundary rather than carried forward (`shoal/context.py`).

| key (`features.health`) | default | effect |
| --- | --- | --- |
| `context_restart_k` | 300 | the context size, in thousands of tokens, past which a role is advised to hand over; 0 is off. A dict sets it per role, with `"*"` as the default: `{"*": 300, "<slug>": 0}` exempts a session that needs a large context |
| `context_restart` | `page` | `page`: the quorum pages the operator to restart a restart-ready role. `auto`: the quorum stops the old session and its detached watcher, then runs `restart --only <role>`. While a client is attached to a Claude role's session (a card's `claude attach`), the restart is held rather than tried: an attached client keeps the worker alive, so the stop would fail and the ladder would move on to its page with no retry. It restarts once the client detaches, and pages once if the hold lasts 30 minutes. A dict sets it per role: `{"*": "page", "<slug>": "auto"}` |

1. Past the limit, `next` and every wake carry a `CONTEXT <n>k (limit <m>k)` line (`next` records when the session was first
   shown it). It asks the role, at its next boundary
   with nothing mid-flight, to refresh its start doc (current state, decisions taken, next steps) and end with
   `queue --idle --restart-ready`, or with its wait plus `--restart-ready`. No extra wake is spent on it.
2. `--restart-ready` is refused unless the start doc meets the contract and its `Last updated` stamp is at or after that first
   advisory ([`start-doc.md`](start-doc.md)). A restart-ready role between turns raises `context-restart`, page or restart as
   configured, keyed by its session. A later queue write without the flag clears it.
3. The new incarnation reads that start doc: the refresh is the handoff.
4. The overview row carries `context_k`, and `context-large` is an informational reason.

Today only Claude reports its context (the transcript's last `usage`).

**Pruning old incarnations** (`shoal/prune.py`; [`handover.md`](handover.md#pruning-old-incarnations)). A restart or
handover stops the old session but leaves it in its harness's listing, so retired `<slug>-r<N>` sessions pile up there
(and a Claude job keeps its `tmp/`). `Backend.prune(session, job, mode)` removes one: Claude writes a tombstone of the job
record under `~/.claude/archived_sessions/jobs/<job>/`, runs `claude rm` (the transcript stays), then re-runs the `retire`
re-stop check on every job sharing the conversation; Codex `thread/archive` (`thread/delete` with `--delete`); opencode
OpenChamber's archive flag over its HTTP API (opencode's `DELETE /api/session/<id>` with `--delete`); Muse moves the
session directory to `archived-sessions/`. `prune.py` decides what is safe: only sessions shoal created (a `<stem>-r<N>`
name or an id in a spec's generation history; standalone predecessors and `shoal-matcher-*` sessions on request), and
never one that is (a) a current or (b) parked role session in any spec, (c) in the viewer registry (by the session
and job ids an entry records, by label only for an entry that records neither), (d) live, (e) on a
conversation a live or protected job shares, (f) holding an existing worktree or a process's cwd, (g) the job the prune
runs in, or (h) locked or bound to a ledger-watch, nor anything younger than `--min-age`. With `features.prune` the
role's earlier incarnations (N-1 and before, on every harness) are pruned once incarnation N proves itself (its
`first-turn` handover event, run detached, or `verify_consumer` in `restart`), and a park prunes nothing. A job whose folder
a start doc names is kept (a role being removed: its own doc does not count), and `queue --restart-ready` refuses a doc that
names one still on this host and notes one that is not. Each removal is a
`pruned` telemetry event. The matcher deletes its own session after every run.

**Pages.** A page is always a ledger `msg` to `user`. `features.health.page` names where else it
lands, as one channel string or a list of them: `"gh-issue:<owner/repo>#<n>"` also comments on
that issue (the inbox, reaching the maintainer through GitHub notifications), and
`"ntfy:<target>"` also publishes to an ntfy topic (a full URL, a `<host>/<topic>`, or a bare
topic on ntfy.sh; an access token rides `$SHOAL_NTFY_TOKEN`, never the spec), which reaches the
maintainer's phone through the ntfy app. Pages are deferred while the maintainer is offline.

The lanes and pipeline sensors route through the same adapters: pipeline starvation wakes the
roles waiting on remote CI, and a harness server respawn wakes every opencode role.

### Bounded operational requests

`msg --kind request --to <role>` records an acknowledgment-required operational request to one
live role. It does not assign project work or grant authority. Plain mail stays non-waking. The
existing `unacked-work` health ladder tracks a request even when its recipient waits on a long CI
job, with the same bounded wake/page policy as offers. The recipient answers with a message linked
by `--re <request-id>` before returning to a long wait, including when the action needs its own
maintainer grant. Only a response from that recipient clears it: a mail cursor, queue write or
third-party response cannot. No new timer, health condition or persistent flag is introduced. A matching `job wait` monitor
also yields at the role's declared rescan deadline, with exit 75 and `RESCAN-DUE`; the job unit
continues. This frees the session to read retained wakes, revalidate the wait and re-arm the monitor,
instead of keeping a background task in flight indefinitely. Local and SSH job monitors share this
rule. A completed job still reports its actual verdict before the rescan check.

## 8. Host configuration and providers

Every host-specific value resolves in one order: an environment variable, then
`config/host.toml` at the repository root (gitignored; `SHOAL_CONFIG` points elsewhere), then a
generic default.

| key | environment | default |
| --- | --- | --- |
| `data_root` | `SHOAL_DATA_ROOT` | `$XDG_DATA_HOME/shoal` |
| `ledger_dir` | `SHOAL_LEDGER_DIR` | `<data_root>/ledger` |
| `ledger_repo` | `SHOAL_LEDGER_REPO` | none (`start` and `ledger-init` then need `--ledger-repo`) |
| `tool_path` | `SHOAL_TOOL_PATH` | the current `PATH` |
| `lease_cmd` | `SHOAL_LEASE_CMD`, then `GPU_LEASE_CMD` | none (lease waits resolve as unknown) |
| `default_repos` | (file only) | `[]` |
| `cli` | `SHOAL_CLI` | `shoal` when installed, else `python3 <package>/cli.py` |
| `psi_out` | `SHOAL_PSI_OUT` | `<data_root>/_instruments/psi_events.jsonl` |
| `server` | `SHOAL_SERVER` | the host name |
| `timezone` | `SHOAL_TZ` | `$TZ` or `/etc/timezone`, else UTC |
| `default_owner` | `SHOAL_DEFAULT_OWNER` | none |
| `azure_org`, `azure_project` | `SHOAL_AZURE_ORG`, `SHOAL_AZURE_PROJECT` | none (the pipeline sensor needs them) |
| `worktree_root` | `SHOAL_WORKTREE_ROOT` | `<data_root>/_pool/worktrees`: where placed jobs are staged on this host |
| `[ci_envs.<name>]` | (file only) | none: CI environments this host offers placed jobs (`python`, `repos`) |
| `pool` | (file only) | `true`; `false` keeps this host out of CI pool placement (a route's own `pool = false` leaves that host out from this side) |
| `skill_homes` | `SHOAL_SKILL_HOMES` (path-separated) | the existing standard homes: `~/.claude/skills`, `~/.agents/skills`, `~/.config/opencode/skills` |
| `[tool_env]` | an exported variable of the same name wins | none: extra variables for every tool call and generated systemd unit (e.g. `OPENCHAMBER_DATA_DIR` for a server kept apart from a desktop app) |

`doctor` prints every resolved value, the host profile (the harnesses of the roles that run here) and
the clock state. The providers (resource leases, wait targets, the board
source, harness adapters) and their contracts are in [`providers.md`](providers.md).

**Several hosts.** One shoal's roles may run on several hosts that share the ledger remote; the remote
is the only coordination path (no host-to-host connection is needed for correctness).
- **Locality.** The spec's `home` names the host for group duties; each role's `server` names its host,
  empty meaning home; an empty `home` is a single-host shoal. `roles.is_local(role, spec)` compares with
  config `server` and is the one test: health evaluates, sensors wake and `request_wake` delivers only for
  local roles, and `restart --only` and `aux-launch` refuse a remote role and print the
  `ssh <server> shoal ...` line. `start` and `add-role` record a remote role and launch only this host's
  roles; a full `restart` on a multi-host shoal restarts this host's roles. Mint on home, then launch each
  remote role on its host. A remote role is watched, woken and launched by its own host. A `next --peek` from
  another host names the role's host for its watcher instead of reading that host's stale or absent sidecar.
- **Shared files.** Role files have one writer. `user` and `shoal` are written from any host, so the
  append-only writer files merge by union: `ledger.sync_pull` ensures the rule in the clone's
  `.git/info/attributes` (in force for every rebase) and commits it to `.gitattributes`.
- **The spec** is JSON and cannot merge by union: `cli.persist_spec` pulls under the clone's lock, applies
  only its own changes (the spec as loaded, `ShoalSpec.base`, to as saved: `merge_spec`, roles by slug and
  field, generations by `gen`) onto the fresh copy, and on a rejected push undoes only its own commit and
  merges again. A key both hosts changed keeps this host's value and is printed as a conflict.
- **Host names are labels, not identities.** A host's `server` (config, default its host name) is the name
  roles and beats use; nothing authenticates it (push access to the ledger remote is the only authority).
  It must be unique among the hosts of a shoal, or two hosts treat the same roles as local. That is
  detected, not prevented: every beat carries a hash of the machine id, and a host that sees its own
  name beaten by another machine raises `host-name-clash` (a page: rename one host). Role slugs are
  unique per shoal by spec validation, since a slug is a ledger writer file.
- **The overview** shows `home` and, on a multi-host shoal, each host's last beat in the header (the contract's `hosts`, empty on a single-host shoal) and a
  `host` column in the role table; the Herdr sidebar gets a `$host` token (empty on a single-host shoal).
  **One view for every host.** A session resolves only through its own host's harness (`claude agents`,
  its OpenChamber), so a viewer attaches a remote role by running that host's `shoal view` over ssh. Routes
  are private host configuration (`[hosts.<server>]`: `ssh`, optional `resolve`, `ssh_opts`, `cli`), never
  in a spec, and the ssh key is the only authority. A MagicDNS name is resolved with `tailscale ip -4` by
  default, so a host whose resolver does not accept tailnet DNS still reaches it. With a route, a remote
  role gets a card and `shoal view` attaches over ssh (a fast failure retries every 30 s rather than taking
  an exclusive attach over); `doctor` checks each route. Without one, `shoal view`, a stale card and the
  asks popup refuse to try (answer a remote role by mail).
- **Clocks.** Events are stamped by the writer's clock, the fold orders by them and conditions are ages:
  `doctor` fails a host that is not NTP-synchronized.
- **Per host:** `install-systemd` installs this host's set (health and lanes for its roles; the pipeline
  sensor, digest, reconcile and report on home only); every host with a live role writes a `host-beat`
  system event and home pages `host-silent` for one that stops; a Claude role's detached watcher runs
  on its own host and resumes its session there; `upgrade` off home leaves the spec's tooling and
  features to home. Still open: telemetry merge for reports ([`limitations.md`](limitations.md) section 2). The providers (resource leases, wait targets, the board
source, harness adapters) and their contracts are in [`providers.md`](providers.md).

**Harness-specific verbs**, the only non-neutral surface:

| verb | Claude Code | opencode |
| --- | --- | --- |
| start a session | `claude --bg` with the brief | a server session, the brief as its first prompt |
| wake own session from its watcher | the background command exits (task notification) | a send to its own session id |
| session wake (`Backend.wake`) | wake-request file, else a direct wake (`claude --bg --resume`) | a send |
| liveness, between turns | `claude agents --json` turn `state` (`done` between turns, `working` mid-turn, `blocked` on a question); never `status`, which reads `busy` while any background task (the role's own `ledger-watch`) runs | the server's session status |

## 9. Telemetry and reports

- **Streams:** `session_transition` events from `queue` and `transition`; `watch_event`s for
  ledger wakes, health actions (condition and ladder step), job starts and verdicts, pool placements
  (`placed`, `moved`, `lease-granted`, `overrun`, `reserved`, `released`, each `placed` and `moved` carrying the hosts considered and
  their predicted starts), and digest refreshes; `handover` phases; one `pruned` event per removed incarnation (in the owning group's telemetry, or
  `<data_root>/_host/telemetry/` for standalone and matcher sessions); reconcile ground truth from GitHub (timer at 04:07 and 16:07); and the ledger itself.
- **Pin:** each event carries a `run` pin: the protocol skill's blob sha, the scripts' shas and the
  schema version, read from the checkout the package runs from.
- **Identity:** time in state is keyed by role slug; anything unresolvable counts as one
  `unresolved` bucket, never a row.
- **Reports** (`report`, and a weekly timer): outcomes, time in state, transition quality, wait
  discipline, wake efficiency, ledger economics, health actions, job units, digest refreshes, host
  memory-pressure fires and harness reaps, and a verdict section. Time in state there is ledger
  posture time, not work time (see below).

### Role time accounting

`shoal/role_time.py` (`shoal role-time report`) charges every second of a configured foreground
role's membership to exactly one class and event: work, a non-idle wait, idle, unknown or excluded.
Average Active Roles (AAR) is work plus non-idle waits per elapsed second; Role Work Fraction is work
over eligible (non-excluded) role-time. Unknown time is never redistributed: reports carry lower
and upper bounds and coverage. The module is pure: adapters build observation snapshots from
host-local caches and the ledger clone, and it never fetches, probes a harness, calls a provider,
wakes a role or runs a model turn.

- **Work comes from the native turn state**, not ledger posture: the ledger records the posture a
  role sleeps in at the end of a turn (§4), so a turn inside an unchanged `waiting` posture is work.
  Ledger `active` with no turn running accrues no work. A positively identified blocking phase
  inside a turn is a wait; an open native question is `wait:user-prompt`.
- **Between turns the ledger classifies** (policy `role-time/v5.1`): waiting substates map to wait
  events (a `lease:` target wins over the nominal CI substate; a `local-ci` wait on a `job:<name>`
  started with placement whose lease has not been granted yet is `wait:lease`: the home job meta's
  `lease_granted` decides, which the role's `job wait` supervisor copies back for a remote job, with
  this host's grant marker as a fallback for a local one), explicit `paused` and `offline` are
  excluded, and an idle posture is classified by its queue. Any runnable item (no blocker, or a
  resolved or expired one under `blockers.py`) makes the role `wait:self`: it has work it is not
  doing. Only when every item is non-runnable is the role in a blocker wait, with the blockers'
  shared kind, `mixed`, or `unverified`. An empty queue (or `queue-empty`) names who owes the role
  work: a requested refill is `wait:refill`; a `fill: assigned` role is `wait:assignment`; a
  `fill: self` role whose recorded priority table is fresh and empty is `wait:sources` (both human
  waits); anything else stays `idle:no-work`. The table read is recorded by
  `worker.candidate_items` after a successful read (`<state_dir>/sources-<slug>.json`, believed for
  2 h), never fetched by the classifier. Park is excluded; freeze and user-offline are not
  exclusions.
- **Policy versions**: snapshots from a `role-time/v5` segment keep v5 semantics (without the two
  v5.1 rules above); snapshots read from a journal segment stamped `role-time/v4.1`, or from a
  journal written before segment headers, keep v4.1 semantics (`idle:ready-work`, `idle:no-work`
  for the cases above), so an old report reads the same. The observer closes a live segment
  written under another policy before appending, so a segment's header describes all of it.
- **Session-less roles** (v5.1): a role with no session recorded in the spec (not a missing process)
  whose declared posture is `paused` or `offline` is excluded; one that is idle with no session stays
  `unknown:session-missing`, so a role that should be running but is not stays visible.
- **Dormant Claude sessions**: a background job whose turn ended (`done`, no worker pid) while its
  ledger watcher is alive and armed for the same session is between turns, not missing; the probe
  reports it as `between` and the ledger posture classifies it. The watcher sidecar is host-local,
  so this is decided where the session runs and travels in the native cache.
- **Population exit**: a `role-removed` system event (written by the `shoal` writer) ends the role's
  membership at its `ts`; a role added back later keeps its later spans. A role that leaves the
  spec without that event gets a closing membership record at the first tick that no longer finds
  it. Membership is rewritten whenever it changes, and the last record for a role wins.
- **Freshness and validity are separate**: each source carries its last successful read and a
  budget; past it the time is unknown. Wait and blocker expiry change the attribution, not the
  coverage. Intervals split at every source deadline and blocker expiry.
- **Two clocks**: `account()` is the reconciled event-time view; `Accumulator` is the live
  observation-time view, which never backdates a late record (it counts it), zero-initializes every
  role, class and event series, checkpoints atomically, charges a restart gap to `unknown:gap` and
  starts a new epoch when its checkpoint is lost. Its Prometheus text carries no host or session
  label, so a handoff does not split a role's series.
- The report reads observation JSONL (segment, membership and snapshot lines; line order is append
  order for same-time records). `--input` takes the live journal, its directory or one segment: the
  live path reads every closed daily segment beside it (compressed or not) and then the live file.
  The classification policy version travels with every report, beside the policies stamped in the
  segment headers it read (`segment_policies`; journals written before rotation carry none).
- **Adapters** (`shoal/role_time_adapters.py`) build those records and stay read-only toward every
  role: no queue write, no wake, no model turn, no live provider call. `shoal role-time
  native-status --spec S` probes the sessions running on the current host and writes a per-host
  cache (`resources watch --role-time-spec S` refreshes the same cache each sample): Claude through
  `health.claude_liveness`, Codex and Muse through their exec lock plus durable-session-file
  activity, opencode through its status line. An attached but idle interactive client is never work:
  quiet reads as between turns, moving or unreadable activity is a raw state the classifier charges
  to unknown. `shoal role-time observe --spec S [--watch S] [--prom-dir DIR]` is the home-side
  integrator: it folds the ledger clone (freshness is the clone's last fetch, `.git/FETCH_HEAD`
  mtime, believed for 180 s; `--pull` refreshes it on an interval), runs `blockers.check(...,
  live=False)` with each blocker's expiry and evidence kind, merges local probes with any
  `--native-cache` files and with every other host's own cache (freshest source timestamp wins; a
  stale cache is unknown time, visible in coverage), appends observation JSONL under the shoal's
  state directory, and
  writes the accumulator's Prometheus text atomically with source-timestamp, eligibility and
  observation-error series beside the counters. Population comes from spec and generation history:
  `fill: none` roles are background unless `features.role_time.foreground` names them, and
  `features.role_time.background` forces a role out. For roles whose session runs on another host
  (`server`), the observer reads that host's native-status cache each tick over its `[hosts]`
  route (§12; `ssh <route> cat <state_dir>/role-time-native.json`, a file read, never a harness
  probe), keeps a copy beside the journal, and reports `shoal_role_remote_cache_ok{server}`; a
  failed read keeps the last copy, whose per-role timestamps age into unknown time. A host without
  a route is not read. `--no-remote` turns this off. The journal rotates at the UTC day boundary:
  the live file becomes `<stem>-YYYYMMDD.jsonl` and is gzip-compressed (about 37x on real journals),
  and the new live file opens with a segment header (`{"type": "segment", "policy", "opened"}`) and
  every role's membership spans in full, so the last membership record for a role wins when
  segments are read together. A journal written before rotation existed closes under the day of its
  first snapshot. Compressed segments older than `--retain-days` (default 365) are removed; a
  closed segment left uncompressed by a crash is compressed at the next rotation. The live
  accumulator follows current membership every tick either way.

### CI wait by repository (`shoal ci-wait`)

`shoal/ci_wait.py` measures how long each configured repository makes a change wait for CI, and why,
from three sources. Repositories come from `features.ci_wait.repos` in a spec; nothing in the code
names one.

- **GitHub Actions**: per workflow job, queue time (job created to started) and run time, with the
  attempt, from `gh api` (the runs list is read one UTC day at a time, since it caps at 1,000
  results; a run's PR comes from `commits/{sha}/pulls`, cached, because a run's own `pull_requests`
  is often empty). A pass re-reads the last two days for late completions and reruns, and fetches
  jobs only for runs whose `updated_at` changed since the last pass (persisted).
- **The Azure Pipelines gate**: per build of the repository, the approval wait (the timeline's
  `Checkpoint.Approval` record, where a serial gate queues) or, with no approval, queue to first job
  start, plus run time. Builds link to their PR head through `triggerInfo` (`sourceVersion` is the
  merge commit). `az` runs under `bash -lc`, so its credential stays in the login environment.
- **Local CI**: `shoal job` records (a placed job's lease wait before `lease_granted` and run time
  after it; the repository is the placement record's, else matched from the working directory by
  `local.paths`, else unattributed) and the resource sampler's observed lease reservations (direct
  test runs; the holder is matched to a repository by `local.holders`; a holder naming the gate's
  own build is marked `gate` and not counted twice). Research jobs are not CI.

Per PR head, **CI complete** is the last completion among every check counted on that head:
GitHub jobs plus gate builds, minus the repository's `ignore` patterns (workflow, job or pipeline
names), or exactly its `required` patterns when given. Records go to `<data_root>/_ci_wait/
<owner>__<name>.jsonl` (append-only; the last record for a key wins) with a cursor state beside them.
`shoal ci-wait observe --spec S` runs one incremental pass (`--watch S` repeats it, re-reading the
spec each pass; `--prom-out F` then writes Prometheus text atomically); `shoal ci-wait report --spec S
[--repo <owner>/<name>] --since T [--json]` prints push-to-CI-complete and gate wait (p50, p95),
GitHub queue and run time, local lease wait and run time, job-minutes per PR head and rerun share,
run time per PR head for the self-hosted GPU gate and for GitHub-hosted CPU CI (average, p50,
p95, max; a GitHub job on a `self-hosted` runner is not hosted time), and the counted checks
with the most wait per PR head. The Prometheus text holds gauges per
repository for 24 h and 7 d windows ending at the pass (quantiles do not aggregate, so they are
computed here): `shoal_ci_wait_pr_heads`, `shoal_ci_wait_push_to_complete_seconds` and
`shoal_ci_wait_gate_wait_seconds` (quantiles 0.5 and 0.95), `shoal_ci_wait_seconds` by source and
phase, `shoal_ci_wait_head_run_seconds` (source `gate` or `github_hosted`, stat `avg`, `p50`, `p95`,
`max`), `shoal_ci_wait_job_minutes_per_head`, `shoal_ci_wait_rerun_share`, and
`shoal_ci_wait_check_wait_per_head_seconds` for the ten checks with the most wait (7 d), plus
`shoal_ci_wait_source_errors_total` and `shoal_ci_wait_last_success_timestamp_seconds`. Labels never
carry a SHA or PR number. The weekly report (`telemetry/shoal_report.py`) adds one line per
configured repository from the same store, with no provider call.

## 10. Live upgrades

`shoal upgrade --spec <spec> [--dry-run]` says how each change reaches a running group before it
changes anything:

| class | what | how it arrives |
| --- | --- | --- |
| live | code loaded per run or invocation (health, digest, matcher, report, `next`, `queue`, `show`) | the next timer run or command |
| re-arm | code a running watcher executes (`ledger-watch`) | `--rearm lazy` (default): at each role's next wake (a detached watcher reloads itself in place when the code changes); `--rearm now`: one wake per stale `ledger-watch` |
| brief | `briefs.py` text | the next restart; `next` and `queue` output carry rule changes now |
| restart | a spec change a live session depends on (model, harness) | never done by `upgrade`; `restart --only` does it |
| ui | the operator UI's feeder (`shoal-ui-feed`) | restarted on any module change (it keeps the protocol modules loaded); it only reads, so no session is woken |

The command refuses a dirty package tree, runs the four selftests, diffs the package against the
spec's recorded `tooling.sha`, applies `--enable`, `--disable` and `--set` feature edits, records
the new sha with a ledger `system` event, and re-arms per `--rearm`. It also syncs the user-scope
copies of the package's skills into every `skill_homes` entry: sessions load `shoal-protocol` from
those copies, so a merged protocol change otherwise never reaches them. `doctor` warns while any
copy is stale.

`restart --only <slugs>` relaunches a subset. Its start-doc checks fail only on the roles it
relaunches; a problem in a kept role's doc prints as info. Once any session has launched, the spec
(new ids, seqs, incarnation and generation record) is saved whatever the exit code, so a launched
id is never lost. Effect-verification ignores a `ledger-watch` sidecar bound to a replaced session
or armed before the launch, and adopts a sidecar's id only when it is a longer form of the launched
id (a Claude short id against its full uuid) or the launched id is unknown.

## 11. Feature flags

A spec's `features` object switches components on per group: `workwake` (`true`, or an object
with `next_max`), `health` (`validity_wake_min`, `page`, `direct_wake`), `continuation` (`after_min`),
`waitdiscipline` (`rescan_cap_min`), `jobs`, `digest`, and
`matcher` (`live`, `model`, `free_recheck_min`, `stale_hours`, `go_fallbacks`, `paid_fallback`,
`private_repos`), `placement` (`true`, or an object with `margin_s`, `staging_s`, `max_moves`, `interval_s`,
`max_age_s`: queue transfers of placed jobs, section 6, off until a manual trial passes; and `ci_budget_min`, the CI
job runtime budget, read whether or not transfers are on), and `prune` (`true`, or `{"enabled": true, "delete": false}`: automatic N-1 pruning of old
incarnations, section 7; retirement condition: fold into the default after two clean weeks). An absent flag leaves the
component off.

## 12. Operator UI

In the lattice's global depth view, `sphere.js` caches directional light contours on a unit hemisphere.
Each contour uses at most six cubic arcs plus its move and close commands, rather than 80 polygon edges:
the script/native path bridge has a render deadline. The scene keeps its contour levels (48 since the globes became
glass, below), a soft reflection,
curved surface grid lines and role contact shadows
against a brighter depth-only background. Role coordinates stay anchored to their globe when yaw changes;
their size and fading follow the camera-facing normal. The local globe projection keeps the surface grid
and role centers within the same circular silhouette. The SVG preview accepts `--depth` and `--yaw`.
Native render checks must load the fixture before the first render: an asynchronous loading screen does
not exercise the depth scene or its path budget. `preview/check_depth.mjs` creates a temporary synchronous
fixture wrapper for repeated native checks, with depth, yaw and read-only graph snapshot options.

Depth mode draws each shoal's globe as glass. The globe's 48 nested light contours (`sphere.js`) each carry a small
alpha, solved from the total opacity wanted at that light level, so the shadow side is nearly clear (5 percent) and
the lit side a tinted veil (27 percent), with the floor grid and the globe's far-side grid lines showing through.
With the pointer anywhere on a globe, on its name or middle, or on one of its roles, items or edges, the globe goes a
little more opaque (16 and 56 percent): `main.js` reports the pointer on a globe as a `globe:<shoal>` hover id and
`scene.js` `focusedShoals` maps any hover or pin id to the shoals in focus. Transparency comes only from alpha on these
fills; the layer count went down, not up (the fixture's two globes take about 1,970 path commands, down from about
3,030), so the native render budget from PR #149 still holds. A role label that sits on a globe takes white or
near-black ink, whichever has the higher WCAG contrast against the glass under it as drawn (`sphere.js`
`glassColorAt`, `geom.js` `readableInk`); a needs-you label keeps its state hue. In depth mode every edge gets a dark
casing under its core and a little more core alpha (`scene.js` `edgeCasing`), so arrows read over a pale surface.

Per shoal, depth mode is a cutout sphere (`depth.js` `projectBowl`, geometry in `sphere.js`). The operator hub sits at
the middle of a sphere whose upper half is cut away, seen from above in perspective (the next paragraph). The cut edge
is a lit ellipse and everything inside it is the bowl's inner surface, lit as a hollow (the light pools lower right).
Each host is a lune of that inner surface, pole to rim, the lunes together tiling the whole bowl, washed and outlined
in the host color, its name just outside the outline. A host's roles sit on its own lune, spread across its azimuths
in label order (two rows from four roles), each held inside the part of the lune seen through the rim, so none is
hidden behind the near wall. Outside seats ring the rim in its plane, outside the sphere; a CI tether ends in the
middle of its host's lune. Edges and arrows are drawn over the bowl with the depth casing; `,` and `.` turn the bowl
about its axis. Projected, every circle on the bowl is an ellipse arc, so each lune is a few exact cubic arcs: the
bowl is about 160 path commands. Flat views are unchanged. `plugins/shoal-herdr-app/preview/check_depth.mjs` gained
`--shoal <id>` (the cutout sphere) and `--hover <id>` (a globe in focus), and now fails if the checked copy drew
another view or fell back to flat.

Depth mode now casts every floor shadow from one light. `depth.js` `lightWorld` turns `sphere.js` `LIGHT`, the screen
light the globes are shaded from (upper left, toward you), back through the camera into a world direction, and
`castShadow` puts each shadow where the line from that light through the caster's middle meets the floor: offset the
same way for every globe and lifted seat, by its height, and sized as a ball's shadow on a plane (its radius across
the light, its radius over the light's elevation along it). On screen, parallel rays converge a little in perspective,
as rails do. The floor now lies below the lowest globe's bottom (`FLOOR_CLEAR`, 0.45 plane gaps), so no globe sits
in its own shadow, and the fit keeps each shadow's middle in view. A shadow is a stack of concentric ellipses (`sphere.js`
`shadowLayers`, each one `geom.js` `conjugateEllipseCmds`, four cubics, turned to the light) whose 8-bit alphas are
solved from a smooth falloff: three in four layers fall in the penumbra, which widens and pales the higher the caster
floats. A globe's shadow has 16 layers, each adding under 7.5 percent, and a lifted seat's has 4. Idle globes are fainter
(5 percent on the shadow side, 27 lit, down from 7 and 34); the hovered step is unchanged (16 and 56). A globe's name
is 14 px (11 before) and sits 12 px clear above the globe and above any role body or badge on its top, with the
needs-you line between (`scene.js` `globeTitleAt`). The cutout sphere is now seen from above in perspective: the
camera hangs 2.4 sphere radii from the middle, off the bowl's axis by the camera's pitch less 30 degrees (22 in tilt,
28 in iso), so it looks down into the bowl over its near rim and the near wall hides next to nothing. The near
wall's outside shows only past the rim (a thin crescent in iso, none in tilt). The rim projects to an exact ellipse
(`bowlFrame` `rim`), each circle on the bowl is drawn as eighth-turn cubics fitted to its projection (within a pixel),
and a screen point maps back to the inner surface by the line of sight (`bowlSurfaceAt`). You, on the opening, are
drawn nearest and 1.25 times your flat size, and a role shrinks with its depth below the opening. The hosts' lunes
tile the inner surface (`depth.js` `bowlLunes`): each host's sector of the flat view is widened to the middle of the
gaps either side of it, so the spans add up to a whole turn and every point inside the rim belongs to one host. Roles
stay within their own sector. The washes are lighter (10 percent, outline 50), so edges and arrows over them read.
In the fixture, the global depth scene takes about 2,790 path commands (282 path layers: globes about 1,940, shadows
about 260), up from about 2,600; the per-shoal scene takes about 740, of which the bowl is about 160. The globes'
highlight went from 12 layers to 8 to make room. `check_depth.mjs` passes 12 of 12 in iso, tilt, turned, hovered and
per-shoal scenes.

Subscription meters are account observations, not per-role token estimates.
`subscription_usage.py` owns provider reads, normalization, one host-wide cache,
freshness, and warning thresholds. The UI status contract adds `usage`; overview
panes and the status line render that same snapshot (the overview as a table of bars, one row
per provider, falling back to text lines on a narrow terminal). An operator explicitly sets
`usage_providers` in the viewer settings to enable collection. No shoal feature
flag or role restart is needed for this read-only UI setting.

The existing feeder starts at most one background refresh. A file lock and
per-provider five-minute TTL deduplicate across processes and shoals. Network
reads never block a feeder tick or a pane. Failures back off up to one hour and
retain the last successful observation with a visible stale marker. No reset
time implies zero usage: an expired window waits for another provider reading.

Collectors call account endpoints only: Codex's native app-server
`account/rateLimits/read`, Claude's OAuth usage read, Muse's subscription key
metadata read, and OpenCode Go's quota endpoint. The Muse response's returned key
and identity fields are discarded immediately; only quota windows are cached.
Codex's process ends after the account RPC, without creating a thread or turn.
HTTP redirects are refused so credentials cannot follow another origin.
Credentials default to CLI-owned stores. Optional per-provider overrides come
from process environment or an owner-only, ignored `config/usage.env` (override
path: `SHOAL_USAGE_ENV_FILE`), parsed as data without shell execution. Cached
files contain normalized meter fields and safe diagnostics only. Direct account
reads require no browser, terminal scraping, always-running CLI session, model
request, or role wake.

An optional, operator-owned `usage_bridge` command supplies fallback observations
only after direct account reads fail. It is disabled by default, runs once per
due batch with failed providers on JSON stdin, and returns provider payloads on
stdout. The cache labels its source. SHOAL contains no browser credential reader;
a separately configured helper owns any additional authentication dependency.
Helpers returning cached readings include their original `observed_at` epoch;
collection never advances that timestamp. Invalid or future timestamps are
refused, so a passive reader of a CLI report cannot turn stale usage into an
apparently current account measurement.

Not every provider returns every window. Missing monthly meters are marked as
not reported, authentication failures are visible, and a Muse response with an active
subscription but no usage block is the server reporting no use in the current windows (the Muse
CLI's own `server_sent_no_usage`), shown as unused since reset: never a fabricated zero, and not a
read failure, so polling does not back off. A malformed usage block stays an error. CLI account APIs
and local credential layouts are version-sensitive; hermetic fixtures exercise
their boundaries and live read-only verification establishes actual coverage.
OpenCode Go's current `percent` wire field maps to the same internal percentage
as older field names. Muse's interactive `/usage` is a token-free display of
the host's last observation, not a fresh quota API: a retained reading keeps
its original observation timestamp. The collector never submits `/usage` to
`muse exec`, where it is interpreted as a model prompt.

The UI is an operator's window onto the groups on one host. It is read-only except for explicit
operator verbs, it never wakes a role as a side effect of viewing, and it owns no session. Details
and the contract are in [`ui.md`](ui.md).

- **Read model** (`shoal/ui/ui_status.py`): `ui status` builds one versioned document (`v: 1`,
  additive) for every non-retired shoal under the ledger clone. Each role row is folded by
  `ledger.View` and probed the way the health quorum probes it (`claude agents --json --all`, the
  OpenChamber status, the watcher sidecar). Rows carry posture, active and next items with
  blockers, asks, offers, liveness and `can_progress` with reasons: `dead-worker`,
  `blocked-on-question`, `watcher-dead`, `blocker-role-cannot-take`, `parked-work` and
  `waiting-on-you`. `watcher-dead` and `parked-work` call the quorum's own predicates
  (`health.watcher_dead` gated by `health.watcher_dormant`, else the informational `watcher-dormant`;
  `health.parked_free`), so the two never drift; the UI checks blockers
  without provider calls. `--conditions` adds the quorum's own `evaluate()` output, cached five minutes
  per shoal. It never pulls (unless `--pull`), commits or marks mail read.
- **Viewer** (`shoal/ui/ui_view.py`, `Backend.view_argv`): `view <shoal> <role>` runs the
  harness's own client on the live session (`claude attach`; the opencode TUI as a client of
  OpenChamber's server). It strips `HERDR_*` and `ZELLIJ*` from the client's environment and
  refuses a dead or unlisted session. `--stub` is a role card that attaches only on Enter.
  `claude attach` opens background jobs only, so a Claude seat running interactively in a terminal
  is not attachable: its card names the pid and tty to use instead, and the move to a background
  job (`claude --bg --resume <id>` once that terminal has ended; never while it runs, which starts
  a copy). A `/bg` inside the session forks a new job with a new id instead: a standalone card whose
  recorded session is not listed as a background job resolves it with `resolve_standalone` (name,
  cwd, `interactiveLineage`, start after the entry's `seen_utc`, not owned by another entry, not
  finished), follows a unique successor by rewriting its registry entry (atomic, one-time backup),
  warns when the original still runs beside one, and lets the operator pick among several
  ([`ui.md`](ui.md)). Cards and the overview dashboard redraw with `ui_term.fit_frame`: each frame is clipped
  to the pane with a marker for the cut lines and clears the scrollback, so a frame taller than its
  pane never piles copies of its top block into the scrollback on every refresh.
  Clients that take a model and effort per run (Muse, Codex) get the seat's own pair: a role's
  from its spec, a standalone seat's from its registry entry (saved by `ui pick --model/--effort`,
  which `muse-new`/`codex-new --pick` pass through), re-read on every attach like the entry's ids.
  Muse and Codex role cards always open a live observer, both during and between turns. The observer reads
  public output and tool activity incrementally without owning either session lock; `q`, Escape
  or Ctrl+C returns to the card. An explicit `i` opens native interaction only after acquiring the
  same lock as automated turns. It refuses while busy; `/exit` releases native ownership and
  returns to observation. A native client holds the session and the role's wakes until it exits, so
  `native_client.py` reports it apart from a turn (`held`, with pid, terminal and start) and offers `T`, which ends
  it unless the session was written to in the last two minutes. A Muse `/exit` while a turn runs cancels that turn
  (Muse records the command, then the run's terminal record as `cancelled`): on the way out the view says so and
  offers `c`, which runs the turn again headless (`muse.continue_cancelled`, only after such an exit) so the role
  finishes it and brings its own queue up to date. Standalone Muse and Codex cards retain native resume when idle. Native
  `--no-session-log` replay is not used: it permits unsaved divergent turns and does not follow
  ongoing work. Modern Muse journal freshness uses `sessions/YYYY/MM/DD/<id>/session.jsonl`;
  a native lock pid must own the locked file and an interactive resume process before it counts
  as an attached viewer. An exec owner is a running turn, not an interactive attachment. Quiet
  attachments read idle, and attachments without readable activity read unknown in role status.
  Key readers preserve pending input across refreshes instead of flushing a just-typed Enter.
- **Herdr adapter** (`shoal/ui/ui_herdr.py`, `plugins/shoal-herdr/`):
  - Layout: one workspace per shoal (an overview tab and a roles tab of cards) and a `standalone`
    workspace for non-shoal sessions. `ui open` is idempotent and re-applies a whole tab with
    `layout.apply`; it never types into a pane.
  - Feeder: the `shoal-ui-feed` unit reports each role pane's state (`pane.report_agent`, source
    `shoal`) and tokens (`pane.report_metadata`, with a TTL, so a dead feeder shows no data rather
    than stale data). A pane whose foreground is a bare shell gets tokens but no agent report:
    Herdr drops that report at once, so sending it only made the role flash in the agents list.
    The `mode` token (`▶` turn, `◷` waiting on a job or CI) says why a role reads working; an attached but
    quiet Muse or Codex seat reads idle (`status_line` adds `active` or `quiet` from the session file's age).
  - Actions: plugin actions open popups that run shoal verbs after a `--dry-run` preview and a
    confirm, and log them to `ui-actions.jsonl`.
  - The adapter never calls `pane.send_*`, `agent.prompt` or `pane.close`, and the UI selftest
    enforces this.
- **Standalone picker** (`Backend.sessions`, `shoal/ui/ui_sessions.py`, `ui_herdr.cmd_pick`): the
  menu enumerates the backend registry and uses one normalized discovery hook per ecosystem, so a
  future ACP backend needs no menu branch. Unsupported discovery contributes no candidates. It lists Claude background
  and live interactive sessions, OpenChamber sessions, Codex threads through `thread/list` (interactive
  sources plus named `exec`/`appServer` threads, since `codex-new` creates its seats headlessly; unnamed
  headless threads are role turns and matcher runs), and Muse root sessions through `session/list`.
  Every saved registry entry that no listing returned (dismissed, stopped or ended) is offered too, as
  `registered` or `dismissed`, so a seat can always be re-added. Re-picking a saved session keeps its
  fields (`doc`, handover settings) and clears only `dismissed`; a new session under an old label
  inherits its `doc` and handover settings, and its model and effort only from the same harness. Short-lived local stdio servers are bounded and reaped;
  listing never starts, resumes or locks a session or requests inference. Role/internal sessions
  and harness subagents are excluded. Interactive picks retain stable job ids and available model
  metadata. Palette and plugin popups pass `--pause`, keeping empty-list and server errors visible
  until dismissed; intentional cancellation closes normally. Explicit `--harness`/`--id` remains
  available for sessions outside the discovered lists.
- **Question classification:** role and standalone Claude cards use the same confirmed prompt
  detector (`health.claude_prompt_open`, exposed by `ui_status.pending_question`). A live standalone
  session with an open prompt displays `QUESTION`, even with idle tempo or active subagents; Herdr's
  transport state remains `blocked`. Answered prompts clear that label. Raw `blocked` alone is not
  proof of an open question, and dead sessions stay dead. Role CI/job waits retain their primary
  wait label with question attention separate. Other harnesses retain their existing probes until
  they expose an equally reliable structured prompt signal.
- **Launch chooser** (`shoal/ui/ui_launch.py`, `splash_graphics.py`): five transparent PNG creatures (three fish, a seahorse and a blowfish)
  sit around the SHOAL logo above the host menu, behind text at Kitty graphics z-index -1. An
  actual PNG query followed by a device-attributes fence selects image mode, with a bounded wait;
  terminal names alone are not proof of graphics support. PNGs travel in 4096-byte base64 chunks
  over the tty, so SSH needs no shared files or image helper. Successful uploads use fresh
  terminal-assigned ids, are cached across redraws and have only their own allocations deleted
  on exit or partial failure. One input owner consumes fragmented and late graphics replies while
  preserving keyboard input, including a target typed during negotiation.
  The images render at twice their base cell size where the window has room, else at 1.5x, else at
  base size (`image_scale`); the compact 72-by-23 minimum does not grow with the art.
  Unsupported or silent terminals fall back to three original 36-by-13 ASCII fish, in different
  colors with one facing left, only when all three fit intact (121 columns by 40 rows for two
  hosts). Smaller windows omit fish. `SHOAL_SPLASH_ART=ascii|none` bypasses the probe; `auto`
  is the default. Wheel package data includes both UI modules and the PNG assets.
  The complete composition stays centered on resize, with fish positions following the logo.
  Measured tty dimensions override inherited `COLUMNS` and `LINES`. Each redraw explicitly
  positions and clears every row with wrapping disabled, independent of output newline settings.
  The compact minimum for two hosts is 72 columns by 23 rows, including image space and help.
  Below it, a bounded enlargement prompt replaces the chooser and requests a resize once per
  small-window episode. Window-manager minimum sizes are not enforceable through the portable
  terminal interface; resizing back restores the centered chooser without losing selection.
- **Herdr app lattice view** (`plugins/shoal-herdr-app/mods/lattice-view/`): a mod for the Herdr app
  (the GPUI desktop window, not herdr 0.9.x) that draws the graph doc v1 (`shoal ui graph --json`)
  as an animated network: a global view of every shoal around the operator hub, a per-shoal view
  with host bands, a needs-you, shoal and host rail, and a compact `pulse` mode (an edge drawer:
  a row per shoal, then up to five needs-you roles; a role row sets the shared key `lattice.focus`,
  which every full copy honors by drilling in and pinning that role). A `shoal` prop opens a copy
  drilled into one shoal as its home view, so a per-shoal tab can read the one shared feed file
  instead of polling its own `exec`. It only reads: an
  `exec` of the feed (optionally `on` a herdr-core machine), a file, or its bundled fixture. Logic
  lives in pure modules tested under Node (`tests/herdr_app/test_lattice_view.mjs`, run by
  `selftest_ui.sh` when `node` is present); `main.js` turns their display list into native
  elements, and all motion is native (`loop`, `frames`, `transition`). Motion follows a budget
  tier (`full`, `balanced`, `low`, `off`, or `auto` from a renderer probe), because any animated
  element makes the window redraw and a frame is expensive on a software renderer; `low` and `off`
  animate nothing, and a data poll redraws only when what is shown changed. The probe trusts a
  forcing or selecting environment first, then the process maps hardware first (the Vulkan loader
  maps every installed ICD, lavapipe included). Positions are kept by node id: a poll with the
  same node set moves nothing, a changed one refines only new and retargeted nodes, and `f` relays
  out and fits the measured scene (labels and cluster titles included) inside the canvas. The `source` argv
  must stay a read-only shoal command. Encodings key on role-time class and event; unknown values
  render as "other" or "unknown". `wait:self` is drawn as its cause (an open question, held while the
  operator is offline, a session that ended, or idle with work queued), and accounting keeps the event. So does
  `wait:assignment`, which after two hours with no open question stops asking for attention (no halo, out of needs-you, a
  count on the rail; the agents list reads it as idle). States use five colors by what you would do, badges and hatches say
  which event, and the legend lists what is on screen, with the full key a click away. A research or prototyping run
  (`wait:research-proto`, role-time group `research`) spins like a CI wait but reads apart from both: a dashed outline, a
  wide hatch and `⚗`. Each role's active, next and
  recently done items are satellites on short spokes: a next item with a recorded blocker is a ring, a stale
  blocker red, and a hover shows the item's card (its title, what it waits on, how many are queued past those
  drawn) while a click opens its issue (an `http` or `https` link only, since a role writes it). A ring is the
  blocker as last recorded, judged stale by age as `ui status` does, so one that has cleared reads as held until
  re-recorded. A hover or
  click reaches a node, then an item, then an edge, then the middle of a shoal's circle, which is the shoal's status (the
  feed's writer node is not drawn). A right-click on a role opens `shoal-actions` on it; on a shoal's circle or
  title, on that shoal; anywhere else, across every shoal. An outside writer or parked role can be taken
  off the picture (shared `lattice.hidden`, restored from the rail). `r` replays the last 24 h (`replay.js`, the feed's `--replay`): its events as
  packets on the live picture, a quarter hour a second, under a bar of a quarter hour a column that seeks on a click.
  `d` is the depth mode (`depth.js`, P3b; per shoal, the cutout sphere described in section 12): each shoal (global) a translucent slab stacked in depth
  under a tilted camera seen in perspective (tilt, iso; `,` and `.` turn it), over a floor grid with stems and shadows,
  each shoal a sphere in the global view with its roles placed on it by need, then activity, then recency (the side facing
  you holds what needs you), the operator and outside seats on a plane above; the layout
  is projected before the scene, so hit tests, packets and cards follow it, and a CPU renderer at low or off motion
  stays flat. The rail warns when the feed's ledger
  clone is over an hour old, because the feed never pulls.
- **Herdr app home** (`plugins/shoal-herdr-app/mods/shoal-home/`): the splash and host chooser as
  a native mod. It reads `shoal ui launch --list --json --summary` (the terminal chooser's list,
  additive v1) every 20 s and on refresh, and draws a logo in original bold rounded glass letters on one baseline, each a small aquarium with water
  in a light blue to blue gradient, a bright waterline and a modest procedural reef (a pre-rendered `assets/logo.png`
  from `preview/gen_logo_image.mjs` and `logo_reef.mjs`, because a mod's render has a time limit and all that detail
  would pass it; the plain outlines in `glyphs.js`, filled, as a light vector fallback while the image is not available), the acronym tagline with each initial underlined, the
  five splash creatures with `image()` (placed like `image_rectangles`, scaled to the window; text only
  below 640 by 460 or without the assets), a kelp sea whose two sets are the shoals, joined by one direct tendril, with two solo vines for
  standalone sessions that weave through the letters and link to both sets (`ocean.js`, a static display
  list; only the solo vines cross the logo, none reaches the cards) and one card per host: reachability with the doctor hint, the last choice,
  per-shoal needs-you, working and waiting, and the pin check. A click runs
  `shoal ui app-open --machine <name>` here and shows its result on the card; unreachable cards
  do not click. Motion uses the lattice budget (its `motion.js`, copied verbatim): a native drift
  only at `full`, stopped once a host is opened. Pure modules are tested under Node
  (`tests/herdr_app/test_shoal_home.mjs`, run by `selftest_ui.sh` when `node` is present).
- **Herdr app actions dialog** (`plugins/shoal-herdr-app/mods/shoal-actions/`): the operator
  actions in place of the 0.9.3 popup palette, a dialog overlay opened by `mod.open shoal-actions`
  (a key), a right-click on a lattice role node (`actionsOverlay` in lattice-view's `model.js`), or
  `openOverlay` with props `{shoal, role, machine}`. Without props it starts on the focused card
  (its `shoal.pane` label and its space's machine), else on the first needs-you role. It runs only
  `shoal ui act-plan roles|<verb> --json` and `shoal ui act-apply --token --json`, on the machine
  of the role's card (exec `on`). A write verb shows the plan's command, `--dry-run` preview and
  wake cost, and applies only on Confirm with the plan's one-use token (offline applies right
  after its plan, still shown); the token stays in memory, and the dialog never types into a pane.
  Read-only verbs (next, asks, status, conditions, upgrade) render the CLI's text; Answer on an ask
  opens Message to that role with wake on. The verbs sit in five labeled rows by what they are for (Talk, Work,
  Seat, Shoal, Inspect: `GROUPS` in `model.js`), and a long command is shown broken at its arguments with shell
  continuations (`commandLines`), since the text renderer's own wrap dropped the spaces at its breaks. Pure decisions
  in `model.js` are tested under Node (`tests/herdr_app/test_shoal_actions.mjs`); `props.fixture` answers from bundled
  fixtures.
- **Herdr app usage dashboard** (`plugins/shoal-herdr-app/mods/shoal-usage/`): the home space's `usage` tab, opened by
  the top bar's Usage (`shoal-status.usage`, else `ui app-usage`). It reads `shoal ui usage --json` (the cached meters;
  Refresh adds `--refresh`, the account APIs, never a model turn) from the first machine with a reading. Each provider
  account is a card of windows: used percent, reset (with its wall-clock time in the app machine's zone, from its
  offset: `clockTime`), and pace (how far into the window against how much is used, so a
  meter heading past 100% before its reset says when it runs out). The tightest meter is the headline. Panels by shoal,
  by role and by standalone seat read an optional `breakdown` in the same document and say so until shoal records
  each session's usage. A shoal's overview (`ui dash`) no longer prints the meters; `ui status` still does.
- **Herdr app board graph** (`plugins/shoal-herdr-app/mods/shoal-board/`, read model `shoal/ui/ui_board.py`): the home
  space's `board` tab, opened by the top bar's Board (`shoal-status.board`, which sets shared `board.focus` to the shoal
  in view, else `ui app-board`). It reads `shoal ui board --json` from the first machine with a board digest: per shoal
  and board, the digest's open items as nodes and `parent` (issue to sub-issue) and `blocks` (open blocker to the item)
  edges, a parent or blocker not open on the board kept as an `off_board` node. Each node's `work` comes from the status
  rows: `active` (a role's active item), `queued` at its `pos`, `queued-blocked` (a recorded blocker) or `unqueued`.
  The board is the root; the model lays each item a layer below its parent and blockers, wraps a wide layer into rows,
  and puts items in no dependency in a grid of their own below the graph. Hover shows an item's details; a click
  selects it (the side panel closes on its x or Esc) for Open on GitHub, Offer to a role (the actions dialog, its
  `item` prefilled; the role picked from a list) or Move to a role (that
  role's Queue, with `pick` set, so the plan transfers it). The metrics panel shows the rail card's counts, closes in
  seven days, the days to none open at that pace, and 14-day trends of open items and closes a day, from
  `board_history.jsonl`, which `digest.write` appends at most every half hour per board and keeps for 90 days.
  The graph draws as cards or as nodes (`DIMS.cards`, `DIMS.nodes`: a shape per repo, `repoShapes`, each state's
  shapes as one fill and one outline path), either at a zoom (`scaleDims`; `fitZoom` finds the largest zoom up to 1
  at which the whole tree fits the view, a smaller zoom fitting more items to a row). It is
  read line by line, so a torn or foreign line is dropped and the rest kept, and no error there fails the digest.
- **The plugin wrapper** (`plugins/shoal-herdr/bin/shoal-ui`): every 0.9.3 pane, Herdr app card and UI unit runs
  this checkout's `cli.py` through it, with `SHOAL_PYTHON`, else the Python of the `shoal` on PATH (its shebang,
  or the exec line when that `shoal` is a shell wrapper), else `python3`.
- **Rollout:** the UI modules are `live` code, except the feeder, which `upgrade` restarts on any
  module change. No session is woken.

### Graph feed (`shoal ui graph`)

`shoal/ui/ui_graph.py` turns the same read model into a network document (`kind: shoal-graph`,
`v: 1`, additive; contract in [`ui.md`](ui.md)). Role nodes take their rows from the status cache
(else `ui_status.role_row` over the graph's own View) and their state from `role_time.classify`
over cached native status only, so the graph speaks the role-time vocabulary; a CI or lease wait
carries its job's host from the local job meta. The maintainer, each shoal's system writer (which absorbs the retired relay's writer) and outside seats (`ext:<slug>`, aliased to a role in another shoal) are
nodes too. Edges aggregate ledger events per (src, dst, class) over a rate window, an hour and a
day, plus `blocked-on` and `alias` edges that need no event, each marked `cross_host`; an edge carries
its last three events' summaries for the lattice's edge hover card. Events are a
capped tail behind an opaque cursor of per-file byte offsets. `ui graph-feed` keeps those offsets
in memory and writes `graph.json` beside the status cache each tick, reading only appended bytes.
It is read-only like the rest of the UI: no pull, commit, wake, `next` or provider call. So a host that is no
shoal's home has a clone nothing refreshes; each shoal says how old its clone is (`ledger_age_s`), and the app's lattice
tabs read the feed of the machine their space is on. Each shoal also carries its priority boards (`boards`): a link for
each from the spec, and, from this host's last digest, its items as done, open, in flight (a role's active item) and assigned
(in a role's queue, nobody's active item). The feed never reads a board itself, so those counts are as old as the digest, and a
host that is not the shoal's home has the links alone. The digest records each board's title, page and done count for this.
Queue items match board items by `item_key`, which needs `default_owner` set: without it a queue item keeps its owner, and reads
as off the board. The same match gives a role's work items their board titles; an item not in the digest has none.
`next_total` counts the whole queue, since the lattice draws only the first few.
A saved standalone session on this host is an `external` node whether or not it ever wrote to a ledger or was dismissed from the
app's spaces, and says which session it is and whether it runs now (`saved`, `standalone`). A ledger writer with no saved session has nothing to remove, so the lattice
offers only to hide it. A blocked Claude session's own words (its job's `needs`) travel with its row as `says`. `shoal dismiss` records a
`needs-dismissed` system event with a fingerprint of the role's reasons, asks, words and last posture; while the fingerprint holds, the row's
`needs_you` is false and `dismissed` true, and any change raises it again. It is display only: health reads neither, and a dismissal over a
`dead-worker` or `watcher-dead` lapses after 24 hours.

### Herdr app adapter (`shoal ui app-*`)

`shoal/ui/ui_app.py` maps the same layout onto the Herdr app, whose spaces, tabs and terminals live in each
machine's herdr-core. Its `shoal` layout is Herdr's own `main` with five blocks swapped: `shoal-agents`,
`shoal-spaces` (which also leaves the home space out of the sidebar: the top bar reaches it), `shoal-chrome` (the window's
mark: SHOAL and its icon, a file the copy carries) and `shoal-tabs` (Home, Lattice, Board, Actions and Usage in the top bar,
beside Files and Changes) are patched copies of Herdr's mods, redone when the shipped text changes (a moved anchor leaves that
block on Herdr's mod), and name each role in its shoal's color on its real host, or offer host-wide actions;
`shoal-agents` also orders its Priority view by the derived agent state each card writes on its terminal
(`shoal.group`, `shoal.state`: `ui_app.role_agent` from a status row's `agent`, `ui_app.standalone_agent` from a seat's
report), with Herdr's status on the same scale for a row that has none;
`shoal-status` adds the next needs-you agent (Ctrl+Alt+N, in the pulse's order), the way back to home (`ui app-home`) and to a lattice (`ui app-lattice`) and per-provider usage, and runs `ui app-sync` on a
slow timer so roles tabs follow the roles. A roles tab is rebuilt only when every card in it is idle. The work is split where the data lives. `ui app-plan` runs on the host with the shoals
and prints the spaces it wants (one per shoal: `overview` and the `roles` tabs of cards; one per saved standalone
session, with its `session` tab; the top bar's Lattice opens the global lattice, drilled into the shoal in view). `ui app-open` runs where the app runs: it reads the plan (in process, or over the
route for another host, which it first adds as a herdr-core machine named after the route), compares it with
`herdr-app ls`, and adds only what is missing with `herdr-app do`, labelling each terminal it starts
(`shoal.pane`). A labelled pane whose terminal came back as a bare shell is replaced; any other foreground
is left alone (the 0.9.3 allowlist). A role card in a herdr-core terminal (`HC_TERMINAL_ID` set) prints its
own OSC 7501 status each refresh, so the app's agents list, dots, sounds and toasts follow the role without a
feeder push; a question from ledger asks also labels their count (`shoal.asks`), which the agents list's badge opens in the
actions dialog's Asks (per ask: Reply, and the issues or PRs it names); a blocked role names `auth`, `question` or `permission` (the only kinds herdr-core keeps), or, for a dead
session, a dead watcher or a blocker that cannot move, sets the terminal label `shoal.need` that the agents list's badge reads; and a role that finished an item and rests says
`done` once (`DoneLatch`, from the row's `last_done`), which the app holds until someone looks. A local standalone
Claude card reports its session too (`standalone_report` over `claude agents`; `done` when a turn it saw has ended),
and every such card on a host shares one listing (`ui_status.claude_rows_cached`, a locked file beside the status cache). `ui app-start` starts the app as a user
unit with its release feed pointed at a dead loopback URL (the pin: the app otherwise updates itself and then
its core), then opens a home space. `ui app-install` writes the `shoal-graph-feed` unit, installs the mods
into the app's home, and sets the 0.9.3 paste keys, the actions key and the `shoal-ocean` theme where the
person has not chosen their own; it also installs the `shoal` icon (`shoal/ui/assets/shoal.svg`) into the user's icon
theme for every `Icon=shoal` launcher. `ui launch` in an app terminal opens the chosen host in the app, and `ui
launch --list --json [--summary]` gives the host list the app's home screen reads. Nothing here wakes a role,
types into a pane or writes the ledger; herdr 0.9.3 is untouched.

`shoal/ui/ui_actions.py` (`ui act-plan`, `ui act-apply`) is the palette without a terminal, for the app's
actions dialog. Its `add-role` verb starts a new role from the app (`shoal add-role`, its start doc checked before the
plan, or first written from guidance by `shoal doc-init`, the plan then stopping at a failed step), and its `sessions` view and `add-session` verb add a standalone seat: every machine's
candidates, saved by `ui pick --save` on the session's own machine, then opened with `ui app-open --show`. A write verb's plan builds the palette's argv, runs its `--dry-run`, names the wake cost, and
stores the argv under a one-time token (0600, runtime dir, ten minutes); apply runs only a stored argv, once,
and logs it like the palette. A caller can confirm what it was shown but cannot pass an argv of its own. The apply runs only the
CLI verbs the plans build (`ui_actions.APPLY_VERBS`: the dialog's verbs, and `set-fill` for Fill).
Read-only views (`next`, `status`, `conditions`, `asks`, `queue`, `upgrade`, `roles`) store nothing.

The Actions dialog's **Handoff** defaults to `handoff --prepare`. `handoff_flow.py` requests a role's own
restart-ready queue checkpoint through a woken ledger message, waits for a new checkpoint and a quiet
session, then calls the ordinary unforced handoff gate. It follows the restart log until a replacement
session is recorded. Saved standalone seats use `handoff --session`: the current session refreshes its
start doc and finishes its turn before retirement and creation of its replacement. The saved seat label,
doc, viewer settings and space remain; provider-specific model defaults are not copied across providers.
Preview binds the current session id, and a changed seat is refused. A missing checkpoint times out.
A restart note (`handoff --note`, the dialog's Restart note) goes into the new session's first prompt after its start
doc: a role's restart gets it as `restart --note SLUG:TEXT`, set on the role for that run's brief only (`briefs.note_block`,
never saved with the spec), and a standalone seat's resume brief has it appended (`handoff_flow.resume_brief`). A role
whose Claude session has a client attached is refused before anything stops (`park._claude_client`): the client keeps
the worker alive, so the stop would report it still live and nothing would restart.
A standalone seat can explicitly use `--no-prepare` when its checkpoint validates, has no live job-folder
references and the source is already idle. `OpenChamberBackend.create` sends a supplied resume brief with
the requested model and effort (`--variant`); role startup creates an empty session and sends separately.

Three cases the 2026-11-09 seat moves hit by hand are now part of the flow:
- **An open question.** A Claude session stopped at a question prompt is never between turns, so a checkpoint
  request would wait to its deadline. `handoff_flow.question_open` refuses that at once, for a role and for a
  standalone seat. For a standalone Claude seat the operator may pass `--through-question`: the checkpoint request is
  then delivered with `ClaudeBackend.deliver(..., through_question=True)`, which stops the turn at the question and
  resumes the same conversation with the request (the question is dropped).
- **A seat handing itself off.** Stopping its own session would end the turn running the command. `is_self` matches
  the caller's Claude job folder or conversation (or `SHOAL_SELF_SESSION` for another harness). The seat writes its
  checkpoint first and opts in with `--no-prepare` (a self `--prepare` is refused); `detach_self` then checks that
  the start doc validates, and relaunches the handoff as a transient user unit
  (`systemd-run --user`, log in the handoff runtime dir) with `--no-prepare --await-idle` and the caller's `PATH`,
  config and `SHOAL_*` environment, since a transient unit starts from the manager's environment without the tool
  path. The unit waits until the seat is idle on two checks `POLL_S` apart, then hands off as usual.
- **Registry lineage.** Each standalone handoff appends the retired harness, session and job to the entry's
  `lineage` (the last ten), so a stale card, job or name-addressed message can be traced to the seat it belonged to.
  A leftover entry is removed with `shoal remove-session`.

A standalone card follows its registry entry by label each tick (`ui_view.registry_follow`), harness included: a
seat moved from Claude to OpenCode or back switches its card in place, clears the app report of the old session, and
reports only while the seat is on Claude. A routed card's attach re-reads the registry on the seat's own host, so it
reaches the session the seat holds now rather than the one the card was opened with.
The app's reconciler (`ui_app.Reconciler._gaps`) also replaces a standalone card that sits at its own screen
with the harness and session it was opened with after its seat moved (`ui_app.seat_ids` read from the pane's
foreground argv against the plan), on `ui app-open` and on the quiet sync. A card with a harness client open in it is
left alone until the client exits.

`ui_runs.py` owns confirmed background handoffs. `act-apply --background` consumes the one-use token and
starts a detached worker; `act-status` returns the retained log and running or completed state. A worker
that disappears is reported as failed. The worker must not carry the app exec's `HC_SCOPE`: herdr-core ends every
process tagged with it once that exec returns, whatever its session or cgroup, so `backends.detached()` drops it
(`env -u`) for every detached launch, the worker and a Muse or Codex turn runner a dialog wake starts alike.
Reopening Handoff on the same target reconnects to its latest run;
closing the dialog does not cancel it. The reply parser preserves progress paths for both background
handoffs and ordinary streamed actions. All lifecycle changes remain CLI verbs behind preview and confirm.

`shoal/ui/ui_queue.py` (`ui queue`, and `act-plan queue` and `queue-set`) is the queue manager's read side and batch. The read
is one role's queue from its latest queue version (active, next, recorded blockers) and its candidates, each with a source:
`offer`, `held` (a claim its queue omits), `board`, `released` (another role gave it up lately) or `other` (another role holds
or was offered it, with that owner). Pending transfers and the latest queue order come from `View.transfers` and
`View.queue_orders` where the ledger has them; their state rules live there. The batch turns a proposed layout into shoal
verbs, `withdraw`, `offer`, `transfer`, `msg --kind request` and `queue-order` last, each previewed by its own `--dry-run`,
with `REFUSED <item>: <reason>` lines returned by item. The plan stops at the first failed step. shoal never writes a role's
queue: the role applies the order with its own `queue` write.

### Host resource observations and placement pilot

Cross-host imbalance over compatible demand (`shoal/pool_metrics.py`): where the pool planner is
importable, the resource sampler's Prometheus text also carries `shoal_pool_placed_queued{host}`
(placed jobs queued for their lease) and `shoal_pool_waiting_compatible{host,target,key}`: those
queued jobs that `pool.compatible(target, demand, home)` says another pool host could run, by lease
key (a pinned job, or a vendor the target lacks, never counts). A rule then charges imbalance only
while the target holds nothing under that key. It recomputes at most once a minute, probes hosts
only when a placed job is queued (through the planner's probe cache), and reports a measured zero
when none is; a copied standalone sampler skips it.

`shoal resources sample|watch|report|balance|install` (`shoal/resource_usage.py`) maintains one
host stream outside git, under `<data_root>/_resources/<host>/`. The stdlib sampler can run as a
standalone file on compute-only hosts. It reads NVIDIA CSV or AMD DRM sysfs, host memory and the
configured lease-provider contract. GPU UUIDs join device observations to leases; exclusive global
GPU holds reserve all devices, shared global gates defer ownership to individual device claims,
and cpu-heavy is a separate host CPU/memory isolation scope. Raw command lines are not collected.

Daily JSONL contains resource samples and explicitly observed acquisition/release edges. Start
metadata is retained; release is bounded between successful observations. Failed probes cannot
close a lease. Short leases between samples can be missed. Reports time-weight per-device GPU
utilization and active fraction at a stated threshold, show memory pressure, cap interpolation gaps,
and expose coverage and open/censored intervals. They measure reservation overlap, not proven
process ownership. Missing metrics are null, never idle zeroes. Each observed episode has a distinct
identity, including when an unattributed claim later reappears.

The placement pilot is advisory: it checks fresh lease state, memory, unreserved fitting VRAM and declared GPU vendor compatibility,
queued requests and explicit session readiness. A compute-ready host without a provisioned harness
is not eligible for session migration. Recommendations retain the current eligible host within a
small score margin to limit churn. The advisor acquires no lease, changes no host mode or role spec,
and performs no handoff (CI job placement, which does act, is `job run --place`: section 6). `resources install --dry-run` previews one host service; `--enable` starts
it only when the operator applies the reviewed deployment, and restarts an already-running service so the new
unit file takes effect. See [resource telemetry](resources.md).

`sample` and `watch` also accept `--prom-out FILE`, writing each sample's GPU and lease projection
(utilization, VRAM, runtime-suspended state, held reservations and queue lengths with measured zeros for known idle keys, sample timestamp) as Prometheus text for a
textfile collector, and `--role-time-spec SPEC`, refreshing that shoal's role-time native status
cache each sample on hosts that run sessions (a copied standalone sampler skips it). `install`
carries both flags into the generated unit.

## 13. Known limitations

What this design does not handle yet, and the measurement that would trigger each change, is in
[`limitations.md`](limitations.md).

**Open: an urgent wake waits behind a role's background job wait** (found live 2026-11-01). A Claude
role's direct wake (`ClaudeBackend.wake`, `shoal/backends.py`) is deferred with `deferred (background
work in flight)` while the session's job reports in-flight background work, and a role that follows a
long job with `job wait` as its background command has that work in flight for the whole run. An
operator's `msg --wake --kind request` to such a role is therefore deferred until the job wait exits,
which can be hours: the message is in the ledger, but nothing puts it in front of the session. The wake
path is unchanged for now; until a fix is designed, a request that cannot wait goes to a role that is
not following a long job, or the operator attaches the session (`shoal view`) and prompts it directly.
