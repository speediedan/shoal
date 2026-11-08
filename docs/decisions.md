# Decisions

The standing design decisions behind SHOAL. Each entry states the decision as it holds now, the
reasoning (with the measurement that settled it, where one did), and the alternatives that were
rejected, each with its reason. This is the only document in the repository that describes designs
the system does not use, and it describes them only as rejected alternatives. `guards.md` lists the
failure classes these decisions guard against.

## All traffic is one append-only ledger, one writer per file

**Decision.** Every piece of coordination traffic (offers, claims, releases, completions, queue
versions, mail, posture, system notices) is an append-only JSONL event in a git repository, with
one file per writer per group. Each `shoal` verb writes one event as one commit and one push, and
readers fold the files into a projection. The human-readable form is `shoal show`, not the files.

**Why.**
- `git pull --rebase` cannot conflict when no two writers touch one file, so no agent ever
  resolves a merge. Two files are shared (`user`, the maintainer from any host; `shoal`, the tooling
  on every host): they are append-only, so they merge by union (`.gitattributes`). Addendum
  2026-10-19: two hosts writing `user` before a sync did conflict and wedged a clone until that rule. The merged state is the union of every writer's events, which is why two
  sessions can never overwrite each other.
- Append-only history is the audit trail the report mines, and a bad line can be flagged without
  rewriting anything.
- It works from any host with a git remote, needs no daemon, and is readable at rest.
- One grammar serves every reader (agents, watchers, lint, report, `show`).
- The latency cost is a pull interval of one to two minutes, against waits measured in hours.

**Rejected alternatives.**
- **Direct session-to-session messaging:** delivery is invisible to the sender. A message sent
  into a session whose server had restarted, or to a session sharing a name with another, reads as
  delivered and is never seen, and each harness needs its own transport. Acknowledgement numbering
  added cost without making delivery observable.
- **A shared queue file per role (plan section plus status section):** two writers per file needs
  an ownership gate on every commit, every refill renumbers items, agent commits land in a design
  repository's history and fight human edits, and the sequence of events had to be reconstructed
  from git history.
- **A lightweight network store (a rendezvous key-value store, or a small HTTP service):** needs a
  daemon on one host, port exposure and authentication for remote agents, and keeps no durable,
  readable audit trail. Its latency advantage buys nothing against hour-scale waits.
- **DHT or gossip:** daemons on every peer, no readability, no audit, and complexity out of
  proportion to a handful of agents.
- **A message broker (streams, pub/sub):** a daemon plus a client library, opaque at rest, and
  remote agents need network exposure; git gives the same at-least-once semantics with no
  operations burden.
- **Issue comments as the per-role queue:** rate-limited and unwatchable without polling the API.
  The issue tracker is used as the priority table instead.
- **A SQLite job table:** needs a shared filesystem or a server for remote agents; the git ledger
  already gives first-landed-wins claims through the push.

## A role's queue is an event stream only that role writes

**Decision.** A role declares its queue as `queue` events in its own ledger file: one active item,
up to N next items (`features.workwake.next_max`, default 3), recorded asks and its posture. The latest version is the role's queue.
Others influence it only by offering or withdrawing work.

**Why.** A queue anyone could write has several writers, which reopens the lost-write and
rebase-conflict problems the ledger closed. The chatter that motivated a separate queue came from
the wake rule, not from where the queue was stored.

**Rejected alternatives.**
- **A separate queue file the maintainer and the role both edit:** several writers per file, and
  a second transport to sync, lint and permission-check.
- **A queue implied by claims and offers alone:** the role's own ordering and its posture would
  exist only in its transcript, invisible to health checks and to successors.

## Permissions are enforced at read time

**Decision.** Every projection ignores a line its writer was not allowed to write, and `ledger-lint`
lists the violations. The verbs refuse most such writes up front (a claim on an item that is held,
done or offered elsewhere), but a line that lands anyway is ignored by every reader alike.

**Why.** If some readers rejected a bad line and others honoured it, the group would disagree about
the state. Read-time enforcement makes every reader compute the same projection from the same
files, and needs no server to reject writes.

