# Guards

Failure classes seen in operation, and the guard the system has for each today. Each entry states
the mechanism in general terms, the guard (the verb, check or rule that prevents or detects it) and
the signature an operator sees. Only classes with a guard in the current system appear here.
`decisions.md` records why the guards have the shape they do.

Health conditions named below are evaluated by the health quorum (`shoal health`, two timers). Each
condition climbs a ladder of wakes and ends in a maintainer page; `architecture.md` lists the
ladders.

## Contested claim

- **What goes wrong:** two roles hold live claims on one item and both work it. In the normal race
  this cannot happen; it does when a claimant dies between pushing its claim and re-checking, or
  when a claim is written by hand.
- **Guard:** `shoal claim` (and the claim `queue --active` makes) pulls, checks, appends, pushes,
  pulls and re-checks. A caller whose claim landed second writes `release --kind lost-race`.
  `ledger-lint` reports a surviving pair as E2 naming both holders, the later-landed holder's
  watcher wakes it because its active item changed, and branch commit order is the tie-break.
- **Signature:** `ledger-lint` E2; `next` prints `TAKEN` on the active item; a `lost-race` release
  within one watch interval of the E2.

## Permission violation

- **What goes wrong:** an event its writer may not write: an offer by a role, a claim on an item
  offered to another role, a withdrawal of someone else's offer, or an event in another writer's
  file. If readers disagreed about such a line, one would honour it and another would not.
- **Guard:** permissions are enforced at read time. Every projection ignores the line, so all
  readers agree. `ledger-lint` lists it (E1 for a forbidden event, E3 for an event in the wrong
  writer file). Roles write only through `shoal` verbs, which write only the caller's own file.
- **Signature:** a nonzero violation count in `ledger-lint` and in the report's ledger-lint section.
  The violator sees no error at write time, only in `show` or `ledger-lint`.

## Unworkable item left unannounced

- **What goes wrong:** a role skips an item it cannot or should not work and says nothing, so the
  item looks taken or in progress. The same item then surfaces again to the same role, or to every
  role in turn, and each pays a turn to decline it.
- **Guard:** declines are ledger events. `release --kind decline --reason <why> --retry <condition>`
  or `queue --pass <item>:<class>[:suggest=<role>]` records the class (`out-of-lane`, `blocked`,
  `needs-maintainer`, `needs-hardware`, `duplicate-or-stale`). A passed item never re-surfaces to
  that role, `suggest=` routes it toward another role, items every self-filling role passed are
  listed once as unowned, and the semantic matcher withholds fit-0 items from a lane entirely.
- **Signature:** the report's ledger economics section: decline-to-claim ratio, and the count of
  items declined by two or more roles.

## Offered work not picked up

- **What goes wrong:** an offer lands for an idle role and nothing happens, because the role's
  wake was lost or the role ignored it.
- **Guard:** health condition `unacked-work`: an offer (or a health wake request) to an idle role,
  or a `--now` offer, newer than the role's latest queue version. It wakes the role and then pages.
- **Signature:** `unacked-work` wakes in the report's health-actions section; an offer older than
  the role's last queue version never triggers it.

## Wake chatter

- **What goes wrong:** every wake is a full model turn. Waking roles for conversation, broadcasts,
  board churn or offers that arrive while they are busy turns the wake discipline back into
  polling, and N roles pay N turns for one broadcast.
