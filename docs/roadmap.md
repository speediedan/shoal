# Roadmap

Directions SHOAL may grow in, written down so that architectural changes made now do not close them
off. Nothing here is committed work. Each item says what holds today, what is missing, and what would
trigger it. Current limits and their triggers are in [`limitations.md`](limitations.md).

## 1. Participants that reach the shoal only through the ledger

**Holds today.** Correctness needs nothing but outbound access to the ledger remote:
- a host behind NAT, with no VPN or tailnet, can run roles;
- its watchers pull the ledger, and its own health quorum watches and wakes its roles;
- it records its liveness as `host-beat` events;
- lifecycle verbs for its roles run on that host.

No host ever connects to another host for coordination (a viewer's ssh route is for a terminal, not for coordination). Server names are labels: they are not
authenticated, and a clash between two hosts is detected from the beats.

**Missing for a holistic view from any host:**
- **Live session state of remote roles:** done for hosts with an ssh route (the read model pulls that host's
  own rows). Without a route, other hosts see ledger state (status, queue, last write) and the host's beat;
  carrying a compact per-role liveness summary in each host's beat would cover that case from the ledger alone.
- **Viewing or attaching to a remote session:** done for hosts with an ssh route (cards run the role's own
  host's `shoal view` over ssh). Without a route, a viewer shows the ledger's view of that role.
- **Reports across hosts.** Telemetry stays in each host's data root. Direction: publish per-host
  summaries, not raw events, where the report can read them.

**Git load, and how it stays small:**
- **Writes.** Events are written only on change, and beats are a few per host per hour. Writes scale with
  work, not with hosts.
- **Reads.** Each watcher fetches on its own interval, so reads scale with roles times hosts. Direction:
  one fetcher per host feeding every local watcher, so reads scale with hosts only.
- **Latency.** Latency is set by the watcher interval (about two minutes), not by git (about two seconds
  per hop, measured).
- **A content-free "fetch now" poke** (a notification topic or a repository push webhook) would cut wake
  latency without moving any state off git. A forged poke costs one fetch, so it needs no trust.

**Why not a DHT or shared storage now.** A distributed hash table, or any shared store, would add:
- an availability dependency;
- NAT traversal;
- an identity and authorization model of its own (git currently provides both through push access).

It would buy latency the watcher interval does not need. Revisit it only together with section 2, where
state outside git may be needed anyway.

**Trigger:** a host that cannot join a VPN or tailnet, or a viewer that wants every role's live state
without connecting to every host.

## 2. Several users collaborating in one shoal (aspirational)

If the multi-host pattern proves itself, a natural widening is collaboration across users. Another
person could join a shoal and contribute sessions to a shared body of work, possibly without the owner
granting them push access to the owner's ledger. That is a substantial protocol and system refactor.
The broad strokes, so that later changes keep them possible:

- **Git stays the point of authentication and authorization.**
  - SHOAL does not authenticate roles or machines.
  - Who may write what remains a question git answers: push access, per-path rules, branch protection,
    and pull request merges as the acceptance step.
  - Signed commits (SSH or GPG) can bind a writer name to a key without a SHOAL-run identity service.
- **Federated ledgers instead of one shared repository.**
  - Each participant owns a ledger repository holding their own writer files.
  - A shoal becomes a set of subscribed ledgers, and readers fold the union.
  - Read-time permissions, which already ignore an event its writer was not allowed to write, extend to
    "which ledgers and writers count for this item".
- **Cross-user actions as proposals.**
  - A participant without push access proposes events (a claim, an offer, a completion) as a pull request
    or an issue against the owner's ledger.
  - The owner's merge, by hand or by a policy the owner runs, is the authorization.
  - With permission, a collaborator gets push access scoped to their own writer path.
  - Without it, a participant keeps a public ledger of their own. The owner's view may subscribe to it,
    and claims made there stay advisory until accepted.
- **Coordination state that does not depend on trusted agents.**
  - Some orchestration may need to live outside git for latency or scale: an append-only, content-addressed
    log replicated by gossip or a relay, or a CRDT.
  - Every event in it is verifiable on its own, by signature and by the same read-time rules.
  - Git periodically anchors it as the authoritative history.
  - No participant's agent is trusted to enforce the protocol: each reader verifies.
- **Views per user.** Each participant chooses which ledgers, roles and items they follow, and sees the
  in-flight work only of those subscriptions. Private ledgers stay private by repository access.
- **Questions to settle before any of this:**
  - what "first landed wins" means across federated logs;
  - spam and sybil resistance for open participation;
  - how human merge latency interacts with claims;
  - rate limits on the hosting service;
  - maintainer channels per user;
  - the norms for credit, licensing and review of contributed work.

**Trigger:** multi-host operation for one maintainer proving reliable over a sustained period, and a
concrete second participant.
