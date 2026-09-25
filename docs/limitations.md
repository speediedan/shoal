# Known limitations and refactor triggers

SHOAL's current design is tuned for one maintainer, one host and a few dozen role sessions. Git is
the ledger's storage, transport, access control and audit trail at once, which is why there is no
server to run. This page records where that design stops fitting, what we have measured, and the
condition that would reopen each choice. A limitation listed here is known and accepted for now; a
trigger is what turns it into work.

Measurements are from one live group on one host (10 roles, 1,465 ledger events after about four
days), taken 2026-09-25. Re-measure before acting on any number here.

## Summary

| # | Limitation | Accepted today because | Refactor trigger |
| --- | --- | --- | --- |
| 1 | The ledger grows without bound: no compaction, snapshot or archival | projection costs ~9 µs per event; the network dominates | a group passes ~50k events, projection passes 0.5 s, or a group retires |
| 2 | Joining from a second host or as a second user is untested | every role runs on one host today | before anyone joins from another host or account |
| 3 | One repository holds every group's ledger | one owner, one host, few groups | a second user, or a group that needs its own access control |
| 4 | Git as the ledger's store and transport caps write rate, wake latency and group size | measured load is ~2 orders of magnitude below the write ceiling | sustained writes, retry exhaustion, wake latency or role count past the thresholds in §4 |
| 5 | Items carried over from earlier design notes | each is tracked; none blocks a single-host group | per item |

## 1. Ledger growth: no compaction or archival

**What happens.** Each role appends to its own JSONL file, and every event is its own commit. Nothing
ever removes or summarizes an event: every projection (`next`, `show`, each watcher tick) replays the
whole log from the start, and a retired group's directory stays in the repository.

**Measured.**

| quantity | value |
| --- | --- |
| events in the live group | 1,465 across 10 writer files, 724 KB of JSONL |
| event rate | about 360 events a day (about 300 commits a day across the repository) |
| raw parse of all events | 12.7 ms, about 9 µs an event |
| projection (`ledger-lint --no-sync`) | 0.10 to 0.12 s, the same as CLI startup alone (0.11 s), so the replay itself is not yet visible |
| clone `.git` | 27 MB, almost all loose objects; the packed history is 1.7 MB |

Two growth curves matter, and the second is the sooner problem:

- **Replay cost** is linear in total events. At today's cost a full replay reaches about 0.5 s near 50k
  events and 1 s near 100k, which is months of one busy group, not days.
- **Repository size** grows faster than the log. Each event commit writes a new blob of its writer's
  whole file, so loose objects scale with events times file size until git packs them. Automatic `gc`
  keeps this bounded in practice; a clone that never packs does not.

**Trigger.** Any of: a group passes about 50k events; projection passes 0.5 s; a group retires (its
log is then dead weight for every clone).

**Directions, cheapest first.**

1. Run `git maintenance start` (or a periodic `git gc`) on every ledger clone. This needs no format
   change.
2. Archive a retired group: move its directory to an archive branch or tag, then delete it from the
   default branch. Existing clones keep reading it; new clones skip it.
3. Snapshot and truncate: a checkpoint event records the derived state (open claims, each role's
   queue version and posture, the ids a watcher has delivered). Projections start from the latest
   checkpoint, and the lines before it move to an archive file. This is the only direction that
   bounds replay cost for a group that never retires. It needs a rule for who may write a
   checkpoint and how a reader verifies one.

## 2. Joining from another host or as another user is untested

**What happens.** The ledger's data model already crosses hosts: each event records its writer, and
the spec records a `server` for each role. Nothing else has been tried. Host-local pieces that a
second host would have to reproduce or coordinate:

- `config/host.toml`: the data root, the ledger clone path, the lease provider and the harness CLIs.
- The health quorum, the digest, reconcile and relay timers. These are installed on one host. A second
  host running its own copy would duplicate pages and board refreshes; a second host running none
  depends on the first host for health.