- **Guard:** wakes are work-only. `ledger-watch` wakes a role only for an offer while it is idle
  (or a `--now` offer), board candidates while it is idle with nothing queued, a lost claim on or
  withdrawal of its active item, or a health wake request. Messages are mail and wake nobody,
  broadcasts included; only a maintainer message sent with `--wake` wakes. Each event id is
  delivered at most once (the watcher's sidecar keeps the delivered set).
- **Signature:** the report's wake-efficiency section: a wake with no ledger action by the woken
  role within 30 minutes is a wasted wake; message-induced wakes should be zero.

## Mail consumed unseen

- **What goes wrong:** `next` marks mail read when it prints it. A role that pipes `next` through
  `head` or `grep` truncates the output, and a message printed below the cut is marked read
  without being seen, so a rule the maintainer sent is never followed.
- **Guard:** `next` prints MAIL right after the identity and offline lines, before the queue, asks
  and board, and prints maintainer mail in full. The protocol says to run `next` unpiped; `next
  --peek` reads without marking anything read.
- **Signature:** a maintainer message whose instruction is visibly not followed in the role's next
  actions.

## Turn ends while the posture is active

- **What goes wrong:** a model turn ends whenever the model stops calling tools, whatever the queue
  says. A role that stops with its posture `active` has work in hand and nothing that will wake it,
  since offers wake only idle roles and mail wakes nobody. Its ledger writes keep its heartbeat
  fresh, so liveness checks read green while the row is false.
- **Guard:** health condition `turn-ended-active` (feature `continuation`): posture `active` and the
  session proven between turns for `after_min` (default 5 minutes) produces a CONTINUE wake, a
  second wake, then a page. A session blocked on a question to the maintainer is not between turns
  and never triggers it. For Claude, "between turns" is the session's turn `state` (`done`), never
  its `status`: `status` reads `busy` while the role's own background `ledger-watch` runs, which
  hid a role that ended its turn while `active` for 6.6 hours (measured 2026-09-29). Without `continuation`, `stale-active` catches the same posture after 90
  minutes. `queue` prints the closing rule whenever it writes `active`, and `next` prints it at
  every boundary: the last write of a turn is `queue --wait ...`, `queue --idle`, or `queue
  --active` followed by more work in the same turn.
- **Signature:** `turn-ended-active` wakes (a healthy role has under two a day); a posted status
  row reading `active` at the end of a turn.

## Wait on a target that already resolved

- **What goes wrong:** a role waits on a CI run, gate, lease or job that has already finished, or
  whose waiter died, and keeps waiting because nothing tells it. A serial resource then sits idle
  while the role believes it is queued.
- **Guard:** `queue --wait ... --target <kind>:<ref>` names a checkable target. Health condition
  `wait-resolved` reads the target's real state through its provider and wakes the role when it is
  no longer pending. Condition `wait-validity` wakes a role on any CI, lease or job wait every
  `validity_wake_min` (default 90) to re-verify it, unless (under wait discipline) its provider
  confirms the target pending. A provider that cannot answer reports unknown, never resolved, and
  the validity wake then says UNVERIFIABLE.
- **Signature:** `wait-resolved` or `wait-validity` wakes in the health-actions section; a waiting
  row whose target reads resolved.

## Wait that never expires

- **What goes wrong:** a role parks on a peer or the maintainer with a long rescan of its own
  choosing, and nothing fires when the rescan passes. Every liveness signal stays green because the
  role is alive, its watcher runs and its posture is honest; the condition simply never clears.
- **Guard:** feature `waitdiscipline`. Rescans are capped per substate (defaults: 240 minutes for
  maintainer and peer waits, 180 for CI, 360 for `queue-empty`). Health condition `wait-expired`
  fires for any waiting posture past its capped rescan. `next` prints `RESCAN EXPIRED`, and the role
  re-verifies, re-raises its ask or pivots to the next unblocked item.
- **Signature:** `wait-expired` wakes; a waiting row past its rescan with no following action.

## Maintainer wait with no raised ask

- **What goes wrong:** a role records "waiting on the maintainer" while the question exists only in
  its own queue text or a status report. The maintainer never sees a question, so the wait cannot
  end.
- **Guard:** `queue --wait` and `transition` refuse a `user-auth`, `user-design` or `user-feedback`
  wait unless the role's queue records the ask (`queue --ask "<question>"`). The protocol pairs the
  record with a first-person prompted question raised in the same turn. The health quorum sends a
  daily page listing each maintainer wait with its ask text, or NO ASK RECORDED. `next` prints ASKS
  TO RAISE while asks are held and the maintainer is online.
- **Signature:** a refused wait (nothing written); NO ASK RECORDED in the daily page.

## Peer wait on an unscheduled item

- **What goes wrong:** role A waits on an item role B holds, but B never schedules it: the claim
  sits outside B's active and next lists, possibly carried over from an earlier generation, and B
  does not know A is waiting. Other roles cannot take the item because it reads as claimed.
- **Guard:** a `session-blocked` wait is refused without `--blocked-on <role>[:<repo>#<n>]`. The
  blocker's `next` and `show` print `BLOCKING <role> on <item>`, and health condition
  `blocking-unscheduled` wakes the blocker when the item is not in its queue. `next`, `show` and
  `queue` flag `HELD-UNSCHEDULED` claims, which the holder schedules or releases with `--kind
  handoff`. With the matcher on, an item named by a peer's `--blocked-on` is lifted to the top of
  the effective priority.
- **Signature:** `BLOCKING` or `HELD-UNSCHEDULED` lines in `next`; `blocking-unscheduled` wakes.

## Transfer attempted by mail

- **What goes wrong:** a role tells another role by message to take over an item. Mail informs but
  does not transfer and does not wake, so the recipient cannot claim the item (it is still held, or
  it was not offered to a role that only takes offers), and the sender may then wait for a claim its
  own claim blocks.
- **Guard:** `release --kind handoff --to <role>` is the transfer. The holder's release becomes an
  implicit offer to the named role, which is woken if idle and claims at its next boundary. It is
  the only way a role gives another role work.
- **Signature:** a message announcing a transfer with no matching handoff release; a holder waiting
  on a claim for an item it still holds.

## Deaf role: watcher dead or never re-armed

- **What goes wrong:** a role's ledger watcher exits (by design, on a wake) and the role acts but
  never re-arms, or the watcher is killed. The role then hears nothing: no offers, no board
  candidates, no health wake requests. A later claim that the watcher is "still armed" is made from
  memory.
- **Guard:** armed means proven: the pid is alive (`ps -p`) and matches the sidecar pid, checked
  every re-arm in the same turn, as two separate commands (read the pid, then `ps -p` it; never
  `$(...)`, which needs a permission prompt under manual permissions). `next` prints `WATCH ledger-watch ... NOT RUNNING` when it is not.
  Health condition `watcher-dead` (pid gone and the session between turns) re-arms a detached
  watcher from the quorum, or wakes the role through its harness adapter (Claude Code: a direct wake
  that resumes the session). A `killed` notification is itself a wake.
- **Signature:** `watcher-dead` wakes; `WATCH ... NOT RUNNING` in `next`; `doctor` flags the
  ledger-watch check.

## Dead timer read as a quiet world

- **What goes wrong:** a timer-driven job (sensor, digest, reconcile, report, or a health instance)
  stops running, after a refactor moved its script or because its unit's PATH lacks a tool. Silence
  from a dead instrument looks the same as silence from a world with nothing to report.
- **Guard:** every timer job writes a tick on every run, and `doctor` fails any tick older than
  three times its interval ("watcher dead, not world quiet"). The two health instances check each
  other's tick and page once if the peer is silent past 15 minutes (`peer-dead`). Units are
  generated from the spec with an explicit tool PATH.
- **Signature:** a `doctor` FAIL on `tick <job>`; a `peer-dead` page.

## Design exit miscounted as a kill

- **What goes wrong:** a watcher that exits by design on every delivered wake is counted as killed,
  and an investigation built on that count chases a kill rate that does not exist. More generally,
  an artifact reads as intact because nothing errored.
- **Guard:** every watcher logs a verdict line on every designed exit (`ledger-watch` prints
  `VERDICT WAKE`, `WAKE-SEND-FAILED` or `INSTRUMENT-FAILED`; `job wait` prints one `JOB-VERDICT`
  line), so a log with armed lines and no verdict proves the termination was external. `shoal
  kill-audit` counts only genuine harness kill notifications (status `killed`, unique per task id),
  never exits and never text quoting a notification.
- **Signature:** a kill tally in which every entry cites a notification; armed lines with no
  verdict in a watcher log.

## Harness reaps long-running work

- **What goes wrong:** a harness kills an idle session's background commands under memory
  pressure. Any local work run that way (a test suite, a render) is lost with its watcher.
- **Guard:** work whose loss costs anything runs as a job unit (`shoal job run --name <n> --
  <cmd>`), a user-scope transient service outside the harness's process registry with a memory
  cap, followed by a background `shoal job wait`. The waiter may be reaped; re-arming it re-attaches
  to the same unit with no side effect. `psi_watch` logs every host memory-pressure fire and
  `kill-audit` joins each genuine kill to the fires before it.
- **Signature:** a `killed` notification followed in the same turn by a re-arm with a proven pid;
  zero job-unit kills in the report.

## Running code differs from the recorded code

- **What goes wrong:** a running watcher executes the code it loaded at spawn, so a fix landed
  after it was armed is silently not enforced. Separately, a deployed tooling sha that does not
  name the code actually running makes every later diagnosis unreliable.
- **Guard:** `doctor` flags watchers armed before the last tooling change as STALE-CODE. `shoal
  upgrade` classifies each change (live, re-arm, brief, restart), re-arms stale watchers lazily at
  their next wake or at once with `--rearm now`, refuses a dirty tooling tree, runs the selftests,
  and records the sha with a ledger `system` event (`kind=upgrade`).
- **Signature:** STALE-CODE in `doctor`; an `upgrade` refusal on a dirty tree.

## Settle predicate unsatisfiable or falsely satisfied

- **What goes wrong:** a waiter's predicate cannot become true (for example it treats a `cancelled`
  run as bad, and superseded runs are cancelled by design), so the wait hangs with no error; or an
  API blip returns zero rows and the predicate reads that as all green. A rollup status can also
  stay pending while individual jobs fail underneath it.
