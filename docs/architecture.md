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
| role start doc | each role's `plan_state.md` | always | mission, current state, `grants-needed` |
| briefs | `shoal/briefs.py` | always | the first prompt of every session, identical in shape for every role |
| work loop | `shoal/worker.py` (`next`, `queue`, `asks`, `offline`, `freeze`) | `workwake` | one read and one write per boundary |
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
| operator UI | `shoal/ui/` (`ui status`, `ui line`, `view`, `ui open`, `ui feed`, `ui menu`), `plugins/shoal-herdr/` | run by the operator | a read-only status contract for every shoal on a host, a viewer that never resumes, and the Herdr adapter (section 12) |

**Where state lives.**

- **Ledger repository** (a git repository every host clones, `ledger_repo` / `ledger_dir`): the
  spec (`shoal.json`) and the ledger, under `shoals/<id>/`. `ledger-init` (on a new or an existing
  ledger) makes its `.gitignore` name the clone's flock file `.shoal-lock` and `*.tmp`, keeping
  existing lines and never duplicating one, and commits the change: an untracked lock file makes
  every rebase, and so every pull, refuse.
- **Data root** (`data_root`, never committed), per group under `<data_root>/<id>/`: `contexts/`,
  `roles/<slug>/plan_state.md` (generated start docs for the wake relay and the residual lane),
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
  candidates, or records why fewer. `next` shows candidates whenever the queue has an open slot: a
  next item with a recorded blocker holds no slot, and a role with no active item has one more.
  Without `--reason`, a short queue's default reason is checked against the candidates (`short
  queue: N eligible candidate(s) not taken` when there are any).

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
STALE`, the idle gate and `parked-work` treat its item as free, and recording a blocker that has
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

**Freeze mode.** `freeze on|off` is the same kind of `system` event, for a restart or maintenance
window. While it is on, `next` prints `FREEZE ACTIVE` (checkpoint, persist the plan_state, push,
sleep), `job run` refuses to start a unit, idle is allowed with a full queue, and `parked-work` is
suppressed. For 24 hours after `off`, `next` prints `FREEZE LIFTED`. Both edges wake every role. A
freeze is a mode rather than mail because a freeze message has no end: measured 2026-09-27, two
lanes kept honoring one for a day after the window closed, idle on full queues.

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
- with `waittargets`, a `local-ci` or `remote-ci` wait needs a checkable target, and the role's
  `ledger-watch` wakes it when a `job:` or `lease:` target resolves. The wake comes on the second
  tick that sees the target resolved, so a session's own waiter acts first. Measured 2026-10-01: a
  detached job waiter reached no session, and the role slept for an hour past its job's end;
- `queue` refuses a finished item in `next` (`done` by any role), and `next` names it `GONE`;
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
| `parked-work` | idle, or waiting on anything but `local-ci`, while a next item has no recorded blocker, or (`blockers`) only a stale one (not `fill=none`, not during a freeze); keyed by the items | wake 30, wake 90, page 180 |
| `relay-dead` (`relay`) | the wake relay's waiter is gone | page 10, then every 6 hours; with `health.revive`: relaunch at sighting, page 15, then every 6 hours |
| `hold-long` (`blockers`) | a `hold:` blocker kept past `hold_page_min` since it first blocked the item; keyed by item and first time | wake at sighting (convert it to an ask or a checkable blocker), page 240 later |

A role waiting on the maintainer is woken only by `wait-expired`, and the maintainer gets one daily
digest page listing each role's open ask.

**Parked roles.** A role with no `session` in the spec (parked or retired) raises no condition, is
left out of the daily digest, and reads `parked (no session)` in the heartbeat classification;
`doctor` reports its heartbeat and `ledger-watch` rows as INFO, never WARN or FAIL, whatever a
leftover sidecar says.

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
| Claude Code | a wake-request file `/tmp/<id>/wake-<slug>`, delivered by the role's own `ledger-watch` and acknowledged by its next `queue` write | the wake relay: `relay-watch` exits with wake lines, and the `wake-relay` session delivers each as a cross-session message |

With `health.revive`, a Claude role whose worker process is gone is respawned first: `claude agents
--json --all` lists it with no `pid` (plain `--json` omits it), and `claude respawn` starts a new
worker on the same conversation. Its background commands died with the old worker, so the wake goes
by relay even if an orphaned `ledger-watch` survives. A wake queued while the relay is down is kept
(the quorum relaunches the relay first, and `relay-watch` resumes from its cursor), and the
condition's page step still fires if nothing answers. This covers the Claude background service
restarting under its workers or retiring an idle one (failure mode FM-14). To keep a role down
under `revive`, park it or mark it `offline`: a dead session alone is revived.

Claude needs the relay because a headless send to a running Claude session forks it instead of
waking it. A harness with no implementation returns `no-adapter`, and the condition escalates to a
page. Adding a harness means adding its `wake`, not changing the quorum.

**Pages.** A page is always a ledger `msg` to `user`. `features.health.page` names where else it
lands, as one channel string or a list of them: `"gh-issue:<owner/repo>#<n>"` also comments on
that issue (the inbox, reaching the maintainer through GitHub notifications), and
`"ntfy:<target>"` also publishes to an ntfy topic (a full URL, a `<host>/<topic>`, or a bare
topic on ntfy.sh; an access token rides `$SHOAL_NTFY_TOKEN`, never the spec), which reaches the
maintainer's phone through the ntfy app. Pages are deferred while the maintainer is offline.

