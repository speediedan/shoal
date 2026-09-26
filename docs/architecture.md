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
[`limitations.md`](limitations.md).

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
| role start doc | each role's `plan_state.md` | always | mission, current state, `grants-needed` |
| briefs | `shoal/briefs.py` | always | the first prompt of every session, identical in shape for every role |
| work loop | `shoal/worker.py` (`next`, `queue`, `asks`, `offline`) | `workwake` | one read and one write per boundary |
| session watcher | `shoal/ledger_watch.py` (`ledger-watch`) | always (work-only waking with `workwake`) | wakes its own session |
| health quorum | `shoal/health.py` (`health`, timers `health-a` and `health-b`) | `health` | detect and recover stalled roles, dead watchers, stale waits |
| session wake | `Backend.wake` in `shoal/backends.py`, one implementation per harness | always | put a wake in front of one role's session (section 7) |
| wake relay | role `wake-relay` (`fill=none`), `relay-init`, `relay-launch`, `relay-watch` | `relay` | the Claude implementation's path when a role's own watcher is dead |
| job units | `shoal/jobs.py` (`job run`, `wait`, `status`, `stop`) | always (brief guidance with `jobs`) | long local work outside the harness |
| board digest | `shoal/digest.py`, `shoal/sources.py` (`digest`, `triage`, `context`) | `digest` | one board read per group, ranked per role |
| semantic matcher | `shoal/matcher.py`, run by the digest | `matcher` (`live: false` is shadow) | a model scores items against each lane's brief |
| residual lane | a role with `aux: true`, `aux-init`, `aux-launch` (`shoal/aux.py`) | optional role | work no active lane covers |
| wait-target providers | `shoal/providers.py` | always | resolve `--target <kind>:<ref>` to pending, resolved or unknown |
| harness adapters | `shoal/backends.py` | always | create, message and probe sessions per harness |
| sensors | `shoal/watchers.py` (`watch lanes`, `watch pipeline`) | always | harness server respawn, CI pipeline starvation |
| heartbeat daemon | `shoal/heartbeat.py` | when `health` is off | stall detection without the quorum |
| live upgrade | `shoal/upgrade.py` (`upgrade`) | always | roll new code and feature edits into a running group |
| telemetry and reports | `shoal/telemetry/` (`transition`, `report`, reconcile) | always | events, ground truth, the review artifact |
| host instruments | `shoal/psi_watch.py`, `shoal/kill_audit.py` (`kill-audit`) | run by the operator | memory-pressure fires and harness reaps, joined |
| timers | `shoal/systemd.py` (`install-systemd`, `uninstall-systemd`) | per spec | systemd `--user` units named `shoal-<id>-<job>` |
| preflight | `shoal/doctor.py` (`doctor`) | always | host checks, and group checks with `--spec` |
| state primitive | `shoal/state.py` (`transition`) | always | heartbeat, wait proof and telemetry in one command |

**Where state lives.**

- **Ledger repository** (a git repository every host clones, `ledger_repo` / `ledger_dir`): the
  spec and the ledger, under `shoals/<id>/`. A group created before the rename lives at
  `hags/<id>/` with `hag.json`; that layout is read as a fallback until the group is migrated, and
  `doctor` warns while it is in use.
- **Data root** (`data_root`, never committed), per group under `<data_root>/<id>/`: `contexts/`,
  `roles/<slug>/plan_state.md` (generated start docs for the wake relay and the residual lane),
  `matcher/`, `briefs/`, `telemetry/`, `reports/`, `jobs/`, `board_digest.json` and, in shadow
  mode, `board_digest.semantic.json`.
- **Scratch** in `/tmp/<id>/`: heartbeats, wait proofs, watcher sidecars, timer ticks, wake
  requests.
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
  or a mode change by anyone but `user` or the system writer, is a violation. Ledgers written
  before the rename carry `hag.py` as the system writer, and it is accepted as such.
- **Claims are first-landed-wins.** `claim` checks before and after its push; a loser writes
  `release --kind lost-race`.
- **Declines are data.** `release --kind decline --reason .. --retry <condition>`; the item never
  re-surfaces to that role.
- **Handoffs.** `release --kind handoff --to <role>` turns the holder's claim into an implicit
  offer to that role. It is the only way a role gives another role work.
- **Completion is verified against GitHub** by reconcile, never taken from `done` events.

## 4. The work loop and wakes