- **Guard:** the protocol requires total predicates: watch the level jobs fail at (matrix legs,
  steps), name `cancelled` explicitly and tolerate it only when the cancelled run is not on the
  current head (checked by sha), treat an empty result as an error, and read the predicate back
  when arming. Waiters are head-pinned and exit loudly when the head moves. The built-in `gh-pr`
  target treats an empty check list as unknown, never resolved, and every wait target resolves
  through a provider that answers unknown when it cannot answer.
- **Signature:** a wait past its norm with its waiter alive and its target terminal; a green
  verdict with no rows behind it.

## Waker that spends turns watching itself

- **What goes wrong:** a session watches its own background process with a monitor that expires,
  and every expiry is a model turn spent confirming the process is fine. A session that ends kills
  its background process with it.
- **Guard:** watchers run detached and keep running across wakes (Claude Code: `--wake direct`,
  started once with `setsid nohup`), or are armed as harness background commands that wake the
  session on exit (the Bash tool with `run_in_background`, no `&`, never the Monitor tool). A
  detached watcher reloads itself in place when shoal's code changes, so it is never re-armed for an
  upgrade.
- **Signature:** watcher re-arm turns per day per role (target near zero for a detached watcher); a
  transcript showing no monitor calls.

## Question raised while the maintainer is offline