The lanes and pipeline sensors route through the same adapters: pipeline starvation wakes the
roles waiting on remote CI, and a harness server respawn wakes every opencode role.

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
  local roles, and `restart --only`, `relay-launch` and `aux-launch` refuse a remote role and print the
  `ssh <server> shoal ...` line. `start` and `add-role` record a remote role and launch only this host's
  roles; a full `restart` on a multi-host shoal restarts this host's roles. Mint on home, then launch each
  remote role on its host. A remote role is watched, woken and launched by its own host.
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
- **The overview** shows `home` and, on a multi-host shoal, each host's last beat in the header and a
  `host` column in the role table; the Herdr sidebar gets a `$host` token (empty on a single-host shoal).
  A viewer gives cards only to the roles on its own host: a session resolves through the local harness
  (`claude agents`, the local OpenChamber), so a remote session cannot be attached from here, and
  `shoal view`, a stale card and the asks popup all refuse to try (answer a remote role by mail).
- **Clocks.** Events are stamped by the writer's clock, the fold orders by them and conditions are ages:
  `doctor` fails a host that is not NTP-synchronized.
- **Per host:** `install-systemd` installs this host's set (health and lanes for its roles; the pipeline
  sensor, digest, reconcile and report on home only); every host with a live role writes a `host-beat`
  system event and home pages `host-silent` for one that stops; a Claude role off the relay's host gets
  the `exit` watcher; `upgrade` off home leaves the spec's tooling and features to home. Still open:
  telemetry merge for reports and per-host relays ([`limitations.md`](limitations.md) section 2). The providers (resource leases, wait targets, the board
source, harness adapters) and their contracts are in [`providers.md`](providers.md).

**Harness-specific verbs**, the only non-neutral surface:

| verb | Claude Code | opencode |
| --- | --- | --- |
| start a session | `claude --bg` with the brief | a server session, the brief as its first prompt |
| wake own session from its watcher | the background command exits (task notification) | a send to its own session id |
| session wake (`Backend.wake`) | wake-request file, else the wake relay | a send |
| liveness, between turns | `claude agents --json` turn `state` (`done` between turns, `working` mid-turn, `blocked` on a question); never `status`, which reads `busy` while any background task (the role's own `ledger-watch`) runs | the server's session status |

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
| re-arm | code a running watcher executes (`ledger-watch`, `relay-watch`) | `--rearm lazy` (default): at each role's next wake; `--rearm now`: one wake per stale `ledger-watch`, and the stale `relay-watch` is killed (see below) |
| brief | `briefs.py` text | the next restart; `next` and `queue` output carry rule changes now |
| restart | a spec change a live session depends on (model, harness) | never done by `upgrade`; `restart --only` does it |
| ui | the operator UI's feeder (`shoal-ui-feed`) | restarted on any module change (it keeps the protocol modules loaded); it only reads, so no session is woken |

The command refuses a dirty package tree, runs the four selftests, diffs the package against the
spec's recorded `tooling.sha`, applies `--enable`, `--disable` and `--set` feature edits, records
the new sha with a ledger `system` event, and re-arms per `--rearm`. It also syncs the user-scope
copies of the package's skills into every `skill_homes` entry: sessions load `shoal-protocol` from
those copies, so a merged protocol change otherwise never reaches them. `doctor` warns while any
copy is stale.

**Relay re-arm.** A `relay-watch` is stale when its pid file (written once at spawn) is older than
the newest change to code it runs. `--rearm now` kills it: killing a Claude background command wakes
its session, and the relay's loop re-arms on a kill. The command then waits up to
`--rearm-verify-s` (default `SHOAL_REARM_VERIFY_S`, else 180 seconds) for a new pid in
`/tmp/<id>/relay-watch.pid` with a live `relay-watch` behind it, and prints a WARN with the exact
re-arm line (and the `relay-launch` fallback) when none appears. `--dry-run` only reports it.

`restart --only <slugs>` relaunches a subset. Its start-doc checks fail only on the roles it
relaunches; a problem in a kept role's doc prints as info. Once any session has launched, the spec
(new ids, seqs, incarnation and generation record) is saved whatever the exit code, so a launched
id is never lost. Effect-verification ignores a `ledger-watch` sidecar bound to a replaced session
or armed before the launch, and adopts a sidecar's id only when it is a longer form of the launched
id (a Claude short id against its full uuid) or the launched id is unknown.

## 11. Feature flags

A spec's `features` object switches components on per group: `workwake` (`true`, or an object
with `next_max`), `health` (`validity_wake_min`, `page`, `revive`), `continuation` (`after_min`),
`waitdiscipline` (`rescan_cap_min`), `relay` (the Claude wake relay; `health.revive` repairs dead Claude workers and a dead relay), `jobs`, `digest`, and
`matcher` (`live`, `model`, `free_recheck_min`, `stale_hours`, `go_fallbacks`, `paid_fallback`,
`private_repos`). An absent flag leaves the component off.

## 12. Operator UI

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
not reported, authentication failures are visible, and Muse's optional missing
quota payload is unavailable rather than a fabricated zero. CLI account APIs
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
  (`health.watcher_dead`, `health.parked_free`), so the two never drift; the UI checks blockers
  without provider calls. `--conditions` adds the quorum's own `evaluate()` output, cached five minutes
  per shoal. It never pulls (unless `--pull`), commits or marks mail read.
- **Viewer** (`shoal/ui/ui_view.py`, `Backend.view_argv`): `view <shoal> <role>` runs the
  harness's own client on the live session (`claude attach`; the opencode TUI as a client of
  OpenChamber's server). It strips `HERDR_*` and `ZELLIJ*` from the client's environment and
  refuses a dead or unlisted session. `--stub` is a role card that attaches only on Enter.
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
- **Rollout:** the UI modules are `live` code, except the feeder, which `upgrade` restarts on any
  module change. No session is woken.

## 13. Known limitations

What this design does not handle yet, and the measurement that would trigger each change, is in
[`limitations.md`](limitations.md).