**The queue stream.** A role's latest `queue` event (`active`, `next` of up to `next_max`, `asks`,
`reason`, `posture`) is its queue and its posture. The maintainer influences it with `offer`
(`--prio`, and `--now` to interrupt a busy role) and `withdraw`.

**Two verbs per boundary.**

- `shoal next` reads, in one call: the queue version, open offers, digest candidates when `next`
  is short, the integrity of the active and next items (taken, done elsewhere), unread mail, wait
  status, and whether the role's watcher is alive.
- `shoal queue` writes one ledger commit: `done`, claims (the active item is claimed if not held),
  passes with a reason class and an optional `suggest=<role>`, asks, the posture, and the new
  queue version. Heartbeat, wait proof and telemetry are written locally in the same call.
- Every pop backfills `next` to `next_max` (default 3), from offers by priority and then digest
  candidates, or records why fewer.

**Every turn ends in a sleep posture:** `queue --wait <sub> --holder <who> --target <kind:ref>`
with the waiter armed first, `queue --idle`, or `queue --active <item>` and keep working. With
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
- rescans are capped per substate (maintainer and peer waits 240 minutes, CI 180, `upstream` 720,
  `queue-empty` 360; overridable with `rescan_cap_min` in the spec);
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
| `ledger-watch` | an in-session background command (Claude: `--wake exit`; opencode: `--wake send:<own id>`) | its own session, for work only | sidecar pid; health `watcher-dead` |
| wait waiter (CI, lease, job) | an in-session background command, head-pinned, with verdict lines | its own session | a missing verdict; health `wait-resolved`, `wait-validity` |
| job unit and `job wait` | a `systemd-run --user` transient unit with a `MemoryHigh` cap | its own session, through the waiter | `JOB-VERDICT` line; unit state |
| `relay-watch` | the wake relay session's single background command | the wake relay session | its pid file; health `relay-dead` page |
| `health-a`, `health-b` | systemd user timers | roles (through adapters), the maintainer | each checks the other's tick |
| lanes, pipeline, digest, reconcile, weekly report | systemd user timers | the health adapters (sensor fires) | `/tmp/<id>/tick-<job>` older than three intervals |

Every watcher wakes only its owner (or routes through the health adapters), logs a verdict line on
every designed exit, and names every terminal state in its predicate: a cancelled run is tolerated
only when superseded, and an empty query is an error, never green.

**Placement.** Anything whose loss costs work runs as a job unit (`job run ... -- <cmd>`) followed
by a `job wait` background waiter, because a harness may reap background commands and a job unit
survives that. Watchers stay small and idempotent: re-arming never duplicates a side effect. A
`killed` notification is itself a wake: the role runs `next`, acts, re-arms and proves the new pid.

## 7. Health quorum (`health`)

Two identical timers run `shoal health`, `health-a` at `*:0/5` and `health-b` at `*:2/5`, sharing
one state file under a lock so whichever runs first acts. Each run ticks and checks its peer's
tick, and pages the maintainer once if the peer is silent past 15 minutes. It replaces the
heartbeat daemon.

A condition instance is keyed by its evidence (offer id, dead pid, target, last boundary). Each
run takes at most one step per instance, and a cleared condition resets its ladder.

| condition | evidence | ladder (minutes) |
| --- | --- | --- |
| `unacked-work` | an offer or wake request to an idle role, or a `--now` offer, newer than its last queue version | wake 10, wake 30, page 60 |
| `wait-resolved` | a checkable wait target is no longer pending | wake 15, page 60 |
| `wait-validity` | any CI, lease or job wait (not `upstream`), every `validity_wake_min` (default 90); the ladder is keyed by the target, so re-declaring the same wait keeps escalating | wake, wake, page, one interval apart |
| `watcher-dead` | the `ledger-watch` pid is gone and the session is between turns | wake 10, page 40 |
| `turn-ended-active` (`continuation`) | posture `active`, session proven between turns | wake at `after_min`, wake 30 later, page 90 later |
| `stale-active` (when `continuation` is off) | posture `active`, no ledger write, session between turns | wake 90, page 180 |
| `wait-expired` (`waitdiscipline`) | a waiting posture past its capped rescan | wake 0, wake 60, page 120 |
| `blocking-unscheduled` (`waitdiscipline`) | a peer's `session-blocked` wait names this role's item and the item is not in its queue | wake 30, page 120 |
| `relay-dead` (`relay`) | the wake relay's waiter is gone | page 10, then every 6 hours |

A role waiting on the maintainer is woken only by `wait-expired`, and the maintainer gets one daily
digest page listing each role's open ask.