- **What goes wrong:** a role raises a blocking prompted question while the maintainer is away. The
  question tool blocks the turn, so the role goes dark for hours with wakes queued and no ledger
  write, and its wait goes stale. A role can miss the offline state entirely if it handles wakes
  without running `next`.
- **Guard:** every health wake text carries the offline banner, which says to raise no prompted
  question now. `shoal asks` refuses to build question sets while offline. The protocol says to
  record the ask and a user-auth wait before calling a blocking question tool. The health quorum
  defers maintainer pages while offline and delivers them as one summary on return.
- **Signature:** a question arriving while offline mode is on; a role silent with a stale wait
  while its session reads busy.

## Ask dropped silently or cleared blind

- **What goes wrong:** a role removes a recorded ask it believes stale after checking the wrong
  evidence (a build id instead of the PR), or clears all asks at once, erasing one whose answer was
  a condition rather than a grant. The maintainer never learns the question existed.
- **Guard:** `queue --drop-ask <n>` requires `--drop-reason`; an unanswered drop is recorded and
  `next` lists it under DROPPED ASKS until it is disclosed in the role's next question set.
  `queue --answered <n>` removes an answered ask. `--asks-clear` also requires a reason.
- **Signature:** an ask count that shrinks without an `--answered` or a disclosed drop.

## Question describing the wrong item

- **What goes wrong:** a role labels a merge question from memory and describes one PR with
  another's summary. The maintainer grants the question as read, and the merge lands with the wrong
  subject and body.
- **Guard:** `shoal asks --question-json` builds the question sets (at most four questions each)
  from live PR data: title, state, head and checks. Every merge ask carries grant, defer, delegate
  and deny options.
- **Signature:** a merge commit whose message names another PR's change.

## Pages nobody reads

- **What goes wrong:** pages delivered only to the ledger sit unread overnight, so an escalation
  ladder ends in silence.