**Rejected alternatives.**
- **Write-time rejection by a server or hook:** needs a central gatekeeper, and a line that slips
  past it (a manual edit, a stale client) would still be honoured by some readers.

## No coordinator: every role is a peer

**Decision.** A group has no coordinator role. Work arrives as offers from the maintainer, or from
the priority table for roles that fill themselves. A role transfers its own claim to a named peer
with `release --kind handoff --to <role>`, which is a transfer, not coordination. Nobody relays
tasking, status or grants.

**Why.**
- The measured failures of coordinated groups were in the relay: instructions written assuming
  delivery, sends into restarted servers, acknowledgements from the wrong session, and a
  coordinator that emitted one posture transition in 91 hours, which left two of four roles
  unmeasurable.
- A relayed authorization is unverifiable from where the recipient sits. A lane correctly declined
  a relayed grant twice, and the second, more emphatic relay was no more verifiable than the first.
- A coordinator is a single point of stall: when it is slow or offline, every lane idles.
- Coordination through traces in a shared ledger (stigmergy) needs no party to hold the ordering;
  the priority table and each role's own queue do.

**Rejected alternatives.**
- **A coordinator role that fills queues and relays:** the relay layer is where the failures
  were, and it cannot carry an authorization.
- **A worker with elevated permission to fill others' queues:** kept the coordinator's
  write path without its value, and every transfer between peers can be expressed as a handoff.
- **Peers writing into each other's queues:** reopens multi-writer queues and lets a role assign
  work its peer never agreed to.

## Wakes are work-only; messages are mail

**Decision.** A role's own ledger watcher wakes it only for new work while it is idle (or a `--now`
offer), board candidates while it is idle with nothing queued, a change to its active item, a wait
it armed, or a health wake. Messages never wake anyone, broadcasts included, except a maintainer
message sent with `--wake`. Mail is read at the next boundary in `next`. The model never polls.

**Why.** Every wake is a full model turn over the session's whole context. One night of
broadcast discussion produced 53 broadcasts, each waking every role. Offers to a busy role fold in
at its next boundary at no cost. Work meant for a role reaches it as an offer or a handoff, both
of which wake it when idle.

**Rejected alternatives.**
- **Periodic polling by the model:** pays a full-context turn per poll to learn, nearly always,
  that nothing changed.
- **A scheduled self-check prompt (a deadman):** the same cost as polling. An hourly check costs
  about two dozen full-context turns a day per role, each needing several forward passes and each
  growing the context it re-reads.
- **Waking on every message or broadcast:** converts the wake discipline back into polling; N
  roles pay N turns for one message.
- **A longer watcher interval to cut wakes:** trades tokens for latency without removing the
  wakes that carry no work.

## Recovery is a health quorum of two timers

**Decision.** Two identical timers per host run `shoal health`, offset from each other, sharing one
state file under a lock so whichever runs first acts. Each ticks and checks the other's tick, and
pages once if its peer is silent. The health process evaluates a fixed table of conditions per
role, each keyed by its evidence, and climbs a ladder: wake, wake again, page the maintainer. It
uses no model tokens unless it sends a wake.

**Why.**
- Detection belongs in a cheap process, not in model turns.
- Two instances, each proving the other alive, make a dead detector visible instead of reading as
  a quiet world.
- Keying each condition instance by its evidence (an offer id, a dead pid, a wait target, a
  boundary) means the same condition never escalates twice, and new evidence starts a fresh ladder.
- A ledger write proves the writer alive, not that its posture is true: a role re-emitting
  `active` on every wake looked fresh for hours while it sat idle. So conditions read posture
  against session state, not freshness alone.

**Rejected alternatives.**
- **A heartbeat daemon paging on stale heartbeats:** freshness said nothing about whether the row
  was true, and its stall messages went to the suspect role through the very watcher that might
  be dead.
- **A per-session deadman prompt:** costs model turns on every role every hour (see work-only
  wakes).
- **A single timer:** its death is silent.
- **Sensors each paging on their own:** separate pagers with separate policies; the sensors now
  route through the health adapters instead.

## Every turn ends in a sleep posture, and a stopped `active` turn is continued