**Direct session wake.** Every wake the quorum or a sensor decides goes through one interface,
`Backend.wake(spec, role, text)` in `shoal/backends.py`, which returns the channel it used. Each
harness implements it in its own way:

| harness | watcher alive | watcher dead |
| --- | --- | --- |
| opencode | a direct send to the session | the same |
| Claude Code | a wake-request file `/tmp/<id>/wake-<slug>`, delivered by the role's own `ledger-watch` and acknowledged by its next `queue` write | the wake relay: `relay-watch` exits with wake lines, and the `wake-relay` session delivers each as a cross-session message |

Claude needs the relay because a headless send to a running Claude session forks it instead of
waking it. A harness with no implementation returns `no-adapter`, and the condition escalates to a
page. Adding a harness means adding its `wake`, not changing the quorum.

**Pages.** A page is always a ledger `msg` to `user`. With `features.health.page =
"gh-issue:<owner/repo>#<n>"` it is also a comment on that issue (the inbox), which reaches the
maintainer through GitHub notifications. Pages are deferred while the maintainer is offline.

The lanes and pipeline sensors route through the same adapters: pipeline starvation wakes the
roles waiting on remote CI, and a harness server respawn wakes every opencode role.

## 8. Host configuration and providers

Every host-specific value resolves in one order: an environment variable, then
`config/host.toml` at the repository root (gitignored; `SHOAL_CONFIG` points elsewhere), then a
generic default. Each `SHOAL_*` variable also honours its legacy `HAG_*` name.

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

`doctor` prints every resolved value. The providers (resource leases, wait targets, the board
source, harness adapters) and their contracts are in [`providers.md`](providers.md).

**Harness-specific verbs**, the only non-neutral surface:

| verb | Claude Code | opencode |
| --- | --- | --- |
| start a session | `claude --bg` with the brief | a server session, the brief as its first prompt |
| wake own session from its watcher | the background command exits (task notification) | a send to its own session id |
| session wake (`Backend.wake`) | wake-request file, else the wake relay | a send |
| liveness, between turns | `claude agents --json` | the server's session status |

## 9. Telemetry and reports

- **Streams:** `session_transition` events from `queue` and `transition`; `watch_event`s for
  ledger wakes, health actions (condition and ladder step), job starts and verdicts, and digest
  refreshes; reconcile ground truth from GitHub (timer at 04:07 and 16:07); and the ledger itself.
- **Pin:** each event carries a `run` pin: the protocol skill's blob sha, the scripts' shas and the
  schema version, read from the checkout the package runs from.
- **Identity:** time in state is keyed by role slug; anything unresolvable counts as one
  `unresolved` bucket, never a row.
- **Reports** (`report`, and a weekly timer): outcomes, time in state, transition quality, wait
  discipline, wake efficiency, ledger economics, health actions, job units, digest refreshes, host
  memory-pressure fires and harness reaps, and a verdict section.

## 10. Live upgrades

`shoal upgrade --spec <spec> [--dry-run]` says how each change reaches a running group before it
changes anything:

| class | what | how it arrives |
| --- | --- | --- |
| live | code loaded per run or invocation (health, digest, matcher, report, `next`, `queue`, `show`) | the next timer run or command |
| re-arm | code a running watcher executes (`ledger-watch`, `relay-watch`) | `--rearm lazy` (default): at each role's next wake; `--rearm now`: one wake per stale watcher |
| brief | `briefs.py` text | the next restart; `next` and `queue` output carry rule changes now |
| restart | a spec change a live session depends on (model, harness) | never done by `upgrade`; `restart --only` does it |

The command refuses a dirty package tree, runs the three selftests, diffs the package against the
spec's recorded `tooling.sha`, applies `--enable`, `--disable` and `--set` feature edits, records
the new sha with a ledger `system` event, and re-arms per `--rearm`.

## 11. Feature flags

A spec's `features` object switches components on per group: `workwake` (`true`, or an object
with `next_max`), `health` (`validity_wake_min`, `page`), `continuation` (`after_min`),
`waitdiscipline` (`rescan_cap_min`), `relay` (the Claude wake relay), `jobs`, `digest`, and
`matcher` (`live`, `model`, `free_recheck_min`, `stale_hours`, `go_fallbacks`, `paid_fallback`,
`private_repos`). An absent flag leaves the component off.

## 12. Known limitations

What this design does not handle yet, and the measurement that would trigger each change, is in
[`limitations.md`](limitations.md).