- Heartbeats, wait proofs, watcher sidecars and job records. These are files on the local host, so
  the other host sees a remote role's liveness only through its ledger writes (a ledger write counts
  as a heartbeat).
- Resource leases and job units. These are local to one host by construction.

**Trust model.** The ledger enforces permissions when it is read: a projection ignores an event its
writer was not allowed to write, and `ledger-lint` lists the violations. Nothing authenticates a
writer beyond git push access. Anyone who can push can write any file under any writer name, and the
read-time rules then judge it by the name it claims. That is sound only while everyone with push
access is trusted.

**Trigger.** Before anyone joins from a second host or a second account.

**Direction, if it proves useful.** Today a shoal is one maintainer's swarm working through one
ordered, multi-repository body of work. A later incarnation may be refactored so that several users
contribute sessions to one shoal targeting a shared multi-repository work set. That would need, at
least: writer identity bound to something stronger than a name (the trust model above), per-user
authorization for merges and other grants, a maintainer channel per user rather than one inbox, and
one agreed host (or a leader election) for the timers.

**What the first test should prove.** A second clone on another machine and account runs one role.

- A claim race between the two hosts resolves to exactly one holder.
- A wake crosses hosts within one watcher interval plus one push.
- The first host's health quorum does not page the remote role as dead.
- Exactly one host runs the digest and reconcile timers, and the other host's `doctor` reports that
  state rather than failing on it.

## 3. One repository holds every group

**What happens.** Every group's ledger is a directory in one repository. Keeping one repository is
currently deliberate: one owner, one host, one set of credentials. It costs a single-group
participant three things:

- **Bytes.** A clone carries every group's history, and every fetch carries every group's new events.
- **Contention.** Every write from any group races for the same branch ref, so one busy group slows
  every other group's pushes (see §4).
- **Access control.** Hosted git grants access per repository. A collaborator who needs one group's
  ledger gets every group's.

**Trigger.** A second user, or any group whose ledger should not be readable by everyone with access
to the others.

**Directions.**