**Decision.** The last write of every turn is `queue --wait ...` (with the waiter armed first),
`queue --idle`, or `queue --active` followed by more work in the same turn. When a role's posture
is `active` and its session is proven between turns for a few minutes (and not blocked on a
question to the maintainer), the health quorum wakes it to continue (`turn-ended-active`).

**Why.** A model turn ends whenever the model stops calling tools, whatever the instructions say.
A rule of "keep working without stopping" guarantees the stopped-while-active posture sooner or
later, and nothing reaches an `active` role: offers wake only idle roles and mail wakes nobody.
The only earlier detector was a crash catcher whose first wake came after 90 minutes.

**Rejected alternatives.**
- **An instruction alone:** unenforceable, since the turn ends regardless.
- **The 90-minute crash catcher alone:** kept as the fallback when continuation is off, but it
  leaves work in hand for an hour and a half.
- **Treating every ledger write as proof of progress:** a role re-emitting `active` looks fresh
  indefinitely.

## Every wait names what clears it, and every wait expires

**Decision.** A maintainer wait requires a recorded ask. A peer wait requires `--blocked-on
<role>[:<item>]`, and the blocker is told. A CI, lease or job wait carries a checkable `--target`.
Rescans are capped per substate, and an expired rescan is a health condition for every substate.
Held claims are scheduled or released.

**Why.** Two roles sat about 14 hours on self-chosen 12-hour rescans while every health signal read
green: one waited on a peer that did not know, and one on a maintainer who had never been asked.
Replayed on that day's data, the discipline would have woken the blocker within half an hour and
flagged the maintainer wait at the four-hour cap instead of never.

**Rejected alternatives.**
- **Rescans chosen freely by each role:** a working expiry signal would still have been half a
  day late.
- **Maintainer waits without a raised ask:** a wait nobody can end.
- **Relying on the waiting role to nudge its peer by message:** mail does not wake, and the peer
  sees nothing until its next boundary.

## Claude Code roles are woken directly, by resuming the session

**Decision.** For Claude Code roles, a health wake is a wake-request file the role's own watcher
picks up. The watcher runs detached and delivers each wake directly: a session with no worker is
resumed (`claude --bg --resume <session> <text>`), and a live session idle at its prompt with
nothing in flight is stopped and then resumed. When the watcher is dead, the health quorum delivers
the same way, or re-arms the detached watcher itself. A session mid-turn, at a question, or with
work in flight is not touched: the wake is deferred to a later tick. Direct wake is the only Claude
wake path; `features.health.direct_wake: dead` narrows it to sessions with no worker.

**Why.** A resume of a session whose worker is gone continues it in place, under the same id and
with most of its prompt cache (verified 2026-10-20); it forks a copy only while a worker runs, which
is why a live session is stopped first and any copy that appears is removed. A direct-wake trial of
about 95 hours on two live groups, with the relay fallback off, lost no wake, and both relays were
then retired (2026-11). A direct wake costs one turn, the role's own.

**Rejected alternatives.**
- **A wake relay session** (a Claude role with `fill=none` whose waiter, `relay-watch`, exited with
  queued wakes it then delivered as cross-session messages; retired 2026-11): two turns per wake
  (the relay's, then the role's), and a single point of failure for every Claude role in a group.
  Measured 2026-10-02: the background service reaped every worker, the relay among them, and
  `relay-dead` paged for six days while every wake it would have carried went nowhere. It also
  reached only sessions on its own host, and needed its own relaunch and re-arm machinery. Its one
  remaining advantage, reaching a live session with background work in flight sooner than that work
  ends, is bounded by the harness's 2 h background-command limit and did not show up as a lost wake.
- **Paging the maintainer directly whenever a Claude Code watcher is dead:** turns a routine
  recovery into human work.
- **Resuming a session while its worker runs:** forks a copy that acts as the role beside it.
- **Watching a process with an expiring monitor:** every expiry is a turn. A relay built that way
  spent 21 turns in two hours re-arming a monitor over a healthy process and relayed nothing.

## Long work runs as job units, not harness background commands