- **Guard:** with `health.page = "gh-issue:<owner/repo>#<n>"`, every page is also a comment on that
  issue, which the maintainer's notifications deliver. Pages are deferred while the maintainer is
  offline and arrive as one summary on return.
- **Signature:** page comments on the inbox issue; with no channel configured, pages exist only as
  ledger messages to `user`.

## Harness server restart kills in-flight turns

- **What goes wrong:** a harness server that hosts several sessions restarts, stopping every
  running turn while leaving transcripts intact. The sessions look alive and are not working.
- **Guard:** the lanes sensor detects the server restart and wakes every role in that harness
  through the health adapters, so each runs `next` and resumes from its ledger state.
- **Signature:** a server-respawn sensor fire followed by a boundary write from each affected role.

## Serial pipeline starved while work is releasable

- **What goes wrong:** a serial CI agent sits idle while gated builds wait for release and leases
  are free, because every role is waiting on something else.
- **Guard:** the pipeline sensor detects depth-zero starvation and wakes the roles waiting on
  remote CI through the health adapters.
- **Signature:** a starvation sensor fire; pool busy share in the report's GPU section.

## Provider outage read as stalls

- **What goes wrong:** a model provider rejects every request from a set of sessions. The failures
  land as error records rather than output, so every session looks stalled and stall checks page
  about lanes when the fault is upstream.
- **Guard:** `doctor`'s `provider errors` check scans the harness message store for failed-send
  records in the last 24 hours, and fails when one landed in the last hour.
- **Signature:** a `provider errors` WARN or FAIL in `doctor` alongside stall wakes for several
  roles at once.

## Routing model unavailable or hanging

- **What goes wrong:** the model that scores the board is withdrawn, rate-limited or refuses the
  caller, or a refused request hangs silently. Scoring stops and the digest goes stale without an
  error.
- **Guard:** each matcher run probes models in tiers (the last model that answered, then free
  models, then the subscription tier, then the paid balance), in its own process group under a
  probe timeout, killed whole on timeout. A switch writes a warning, a ledger `system` event
  (`kind=matcher-model`) and at most one page a day; `doctor` warns while a paid tier is active.
  When nothing answers, cached fits up to `stale_hours` (24) are reused, then items go unscored,
  and a live matcher with nothing scored falls back to keyword ranking.
- **Signature:** `matcher-model` system events; the tier shown in `doctor`.

## Completion claimed without ground truth

- **What goes wrong:** a role records `done` for work that did not land, or a report counts
  completions from role claims alone.
- **Guard:** completion is verified against GitHub, never taken from `done` events. Reconcile runs
  twice a day, date-filtered at the source so older issues closing are seen, and matches each
  merge or closure by repository and number (a bare number would conflate repositories). The report
  counts unverified `done` events.
- **Signature:** unverified dones in the report's outcomes section.

## Telemetry that reads complete while miscounting

- **What goes wrong:** a report looks whole while counting some things twice and others not at
  all: a posture change and its ledger twin both counted, one role split across several session
  keys, a role with no transition in the window shown as unobserved.
- **Guard:** the report keys time in state by role slug, resolving slug, then session, then session
  prefix, and counts anything else as `unresolved` rather than giving it a row. It drops a ledger
  `state` twin landing within 5 seconds of its local transition, and seeds each role's state at the
  window start from its last earlier transition. An event that cannot be written reports the
  failure on stderr rather than vanishing, so a gap in telemetry is never silent.
- **Signature:** per-role time summing to 100% with zero `unresolved`; `emit failed` lines in a
  timer's journal.

## State vocabulary drift

- **What goes wrong:** roles invent substates, or use near-synonyms, so time in state is scattered
  across values no rule recognizes.
- **Guard:** `shoal transition` canonicalizes known aliases (for example `user-feedback` to
  `user-design`, counted as aliased) and refuses unknown substates with nothing written.
- **Signature:** aliased and refused counts in the report's transition-quality section.

## Row label identity drift

- **What goes wrong:** a role labels its status row with a generation counter instead of its
  session tag, so rows from different generations cannot be told apart or joined.
- **Guard:** `next` prints the role's label, `<slug> (<slug>-r<N>)`, for the role to copy, and
  `show`, posted blocks and reports all use the same label. A group incarnation number is never a
  row label.
- **Signature:** a posted row whose label differs from the one `next` printed.