| option | isolates bytes | isolates contention | isolates access | cost |
| --- | --- | --- | --- | --- |
| partial clone plus sparse checkout (`--filter=blob:none --sparse`, `sparse-checkout set <dir>`) | mostly (commits still fetched) | no | no | no code change; a joiner's recipe |
| a branch per group (`--single-branch`) | yes | yes | no | spec names the branch; tooling reads it |
| a repository per group (the spec's ledger remote, set per group) | yes | yes | yes | one repository to create per group |
| archive retired groups (§1) | partly | partly | no | helps every layout |

A repository per group is the likely default once a second user joins, because access control is the
one cost the other options cannot remove. Until then, archiving retired groups keeps the shared
repository small.

## 4. Git as the store and transport limits scale

**The write path.** An append is a local commit followed by a push. On one host, a lock on the shared
clone serializes every role's writes. Across hosts, the remote's branch ref does: a push that loses
the race is rejected, the writer fetches and rebases, which cannot conflict because every file has one
writer, and retries up to three times. After that the event stays committed but unpushed until the
next sync. Contention therefore costs latency, not data. Claims lean on this: the push is the single
point that orders them, which is what makes "first landed wins" well defined.

**The read path.** Each role's watcher runs `git fetch` and a rebase every 120 s. A fetch took 0.91 s
here, and `shoal show` including its pull took 1.14 s, so the network is almost all of it. Wake
latency averages about half the interval, plus the writer's push.

**Where the ceilings are.**

| dimension | how it scales | today | where it bites |
| --- | --- | --- | --- |
| write rate per repository | one ref update per push round trip (~1 s); rejected pushes retry | about 0.25 events a minute on average, bursts at restarts | sustained ~10 events a minute, or any write burst from many roles at once |
| retries | a rejected push repeats a fetch, a rebase and a push; after three it gives up to the next sync | not observed | writes left unpushed across a watcher interval, more than occasionally |
| fetch load | roles × hosts ÷ interval | 10 roles: 5 fetches a minute | hundreds of roles polling one hosted repository |
| wake latency | interval ÷ 2 + push | about a minute | work that needs sub-minute hand-offs |
| replay | linear in events (§1) | negligible | tens of thousands of events |

**Triggers.** Any of: sustained writes above ~10 a minute for a repository; unpushed events surviving
a watcher interval more than a few times a day; a p95 wake latency above five minutes; more than about
50 concurrently active roles on one repository.

**Directions.** Any replacement must keep three properties: single-writer logs, a union merge, and a
single place that orders claims.

1. **A per-host daemon over git.** One process per host holds the projection in memory, serves `next`
   and `show` locally, and batches writes into one push every few seconds. Git stays the durable log,
   the access control and the audit trail. This cuts commits, pushes and replays, changes little, and
   is the natural first step.
2. **Push notification instead of polling.** A hosted-git webhook, or a small publish/subscribe
   channel, tells watchers a write landed. Wake latency drops from about a minute to the push time, and
   fetch load stops scaling with role count.
3. **A log service.** An append-only log served by a small daemon keeps its state in memory, snapshots
   periodically to durable storage, and gives each consumer an offset (the Kafka pattern). Git becomes
   a periodic archival mirror. This scales furthest in write rate, at the price of running a server
   and its authentication.
4. **Peer-to-peer replication.** A daemon on each host gossips the per-writer logs. Because the ledger
   is already a set of grow-only single-writer logs, merging them needs no coordination (it is a CRDT).
   A DHT or a gossip overlay removes the central remote. The hard part is claims: with no single
   remote there is no push to order them, so claim arbitration needs its own rule, such as a
   deterministic total order over (logical clock, writer) or a designated sequencer, and writers need
   signed events in place of push access.
5. **A database behind an API.** SQLite with replication, or Postgres, gives strong ordering and cheap
   queries, and needs a server.

Directions 1 and 2 keep the no-server property and are enough for roughly the next order of magnitude.
Directions 3 to 5 are for a group of many users or many hosts, and each gives something up: the
server-free deployment, or the free authentication and audit trail git provides.

## 5. Items carried over from earlier design notes

These predate the SHOAL extraction. Each is tracked, and none blocks a single-host group.

- **Waiters bound to retired sessions, and the relay-watch cursor race** (shoal#2). A restart should
  kill the previous generation's waiters itself; until then the operator kills them by pid.
- **Pushing into harness sessions.** A socket push into a running Claude Code session is unproven, so
  a watcher's exit is the wake. Prove a push channel before relying on one.
- **Within-session watches as the only mechanism.** The timers stay as a fallback until an incarnation
  shows no stale waits and no stall-suspect pages for every role.
- **Direction independence.** No pilot has yet run a Claude Code coordinator with opencode workers,
  which would show the protocol does not depend on which harness leads.
- **Archiving opencode sessions.** Finished Claude Code sessions can be archived with a host script
  that wraps `claude rm` and keeps their history. opencode has no equivalent command: the only route
  is the OpenChamber server's HTTP endpoint, so retiring an opencode session is manual.
- **Backlog sources beyond GitHub Projects.** The backlog is a GitHub Projects v2 board today. The
  intended design accepts any ordered set of tasks that meets a defined source protocol (stable item
  ids, an order, open or closed state, and text to match against); the protocol is not written yet.
- **Matcher tuning.** The semantic matcher's shadow-mode comparison has not yet been used to tune its
  scores.
- **Host-local state on reboot.** Heartbeats, wait proofs and sidecars under `/tmp` do not survive a
  reboot. The health quorum then sees stale state until each role re-arms.

## Keeping this page honest

Update a row when its measurement changes by more than a factor of two, when its trigger fires, or
when the work lands. Record a fired trigger here with its date and measurement before starting the
refactor, so the reason stays next to the limitation.