**Decision.** Work whose loss costs anything (test suites, renders, GPU work under a lease) runs
through `shoal job run`, as a user-scope transient service with a memory cap, followed by a
background `shoal job wait` that prints one verdict line. Watchers stay tiny and idempotent.

**Why.** A harness can kill an idle session's background commands on a host-wide memory-pressure
signal, and that signal can fire for every session on the host at once while plenty of memory is
still available. A job unit lives outside the harness's process registry and survives any reap;
re-arming its waiter re-attaches with no side effect. The memory cap also keeps one runaway suite
from pushing the host into kernel OOM kills that take other work down.

**Rejected alternatives.**
- **Harness background commands for long work:** reapable, and the work dies with the watcher.
- **Detached processes that write a log:** nothing wakes the session, and an idle session never
  reads the log.
- **A foreground wait inside the turn for long work:** holds a turn open for hours.

## The harness's memory-pressure reaper stays enabled

**Decision.** SHOAL does not disable the harness's background-shell reaper. It places work so that
a reap costs nothing: watchers are cheap to re-arm, jobs are outside the harness, and a `killed`
notification is itself a wake.

**Why.** With current harness versions the reaper fires only near host OOM, where it protects the
harness itself. Disabling it would trade watcher deaths for harness OOMs.

**Rejected alternatives.**
- **The undocumented opt-out:** removes the harness's own protection near OOM, and is unsupported.
- **Inferring kills from watcher exits:** watchers exit by design on every wake; kills are counted
  only from harness notifications with status `killed` (`shoal kill-audit`).

## Routing is soft, with one hard exclusion

**Decision.** A role's working context is a short brief plus a `never` list. Routing ranks items;
nothing is hidden by fit except `never`, which is reserved for safety.

**Why.** Under a hard scope filter with a per-role board walk, five roles over 18 hours produced 126 declines against 25
claims, 123 of them out of lane, and 52 items were declined by two or more roles. Replaying soft
contexts over that history kept 25 of 28 historical claims and surfaced only 16 of 124 historical
declines.

**Rejected alternatives.**
- **A hard per-role scope filter:** brittle, and silent about what it hides.
- **Weighting a repository several lanes share:** it cannot separate those lanes and lifts every
  item in it.

## One board read per group

**Decision.** A digest timer reads the priority table once per group, on a short interval and on
demand, and writes one ranked file. Watchers and `next` read that file, never the tracker.

**Why.** Every role walking the board on its own multiplied API reads and produced the decline
storm above. One read also gives one place to apply decline routing and list unowned items.

**Rejected alternatives.**
- **Each role's watcher walking the board:** N reads per interval, and N independent chances to
  surface the same out-of-lane item.

## A semantic matcher ranks the board, not keyword scoring

**Decision.** A model scores each open item against each lane's brief (fit 0 to 3, a class, a
prose blocker and a short reason). Results are cached per item content and brief, so a quiet board
costs no call. It runs first in shadow, and switches live only when a comparison on the ledger's
own history meets the bar.

**Why.**
- Keyword and affinity weights show a lane with nothing matching its leftovers at score 0 in board
  order, which is unrelated work.
- One call routed a 65-item board in about 100 seconds; a cached quiet board took no calls.
- In the shadow comparison the semantic ranking matched the keyword ranking on claim recall and cut
  out-of-lane leakage from 0.115 to 0.049.
- The switch bar is measured, not judged: semantic recall at least keyword recall, semantic leakage
  at most keyword leakage.

**Rejected alternatives.**
- **Affinity, keyword and path weights:** hand-tuned per lane, and they surface unrelated items
  once a lane's own items are taken. Kept only as the fallback when nothing is scored.
- **Switching without a shadow run:** no measured before-state to judge the switch.

Private-repository items are sent to the matcher by default; `private_repos: "withhold"` keeps
them out, at the cost of leaving them unscored.

## Effective priority, never a reordered board

**Decision.** The digest computes an effective position per item: an item that blocks an active
role's work is lifted to just above what it blocks, an item named by a peer's `--blocked-on` wait
goes to the top, and an item with an open blocker sinks below every workable item. The board
itself is never reordered.

**Why.** Board order is the maintainer's only direct priority input. A model-reordered board makes
that input circular (the matcher's output becomes its input), silently overwrites maintainer
decisions, and adds noise to the project history.

**Rejected alternatives.**
- **A daemon that reorders the board:** the reasons above.
- **A suggested reorder the maintainer applies:** not built; worth building only if the maintainer
  finds themselves reordering by hand to match what the digest already knows.

## Free models run through `opencode run`

**Decision.** The matcher reaches free models by running `opencode run` with the prompt attached as
a file, in its own process group under a probe timeout, and deletes the session each run creates.
A direct OpenAI-compatible API call is used only for a paid balance tier.

**Why.** The free tier refuses direct API calls; it can only be used from inside the opencode
client, whatever key the caller holds. A 25 kB prompt passed as a command argument hung for ten
minutes without creating a session; attached as a file it completed. A refused request can hang
silently, so runs are killed whole on timeout.

**Rejected alternatives.**
- **Direct API calls for every tier:** cannot use free models at all.
- **Passing the prompt as an argument:** hangs on long prompts.
- **Leaving run sessions in place:** each run leaves a session in the client's store.

## Model fallback is tiered, cheapest first

**Decision.** Each matcher run that needs a call probes the model that last answered, then the free
tier (configured models, then free models discovered from the provider's model list and pricing
table), then a subscription tier, then a paid balance. While a paid tier is active the free tier is
re-probed hourly. A switch warns, writes a ledger `system` event and pages at most once a day. When
nothing answers, cached fits up to a day old are reused, then items go unscored.

**Why.** Free models rotate: a provider's default free model became unavailable without notice. A
subscription model agreed with the free model on 99% of fits (within one point) at about a minute
per 40-item chunk, and costs nothing per call once subscribed, so it comes before a per-token
balance.

**Rejected alternatives.**
- **One fixed model:** routing stops when it is withdrawn.
- **Paid first:** spends money the free tier would have saved.
- **Balance before subscription:** pays per call for what the subscription already covers.

## Asks are raised first-person, as question sets built from live data

**Decision.** Only the session that needs a decision asks for it, as a prompted question with
options including a decline or hold, at the moment it blocks. The question sets are built by `shoal
asks --question-json` from live PR data, at most four questions per set. Every merge ask offers
grant, defer, delegate and deny, and names the work that continues meanwhile. Authorizations do not
transfer: a successor session asks for its own grants in one question before any work.

**Why.**
- A relayed grant cannot be verified by the session that must act on it.
- A question hand-labelled from memory described one PR with another's summary, and the grant
  merged the PR under the wrong subject and body.
- A question inside a status report is not a question: it defers the decision into a document the
  maintainer reads later, strips its context, and brings the answer back as prose to interpret.

**Rejected alternatives.**
- **Relayed grants:** unverifiable where they land, however carefully written.
- **Hand-written question labels:** can describe the wrong item.
- **Questions in status reports or "waiting on you" rows:** never reach the maintainer as a
  decision.
- **A bare merge-or-nothing choice:** stalls the role on the answer instead of letting it continue
  stacked work.

## User-offline mode

**Decision.** `shoal offline on` tells every role the maintainer is away. Roles never wait on the
maintainer while it is on: they record each blocking ask with `queue --ask`, stack work that
depends on unmerged changes, and take the next unblocked item. Every wake carries the offline
banner, `shoal asks` refuses to build questions, and maintainer pages are deferred. `shoal offline
off` starts a return protocol: each role re-verifies its asks, drops stale ones with a reason, and
raises the rest.

**Why.** A blocking question tool holds the turn until answered. A role that raised a question
while the maintainer was away went dark for over eight hours with wakes queued and no ledger write.
Recording instead of asking lets the work continue and delivers every question in one pass on
return.

**Rejected alternatives.**
- **Raising prompts at the block regardless of the hour:** a blocking question can freeze a role
  for the whole absence.
- **Idling until the maintainer returns:** the absence buys no progress.
- **Pages delivered while offline:** a pile of stale pages instead of one summary.

## A residual aux lane takes what no lane covers

**Decision.** An optional self-filling role with `aux: true` and no brief takes free `workable`
items ranked by the lowest maximum fit over the other active roles, with items that unblock an
active lane first. It never takes an item another active lane fits strongly unless that lane
declined it. A parked lane's items become aux territory until the lane returns.

**Why.** Once the lanes' own items are taken, useful work (hygiene, tests, small fixes) that no
brief covers sits unowned. Unblockers raise every other lane's throughput, and the ranking reads the
matrix the matcher already computes, so it costs no extra call. The ranking follows the roster, so a
running aux session never needs a new brief.

**Rejected alternatives.**
- **Work-stealing from lanes with long waits and deep queues:** overlaps lane judgment and invites
  contested claims.
- **Leaving unowned items for the lanes' keyword fallback:** surfaces unrelated work at score 0.
- **Keyword ranking for the aux lane:** meaningless for a lane with no brief, so aux reads the
  semantic ranking even while the matcher is in shadow.

## Changes reach a running group by rollout class

**Decision.** `shoal upgrade` classifies each tooling change and delivers it accordingly: `live`
(code loaded per run or invocation arrives with the next run), `re-arm` (code a running watcher
executes arrives at each role's next wake, or at once on request), `brief` (text arrives at the
next restart, with rule changes carried in `next` and `queue` output meanwhile), and `restart` (a
spec change a live session depends on, which `upgrade` names and never performs). It refuses a
dirty tooling tree, runs the selftests, and records the sha.

**Why.** Most changes need no restart. A restart bounces every session and costs each one a fresh
grants ask, while a running watcher silently keeps the code it loaded at spawn. Since every wake
ends in a re-arm, a lazy re-arm reaches every role at no extra wake. Recording the sha from a clean
tree means the recorded version names the code that runs.

**Rejected alternatives.**
- **Restart for every change:** needless disruption for changes that load per run.
- **Changing code without recording it:** a stale watcher and a fresh one become
  indistinguishable, and diagnosis cannot trust the deployed version.
- **Waking every role to re-arm at once by default:** spends a wake per role for code that would
  have arrived at its next natural wake.

## Leases and wait targets are provider contracts

**Decision.** SHOAL talks to host facilities only through narrow contracts. A lease provider is any
command whose `--status --json` reports holders and waiters per key. A wait target `<kind>:<ref>`
resolves through a provider to `pending`, `resolved` or `unknown`. A provider that cannot answer
reports unknown, never resolved, and an unknown kind is covered only by the periodic validity wake.

**Why.** Different hosts serialize resources differently (a file lock, a cluster scheduler, a lease
object) and use different CI systems. A contract lets each environment supply its own
implementation without changing SHOAL, and "unknown, never resolved" keeps a broken provider from
declaring a wait finished.

**Rejected alternatives.**
- **Calling one host's lease script and one CI system directly:** ties the system to a single
  host.
- **Treating a provider failure as resolved:** wakes roles into work that is not ready.

## A console script and config, not baked absolute paths

**Decision.** SHOAL installs as a package with a `shoal` console script on PATH. Host values (data
root, ledger location and remote, harness tool PATH, lease provider, page channel, key file
location) come from the environment, then a gitignored host config file, then generic defaults. No
default names a real host, user or private repository.

**Why.** Absolute paths baked into briefs, units and code had multiplied into several copies of the
same value, and a refactor that moved a script left timer jobs pointing at a file that no longer
existed. A command name survives relocation, and one config module is one place to change a value.

**Rejected alternatives.**
- **Absolute script paths in briefs and units:** break on every move, and differ per host.

## Pages go to a channel the maintainer is notified on

**Decision.** A page is always a ledger message to `user`. When `health.page` names an issue, every
page is also a comment on it, which the maintainer's notifications deliver.

**Why.** Pages written only to the ledger went unread overnight: seven pages and a digest naming a
stuck wait were seen only the next day.

**Rejected alternatives.**
- **Ledger-only pages:** nobody reads the ledger in time.

## Completion is verified against the tracker

**Decision.** A `done` event is a claim with evidence. Reconcile reads merged PRs and closed issues
from GitHub twice a day, and the report counts dones it could not verify.

**Why.** A role's own record of completion is the thing being checked, so it cannot also be the
evidence. Only a ground-truth read separates work that landed from work a role believes landed.

**Rejected alternatives.**
- **Trusting `done` events:** measures what roles said, not what landed.

## Job waiters stay under the harness's control

**Decision (2026-11-08).** A Claude role keeps following its job with `shoal job wait` as a harness background command.
Shoal does not move job waiters out of the session to make every wake deliverable. A wake that arrives while background
work is in flight is deferred until that work ends; the return to online reaches roles on every host through each host's
own quorum (`asks-on-return`); and a role's recorded asks are shown on its row, so the maintainer can answer them without
waiting for the role to raise them.

**Why.**
- **Measured cost of the deferral:** on one host, 9 deferral episodes across 3 roles from 2026-11-05 to 11-08, 6 of
  them over 30 minutes, the longest about 90 minutes. Real, but bounded by the waiter's own end.
- **Tokens: expected roughly neutral, not measured.** Each job end costs one turn either way, a task notification or a
  watcher wake through resume. A long job's prompt cache has expired either way. Short jobs would pay a little more:
  up to about 4 minutes of watcher latency, then a stop and resume.
- **Reliability:** the harness's own notification is the most reliable wake path there is. Moving waiters out trades it
  for the watcher, direct wakes and their failure modes (orphaned waiters, a failed resume, usage-limit holds), and slows
  a job-end wake from seconds to minutes.
- **Ecosystem neutrality, the deciding point:** each harness waits differently (Claude background commands, Codex, Muse,
  opencode). Shoal owning the waiters would mean altering each harness's wait procedure, a per-ecosystem adapter that
  would be fragile and would pull the protocol away from neutral.

**Rejected alternatives.**
- **Detached waiters for every role:** the costs above, with the watcher also needing to follow remote jobs over ssh.
- **Stopping and resuming a session with work in flight after a deferral cap:** shoal cannot see what a background task
  is, so it could cut off a real build rather than a waiter.

**Revisit when** minute-level urgency for a maintainer message or offer to a busy role starts to matter, or the measured
deferrals grow. Trial detached waiters on one role for a day first, and measure turns and tokens per job end.

## Blockers are verified, and idle is checked against the board

**Decision.** With `blockers`, a next-item blocker is checked by kind every time it is read: a peer
blocker clears when its item is done, a provider blocker when the provider says so, a maintainer
blocker only while an ask is recorded, and a free-text hold expires and must be re-recorded. A stale
blocker parks nothing. Idle is refused while the board offers strong-fit candidates the role has not
passed, and a watcher wakes an idle role for candidates when all its next items are blocked.

**Why.** Measured 2026-09-29 on a live group: two lanes idle for 2.5 and 11.5 hours with every next
item "blocked". Every blocker was stale: a peer dependency whose owner had messaged that it was
clear, a design question answered two days earlier, and holds citing an offline mode and a lease
that had both ended. Beside them the digest held 2 and 8 eligible candidates that `next` never
showed. The idle gate checked that a blocker was recorded, not that it held, and a free-text reason
is exactly what a model reaches for to end a turn.

**Rejected alternatives.**
- **Banning `hold:`:** some blockers are real and uncheckable (a design direction nobody has given
  yet). Expiry keeps them expressible while making them cost a re-verification.
- **Waking on every stale blocker immediately:** the existing `parked-work` ladder already escalates
  wake, wake, page; stale blockers join it rather than adding a second ladder. Revised 2026-11-06
  for one case. `wake_off` strips that ladder to its page, and it is right to for a blocker that
  merely expired. A role or provider blocker that cleared is different: nothing else wakes the role
  (the watcher has no such event), and only the role is needed to look. Measured: a peer finished the
  blocking item, its mail saying the maintainer had deferred the next one did not wake the role, the
  role slept on behind a stale blocker, and the next signal would have been a page to the maintainer
  at 180 minutes. So that case alone wakes once, as `blocker-resolved`, and an expired blocker still
  joins `parked-work`. The wake says to look, not to start: a cleared blocker is not a go.
- **A scheduled self-check prompt:** costs tokens every cycle on every lane; the checks here run in
  the token-free quorum and at the role's own boundaries.

