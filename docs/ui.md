# Operator UI

Shoal stays a coordination tool. It does not ship a terminal, a multiplexer or a session manager.
The UI is two small pieces: a **read model** that every UI binds to, and an **adapter** that maps
that read model onto a terminal workspace someone else maintains. The first adapter targets
[Herdr](https://github.com/herdrdev/herdr). Section 12 of [`architecture.md`](architecture.md)
places these pieces among the components.

Three rules hold for every piece:

1. **Viewing is never a wake.** Nothing that only views calls `next` without `--peek`, pulls the
   ledger, commits, or messages a role. A card refresh, a feed tick and an attach cost no tokens. A
   write verb's preview runs that verb's own `--dry-run`, which syncs like the verb itself.
2. **The UI owns no session.**
   - Panes run the harness's own client on a session that already exists (`claude attach <id>`, or
     the opencode TUI as a client of OpenChamber's running server).
   - The UI never resumes a running Claude session (that forks it), never starts an opencode
     server, and never restarts a role. Muse observation owns no session; native interaction is
     a separate explicit action while idle.
3. **Every write is a shoal verb.** Actions run `shoal msg`, `offer`, `withdraw`, `offline` or
   `freeze` after a `--dry-run` preview and an explicit confirm, and are logged to
   `<data_root>/ui-actions.jsonl`. The adapter never types into a pane.

Muse and Codex role cards open a live read-only observer on Enter, even when the role is idle. It
leaves automated wakes enabled and shows public output and tool activity. Press `q`, Escape or Ctrl+C
to return to the card. Native interaction is a separate `i` action inside that view, available
only while idle; `/exit` quits the native client and releases its exclusive ownership. A running turn
is never stopped to attach. The view's status says `running` only for a turn; a native client open
elsewhere reads `held` with its pid, terminal and start, since it also holds the role's wakes. When `i`
is refused by one, `T` ends it and attaches here, unless the session was written to in the last two
minutes. Standalone Muse and Codex cards retain their native interaction when idle.

## The read model: `shoal ui status`

```bash
shoal ui status [--shoal <id> ...] [--conditions] [--refresh-conditions] [--pull] [--json]
shoal ui line
```

`--json` prints the observation contract. It is versioned (`v`), changes are additive only, and a
reader ignores fields it does not know.

```json
{"v": 1, "host": "<host>", "ts": "<utc>",
 "shoals": [{
   "id": "<id>", "spec": "<path>", "incarnation": 2, "gen": "r3", "retired": false, "dir": "<path>",
   "mode": {"offline": false, "offline_until": null, "freeze": false},
   "pages_24h": 0, "last_page": null,
   "asks_open": 0, "needs_you": 1, "stalled": 1, "violations": 0, "contested": 0,
   "tooling": {"sha": "<12>", "head": "<12>", "current": true}, "features": ["..."],
   "conditions": {"source": "health-dry-run", "ts": "<utc>", "age_s": 40,
                  "open": [{"slug": "<role>", "condition": "parked-work", "since": "<utc>", "detail": "..."}]},
   "roles": [{
     "slug": "<role>", "tag": "<role>-r3", "harness": "claude", "model": "<model>", "fill": "self",
     "server": "", "aux": false, "session_id": "<id>",
     "status": "active|wait|idle|offline|parked", "sub": "local-ci", "status_age_s": 600,
     "wait": {"sub": "...", "target": "...", "holder": "...", "rescan_utc": "..."},
     "active": "<repo>#<n>:fix",
     "next": [{"item": "<repo>#<m>:review", "blocker": "<role>:<repo>#<k>", "stale": true}],
     "last_done": {"item": "<repo>#<j>:fix", "ts": "<utc>"},
     "blocker_stale": true, "offers_open": 0, "mail_since_boundary": 1, "asks": ["..."],
     "watcher": "alive|dead|none", "session": "working|blocked|idle|dead|unknown|parked",
     "answered": null, "last_write_age_s": 900, "heartbeat_age_s": 60,
     "can_progress": {"ok": false, "reasons": ["blocked-on-question"]}, "needs_you": true,
     "agent": {"group": "needs-you|working|waiting|blocked|held", "state": "asked", "detail": "..."},
     "view": ["shoal", "view", "<id>", "<role>"]}]}]}
```

**The agents list's attention axis** (`ui_status.agent_state`). Every role is in exactly one `agent.group`, and
`agent.state` says why in more detail than the status word (a role's own `idle` covered every item blocked,
nothing assigned, held and handoff pending alike). The first match decides, in this order:

| Group | Whose move | States |
|---|---|---|
| `needs-you` | the maintainer's: it moves on only through you | `asked` (an open ask, a parked role's included, or a user wait), `question` (an open prompt), `dead-worker`, `blocker-role-cannot-take`, `wake-held` (its wait ended, an attached client holds the wake), `wakes-held` (its watcher holds new events for a client attached to its session, as the watcher's sidecar records; the quorum pages it once: exit the client. While you are offline, a card's client is ended after an hour), `handoff-pending` (restart-ready, and the shoal's context restart is `page`, so the operator restarts it; in `auto` mode a restart-ready role needs nobody), `restart-held` (its automatic restart is held for an attached client: detach it; while you are offline, a card's client is ended after an hour), `says-waiting` (Claude reads the session as `blocked`: its last turn ended sounding like it waits on you, with no prompt open and no ask recorded; only when the ledger has no reason of its own, a recorded wait or a blocker on every next item, in which case the role reads by the ledger with those words in its detail), `no-work` (fill=assigned, empty queue, no offer) |
| `working` | the role's, now | `working`, `restarting` (a restart is replacing its session) |
| `waiting` | nobody's: it resumes by itself | `answered` (you replied to every open ask since its last write; see Answered below), the wait's substate (`local-ci`, `remote-ci`, `research-proto`, `research-window`, ...), `<sub>: resolved` while its wake is due, `work-due` (a blocker resolved, its wake is due) |
| `blocked` | another role's or the outside world's | `session-blocked`, `upstream`, `blocked: role`, `blocked: upstream`, `blocked: lease` (a blocker only past its daily look says so in `detail`, still blocked) |
| `held` | the maintainer's, by choice | `parked`, `held` (a `hold:` blocker), `observer` (fill=none) |

**Answered.** A message you write to a role (`by: user`) after its last write answers its open asks when it names
them (`re ask <n> [<id>] "<opening words>": ...`, the Asks view's form: those asks, matched by id or words, so an
answer to an ask the role has since replaced answers nothing) or wakes it (a plain note without a wake answers nothing).
When it answers every ask, the role reads `waiting`, state `answered` (`ui_status.reply_answer`; the row carries
`answered`: `{ts, asks, partial, late, age_s}`): your move is made, and its watcher wakes it on your reply. It stays
`asked` when you answered only some (its detail says which), when the reply is older than `ANSWER_PICKUP_S` (600 s: the
watcher pulls every 120 s, so something holds it, and the detail says when you answered), or when the reply cannot reach
it (`ANSWER_HOLDS`: parked, a dead worker, a held wake, a question open in its session). A turn running reads
`working` as before, and the role's next write (its `queue --answered`, or a new ask) ends `answered`: a new or
unanswered ask reads `asked` again.

`needs_you` is `group == "needs-you"` and not dismissed. A dismissal is keyed on the needs fingerprint, which
includes `agent.state`, so a dismissed row keeps its group and state (shown apart, under Dismissed) and comes back
when either changes.

A dead watcher and a stopped active turn are the quorum's to fix (it re-arms the watcher and wakes the role):
they count as stalled, and the role keeps the group of what it is doing.

A role card writes `agent.group` and `agent.state` on its terminal as the labels `shoal.group` and `shoal.state`
(`ui_app.role_agent`; group `dismissed` while the row is dismissed), and the `shoal-agents` clone orders its Priority
view on one scale: Needs you, Working, Done, Waiting, Blocked, Held, Idle, Dismissed (Done and Idle only for rows with
no group, which keep Herdr's own status). A finished turn not yet looked at keeps its `done` badge inside its group.
Standalone seats (`ui_app.standalone_agent`) take the same groups from their session and registry entry:

| Group | State | When |
|---|---|---|
| `needs-you` | `question` | an open question prompt |
| `working` | `working` | a turn running |
| `working` | `background` | between turns with its own background tasks running (Claude's `inFlight`: subagents, shells, monitors): they re-invoke it, so it is still at work (2026-11-14) |
| `blocked` | `session-blocked` | it declared a wait on another session or role (`shoal ui wait <seat> --on <who>`) |
| `waiting` | its substate | it declared a wait on a target only (`shoal ui wait <seat> --target <kind:ref>`) |
| `needs-you` | `your-turn` | its reply ended (the listing reads blocked) with no prompt open |
| `dismissed` | `your-turn` | that reply was dismissed (`shoal dismiss --session <seat>`); it lapses at the next reply |
| `held` | `idle` | between turns, nothing declared |
| `held` | `stopped` | no worker |
| `dismissed` | its state | the seat is closed in the viewer (the registry's `dismissed`) |

A finished reply needs you until you dismiss it (maintainer, 2026-11-11, replacing the 2026-11-08 reading of every
reply as held). A declared wait covers the turn that declared it: a later turn's end lapses it unless the seat declares it
again, as a role writes a fresh sleep status each turn. Muse and Codex seats report from their harness's status line
(`ui_app.native_report`): working while a turn runs, else what they declared, else held. A seat with an attached client
reads as its session does: its card keeps the labels current through an attach whatever the harness (2026-11-14:
an attached Muse seat had kept a cleared `session-blocked` for an hour), and a Muse or Codex seat's declared wait
lapses at a turn end its card saw, since those harnesses report none. A Muse turn waiting on its subagents counts
their records as activity.

**Sources.** Each field is derived once, from the code that already owns it:

- Status, items, blockers, offers and mail come from `ledger.View`.
- Stale blockers come from `blockers.stale` (no provider calls).
- Claude liveness comes from `claude agents --json --all`: a row with no `pid` is a dead worker.
- opencode liveness comes from `openchamber session status`.
- The watcher state comes from the `ledger-watch` sidecar.
- `conditions` come from the health quorum's own `evaluate()`. They are cached for five minutes per
  shoal in `/tmp/<id>/ui-conditions.json`, because `evaluate()` probes providers. The section is
  optional: it is filled from the `health_condition` telemetry event once that event exists, without
  a version bump.

**`can_progress` reasons** are the stalls measured on a live group:

- `dead-worker`: the session is listed with no worker process.
- `restarting`: the session has no worker while a restart or handoff holds the role's restart lock (from the old
  session's stop until the new one is bound). Not a need: the row reads `working`, state `restarting`, with no
  badge (measured 2026-11-27: each handoff put its role in Needs you as `dead-worker` for about a minute).
- `context-large`: the session's context is past its `context_restart_k` limit (informational; the row's
  `context_k` holds the size in thousands of tokens).
- `dormant`: a Claude session with no worker (the background service retired it idle) behind a live
  detached watcher. The watcher resumes it directly when work arrives, so it is not blocking and shows
  as `DORMANT`.
- `blocked-on-question`: the session waits on a pending question to the maintainer.
- `watcher-dead`: the session is between turns with no `ledger-watch`, and the role is active or has work its watcher
  would deliver.
- `watcher-dormant`: the same dead watcher on a role with nothing it would be woken for (`lazy_rearm`): its next real
  work raises its own wake, which re-arms it. Informational, so a role waiting on its own job reads working on it.
- `blocker-role-cannot-take`: a next item is blocked on a role that cannot take it.
- `parked-work`: the role sleeps while a next item has no current blocker.
- `waiting-on-you`: the role has recorded asks, or a user wait.

`parked`, `dormant`, `watcher-dormant` and `context-large` are informational. `ok` is false for any other reason except `waiting-on-you`.

`watcher-dead` and `parked-work` come from the health quorum's own predicates
(`health.watcher_dead` with `health.watcher_dormant`, `health.parked_free`). Blocker checks there run with `live=False` (no
provider calls), so a provider-checkable blocker (`upstream:`, `lease:`) that resolved minutes ago
still reads as current until the quorum's next run marks it stale. This is deliberate: never turn
provider calls on in the 20-second feed. `--conditions` (the quorum's own `evaluate()`, live, cached
five minutes) is the up-to-date view.

## The graph feed: `shoal ui graph`

```bash
shoal ui graph [--shoal <id> ...] [--json] [--since <cursor>] [--events N] [--window-rate S] [--replay]
shoal ui graph-feed [--interval S] [--out PATH] [--once]
```

`ui graph` prints every shoal on the host as a network: roles, the maintainer and outside seats as
nodes, ledger traffic as edges, and the tail of recent events. Without `--json` it prints a short
summary per shoal (roles by state class, the needs-you list, the busiest edges). `ui graph-feed`
writes the same document every `--interval` seconds (default 5) to `graph.json` beside the status
cache (`$XDG_RUNTIME_DIR/shoal-ui-<uid>/`), atomically (temp file plus rename). It runs as it is
under a user unit.

The contract is versioned like the status document: `v: 1`, additive only, unknown fields ignored.

```json
{"v": 1, "kind": "shoal-graph", "host": "<host>", "ts": "<utc>", "cursor": "<opaque>",
 "fresh": {"as_of": "<utc>", "age_s": 30, "stale_after_s": 90, "stale": false, "reasons": []},
 "windows": {"rate_s": 300, "recent_s": 3600, "day_s": 86400},
 "hosts": [{"id": "host:<host>", "name": "<host>", "home": true,
            "roles": 6, "working": 1, "waiting": 4, "needs_you": 2, "msgs_1h": 14}],
 "shoals": [{"id": "<id>", "gen": "r12", "retired": false, "home": "<host>", "ledger_age_s": 120,
             "mode": {"offline": false, "freeze": false},
             "metrics": {"roles": 6, "working": 1, "waiting": 4, "needs_you": 2, "stalled": 1,
                         "asks_open": 1, "pages_24h": 2, "done_24h": 6, "done_7d": 31, "claims_24h": 8,
                         "offers_24h": 5, "msgs_1h": 14, "msgs_24h": 120, "work_ratio_24h": null,
                         "grants_pending_n": 1},
             "boards": {"items": [{"id": "<owner>/<n>", "title": "<board>", "url": "https://github.com/users/<owner>/projects/<n>",
                                   "counts": {"done": 134, "open": 58, "in_flight": 4, "assigned": 10}}],
                        "off_board": {"in_flight": 1, "assigned": 2}, "age_s": 240, "stale": false}}],
 "nodes": [
   {"id": "user", "kind": "user", "shoal": null, "label": "you",
    "state": {"class": "work", "event": "", "group": "", "needs_you": true},
    "work": {"asks_open": 3, "grants_pending": 1}, "metrics": {"msgs_out_1h": 2, "msgs_in_1h": 5}},
   {"id": "<id>:<role>", "kind": "role", "shoal": "<id>", "slug": "<role>", "label": "<role>",
    "ecosystem": "claude", "model": "<model>", "host": "<host>", "fill": "assigned",
    "state": {"class": "wait", "event": "local-ci", "group": "system", "source": "role-time", "quality": [],
              "status": "wait", "sub": "local-ci", "session": "working", "since_s": 900, "stale": "unread",
              "needs_you": false, "reasons": [], "shoal_offline": false, "restart_ready": false,
              "job_host": "<pool host>",
              "job": {"unit": "<unit>", "host": "<pool host>", "placed": true, "granted": true,
                      "started_age_s": 840}},
    "work": {"active": {"item": "<repo>#103:fix", "url": "https://github.com/<owner>/<repo>/issues/103"},
             "next": [{"item": "<repo>#104:review", "url": "...", "title": "<board title>", "blocker": "<role>:<repo>#103", "stale": false}],
             "next_total": 1,
             "asks": [], "offers_open": 0, "grants_pending": [{"item": "<text>", "kind": "<kind>"}],
             "done_recent": [{"item": "<repo>#90", "url": "<the done event's url>", "ts": "<utc>"}]},
    "metrics": {"done_24h": 1, "claims_24h": 1, "msgs_out_1h": 2, "msgs_in_1h": 3,
                "last_write_age_s": 600, "heartbeat_age_s": 30, "context_k": 95},
    "view": ["shoal", "view", "<id>", "<role>"]}],
 "edges": [{"id": "<src>|<dst>|msg", "src": "<id>:<a>", "dst": "<id>:<b>", "class": "msg",
            "n_rate": 2, "n_1h": 4, "n_24h": 18, "rate_per_h": 3.5, "last_ts": "<utc>", "last_age_s": 40,
            "shoal": "<id>", "cross_host": false,
            "recent": [{"ts": "<utc>", "age_s": 40, "kind": "info", "summary": "<= 120 chars",
                        "item": "<optional>", "url": "<optional>"}]}],
 "events": [{"seq": 101, "ts": "<utc>", "shoal": "<id>", "src": "<id>:<a>", "dst": "<id>:*",
             "type": "msg", "class": "broadcast", "kind": "info", "item": "", "summary": "<= 120 chars"}]}
```

**Nodes.**

- `role`: one per spec role. Its row (status, sub, session, `since_s` from `status_age_s`,
  `needs_you`, reasons, active and next items, asks, offers) is the `ui status` row. Every role node
  carries `host`: the role's server, else the spec's home, else this host.
- `user` (the maintainer, one for all shoals): open asks summed, and `grants_pending` summed from the
  status rows' `grants_pending` lists (0 while no row carries one; grant events are not folded).
- `system` (`<id>:shoal`, the `shoal` writer), with the shoal's home as `host`. The retired wake
  relay's writer folds into it, so its historical traffic stays counted without a node of its own.
- `external` (`ext:<slug>`): an outside seat, from an `ext-<slug>.jsonl` writer file or an `ext:<slug>`
  target with an event in the last day, or a saved standalone seat. `alias_of` names `<other-shoal>:<slug>` when that slug is a role in another discovered shoal;
  the seat then takes that role's state and host.
- `writer`: any other edge or event endpoint (a removed role, a tool's own writer file), state unknown.

`<id>:*` (a broadcast target) is not a node.

**Role state.** `state.class`, `state.event` and `state.group` are `role_time.classify` over a
snapshot built by `role_time_adapters.build_snapshots`: the ledger part from the View, the native part
only from the role-time cache files (`role-time-native.json` in the shoal's state directory, plus the
`role-time-native-<host>.json` copies `role-time observe` keeps there). `event` is the raw role-time
event (`turn`, `local-ci`, `remote-ci`, `research-proto`, `lease`, `user-auth`, `user-ask`, `user-prompt`,
`user-design`, `assignment`, `sources`, `upstream`, `session-blocked`, `self`, `hold`, `handover`,
`refill` and the rest of the vocabulary), passed through as it is, so a renderer can key encodings
off it; `group` is `WAIT_GROUP` for waits (`research` for `research-proto`, apart from the CI waits' `system`), else empty; `quality` is the classifier's quality flags.
`state.source` is `role-time` for that reading. A role with no native observation (no cache, or a
background role) gets a best-effort reading of its status row in the same vocabulary, marked
`source: status`. As in role-time accounting, a source is believed only within its budget (the
ledger half only while the clone's last fetch is fresh). Where accounting would read
`unknown:stale-source` or `unknown:missing-source`, the graph falls back to the status reading
instead, marks it `source: status`, and adds `role-time:<event>` to `quality`, so a host whose clone
stopped fetching still shows what its roles last declared.

For `wait:local-ci`, `wait:remote-ci`, `wait:research-proto` and `wait:lease`, `state.job` describes the job a `job:<name>`
wait target names, from the job meta the role-time adapter already reads (`<state dir>/jobs/<unit>.json`):
`unit`, `host` (where the job runs: a placement's host, else a remote `--on` host, else this host;
it may differ from the role's host), `placed`, `granted` and `started_age_s`. `state.job_host` mirrors
`job.host`. Both are omitted when the wait names no job this host knows. A job's host joins `hosts`.

**Work.** Item URLs are `https://github.com/<owner>/<repo>/issues/<n>` (GitHub redirects a pull
request's number), with the owner from the item, else the spec's `repos`, else the host's
`default_owner`; `null` when none resolves. `done_recent` lists the role's `done` events of the last
24 hours (at most 10), with the event's own `url` when it has one. `grants_pending` is the status
row's list, passed through when the row has one, else omitted (likewise the shoal's
`grants_pending_n`).

**Edges** aggregate ledger events of the last 24 hours per (`src`, `dst`, `class`):

| class | from |
|---|---|
| `msg` | a `msg` between writers |
| `broadcast` | a `msg` to `*`; `dst` is `<id>:*` |
| `ask` | a `msg` of kind `request` or `ask` to `user`; also present (with `open`, the role's open asks) when the role has recorded asks |
| `health` | a `msg` of kind `health` (a page) |
| `offer` | an `offer`, `by` to `to` |
| `grant` | a `grant`, `user` to its `role` |
| `handoff` | a `release` of kind `handoff` to a role |
| `blocked-on` | a role's next item whose blocker names another role of the shoal (`<role>:<item>` or `role:<role>`); no event needed, `structural: true`, `stale` when every such blocker is stale |
| `alias` | an outside seat to the role it aliases; `structural: true`, `shoal: null` |

The feed keeps one edge per direction and class. The lattice's multi-shoal view draws one per pair of nodes: every
class and both directions merge into it (drawn as its most important class, a blocker first, then an ask, at its
hottest tier, with an arrowhead at each end that receives traffic), and its hover card lists each direction's
`1h / 24h` counts and kinds. A single shoal's view keeps its edges per class.

`n_rate`, `n_1h` and `n_24h` count events within the rate window (`--window-rate`, default 300 s),
an hour and a day. They are counts, never per-hour averages, and an ask a role raises in a queue version (new
against its previous version) counts as an `ask` message from it to you, in the edge and in the role's and the
shoal's `msgs_*` counts, so a role with an open ask never reads no messages to you in the window it raised it.
In the items layer a next item still blocked (its blocker as recorded, or only past its daily re-check:
`stale_kind: expired`) is a ring in the Board's blocked color, never the role's own tint; one whose blocker cleared
(`stale_kind: resolved`) is a red dot, work due. `rate_per_h` is an exponentially weighted rate with a one-hour half-life
(`ln 2` times the sum of `2^(-age_h)` over the day's events), so a steady r per hour reads r and a
burst fades within hours. `last_ts` and `last_age_s` are the newest event's, `null` for a structural
edge with none. `cross_host` is true when both ends have a known host and the hosts differ (the
maintainer node has none). `recent` holds the edge's last three events of the day, newest first
(`ts`, `age_s`, `kind`, the same `summary` as an event, and `item` and `url` when the event has them);
an edge with none omits it. The lattice shows them on the edge's hover card: hover a curve for its counts,
rate and last events, click it (outside a cluster disc in the global view) to pin the card, and a pinned
card's events link to their `url`. Writers map to node ids as above: `user`, `shoal` to `<id>:shoal`,
`ext-x` and `ext:x` to `ext:x`, a role slug to `<id>:<slug>`.

**Events** are the last `--events` (default 200) traffic and work events across every ledger file in
`ts` order: `msg`, `offer`, `withdraw`, `claim`, `release`, `done`, `grant` and mode changes (never
`state`, `queue` or host beats). Each has a `seq`, increasing along a cursor chain. `cursor` is opaque
(URL-safe base64 of each file's byte offset plus the last `seq`). With `--since <cursor>`, `events`
holds only lines appended after it, numbered on from its `seq`; nodes, edges and shoals are always
complete. A cursor that does not parse returns the plain tail with `cursor_reset: true`.

**Replay** (`--replay`) adds `replay: {from, to, dropped, events}`: every tail event of the last 24 h in `ts` order,
numbered on their own from 1 (the cursor chain is untouched), the newest 4000 kept and the rest counted in `dropped`.
The lattice's replay asks for it once (with `--events 0`), on the machine its feed reads.

**Hosts** are this host, every role's host, every shoal's home and every job host, with `home` true
for this host and per-host role aggregates: `roles`, `working`, `waiting`, `needs_you` and `msgs_1h`
(messages written by the roles there in the last hour).

**Freshness.** `fresh` says how live the status shown is, by one rule (`ui_status.freshness`, `is_stale`; the mods'
`fresh.js` is the same rule in JavaScript):

- `as_of` is when the rows' live state was read: the status cache's `ts` when the rows came from it, else when the
  graph's own harness listing was taken. `age_s` is its age when the doc was built.
- `stale_after_s` (90) is the age past which a reader shows the doc as stale. The status cache is read up to 45 s old,
  and a feed file or a home summary adds its own interval, so a live chain stays under it; a `graph.json` whose writer
  stopped (a host asleep, a unit down) passes it, and a reader judges that with its own clock (reason `old`).
- `stale` and `reasons` say what the feed knew was behind when it built the doc. A role node carries `state.stale` when
  its live fields (session, watcher, agent group) are not current (`ui_status.row_stale`): `unread`, a role on another
  host with a route whose row was built without that host's live state (the graph's own rows before the UI feeder's
  first status after a restart: such a role reads only what the ledger says, measured 2026-11-22 as `user-auth` for
  roles that waited at a prompt), or `host-old`, a role whose host did not answer the last read. A host with no route is
  never read, so its roles are not marked. A doc without `fresh` (an older feed) is not judged.

The lattice greys every marked role (and every role once the doc is `old`), dims its needs-you and pulse rows, puts the
reason on its card, and says `updating` with why in the canvas header and the rail; the home screen's host card does
the same for its summary (`launch --list --summary` passes the graph's `fresh` through).

**Sources and cost.** Role rows come from the status cache (`status.json`, written by the
`shoal-ui-feed` feeder) when it is at most 45 seconds old. Otherwise the graph builds them with
`ui_status.role_row` over its own View, running the local harness listing (`claude agents --json --all`: read-only, no wake, no
network) at most once per 45 seconds, with the opencode probe off and no remote merge. With `--ledger-root` the cache is skipped. The feed keeps each ledger
file's byte offset in memory and reads only whole lines appended since the last tick; the View is
folded from those parsed events, not re-read from disk. A tick:

- never pulls, commits or writes in a ledger clone, and never calls `next`;
- never messages or wakes a role;
- makes no provider or network call: blocker checks run with `live=False`, native turn state comes
  only from cache files, job details from local job meta files, and the only process it may start is
  the local harness listing above;
- does not change `shoal-ui-feed` or its document.

The document stays small: edges are aggregated, events capped, and `work_ratio_24h` is `null`
(role-time journals are not parsed).

## Subscription meters

Enable the account plans used on the host, once for every shoal:

```bash
shoal ui set usage_providers codex,claude,muse,opencode-go
shoal ui usage --refresh
shoal ui usage --json
```

A provider with more than one account on the host (`[accounts.<name>]` in `config/host.toml`, `shoal accounts`) gets
one row per account: each declared account is read with its own sign-in and shown by its name (`claude-2`; `cl2` in the
compact status line), after its provider's row.

`shoal ui status` displays the meter block above the role tables (a shoal's overview leaves it to the app's Usage
tab, so it stays about the shoal): a boxed
table with one row per provider and its 5h, week and month windows side by side.
In the app's Usage tab, each reset also reads as a wall-clock time in the zone of the machine the app runs on
("resets in 4h (14:30 PST)", the weekday within a week, the date beyond). Each reported window is a short bar, its used percentage and its reset date/time
in the host's configured timezone (`SHOAL_TZ` or `timezone` in host.toml), and
the last column says how old the reading is. A reached limit, a fallback reader
or a read error is named on a line under the table, and so is a reading taken from another host because this one
could not read the account (`codex: read on <host>; here: Codex is not signed in on this host`). The bars shrink (8, 5, then 3
cells) to fit the terminal, and a terminal too narrow for the table gets one text
line per provider instead. The status line (the quick view in the tab bar)
shows each provider's used percent for 5h, week and month, for example
`codex: 4%/23%/- | claude: 10%/87%/- | opencode: 4%/10%/13%`, with `-` for a window the provider does not report and no reset times. Use `shoal ui usage` for the
same snapshot outside Herdr. `--providers` temporarily chooses a comma-separated
list; an empty `usage_providers` setting disables background collection. A subscription that reports nothing until
its first use in a short window (Muse, `idle`) keeps its week: while the week's last reported reset is still ahead, the
use last read in it stays shown (`carried`: an idle reading says only that the new 5h window is unused, so the week
cannot have dropped); once that reset has passed it shows at 0% and "unused since its last reset", the reset rolled
forward by whole weeks (until it has been used once, there is none to show). `shoal ui set ecosystems
claude,codex,opencode` names the agent ecosystems in use on a host (default all four): one set aside drops out of Add
session's discovery and New role's and Handoff's providers at once, `shoal handoff` (a role's or a standalone seat's),
`shoal add-role` and Add session refuse it, and
it drops out of usage (the Usage tab, `ui status` and the status bar) once no role on it is left: a role still running
on it keeps spending it, so its meter stays, and `ui set` names those roles, until they are handed off to an ecosystem
in use. `--from <UTC>` schedules the change (for a subscription that ends at its quota reset:
`shoal ui set ecosystems claude,codex,opencode --from 2026-11-16T00:00:00Z`): the setting takes over at that time with
nothing to run then, and a later plain `ui set ecosystems` replaces it (adding an ecosystem back re-enables it).
The setting is per user and per host (`~/.config/shoal/ui.json`), so set it on each host; removing a provider this
way leaves its support in shoal untouched. Two things still read a provider set aside, locally and without spending
it: pruning old incarnations lists its sessions (so roles that moved off it are still cleaned up), and `shoal
resources` reports its tool when it is installed.

Color is green below 70 percent, yellow at 70 percent, and red at 90 percent or
when the service explicitly denies included usage. Text remains useful without
color: in the status line `~` after a provider name marks a stale reading, `LIMIT` a reached limit and `?` no reading; in the table `~` marks stale values,
and expired resets are marked `~` as well (the legend says stale or refresh due). The display never clears a limit merely
because a clock passed its reset. Unknown readings show `? unavailable` and a
reason. `--` means a window is not reported, including monthly quotas absent
from a provider's API. Claude's optional paid extra-usage budget is explicitly
named `extra month`; it is not treated as an included subscription allowance.

The feeder polls at most once per provider per five minutes in a background
thread. Every pane and shoal reads the same cache. Failed reads keep the last
measurement, mark it stale, and back off up to one hour. Read-only status and
line commands never start polling. `--refresh` forces one account read and
costs no inference tokens. There are no model turns or role wakes.

| Provider | Account source | Limits |
| --- | --- | --- |
| Codex | Native `codex app-server` account quota RPC, using the CLI's own login | Preserves multiple buckets and server denial; uses reported window duration, not assumed five-hour/weekly names. See the [official app-server account interface](https://learn.chatgpt.com/docs/app-server). |
| Claude | OAuth usage endpoint with the CLI-owned login | Included five-hour and weekly meters, additional named weekly limits, and optional extra usage. An expired login needs renewal through the CLI; the collector does not rotate credentials. |
| Muse | Login metadata endpoint, discarding the returned inference key and identity | Some responses omit `subs_usage`; this is unavailable, even if the subscription is active. No fake 0 percent and no automatic browser cookie access. |
| OpenCode Go | Quota endpoint with the current opencode credential store (read-only SQLite, legacy file fallback) | Five-hour, weekly and monthly meters when returned. A rejected key produces an authentication diagnostic. No opencode CLI/server is started. |

The per-user runtime cache stores normalized percentages, reset epochs,
observation/attempt timestamps and safe diagnostics, with mode 0600. It stores
no tokens, email addresses, account IDs, payment metadata or raw responses.
HTTP account reads refuse redirects. The feature adds no dependency and no
always-running process beyond the existing feeder. Account usage includes
standalone sessions sharing that login; it is not summed from role transcripts.

### Authentication without a browser

All four default collectors authenticate from local CLI credentials and call
account endpoints directly. No open dashboard, browser process, cookie import,
or laptop connection is required. Codex delegates credential renewal to its CLI;
the other collectors read credentials without changing their stores. An expired
OAuth token must be renewed through that provider's normal login.

Optional overrides go in `config/usage.env`, which is ignored by git, or a file
selected by `SHOAL_USAGE_ENV_FILE`. The file must belong to the current user and
have owner-only permissions (`chmod 600`). It is parsed as `KEY=value` data, never
sourced as shell code, and its values are never put in the cache or command
arguments. Matching process environment variables take precedence.

| Setting | Required credential |
| --- | --- |
| `SHOAL_USAGE_OPENCODE_GO_API_KEY` | A valid Go key authorized to read the Go usage endpoint |
| `SHOAL_USAGE_CLAUDE_OAUTH_TOKEN` | Personal CLI OAuth token authorized for subscription usage, not a generic inference API key |
| `SHOAL_USAGE_MUSE_DEVICE_TOKEN` | Muse device-login token; the login response may still omit subscription quota |

Codex subscription meters require a ChatGPT login in Codex. An OpenAI inference
API key is not a replacement for that login. Likewise, a Meta inference key is
not a verified credential for personal Muse subscription meters. An override
cannot supply quota fields that a provider does not expose to that credential.

Muse's interactive `/usage` is local and does not spend tokens. Its subscription
section can be a previous observation, explicitly labeled `as of`; a fresh
session can instead show `Currently unavailable`. Keeping a CLI open does not
make that observation current. `muse exec /usage` treats the text as a model
prompt, so the collector never uses it. A retained CLI observation must keep
its original timestamp and stale/reset-due markers. OpenCode Go's current
endpoint uses `percent`; the adapter also supports earlier percentage fields.

For accounts without a usable direct quota API, an operator may explicitly set
`usage_bridge` to a JSON object with `providers` and a `command` argv array.
It is disabled by default. Direct reads always run first; one fallback invocation
receives only failed, due providers as `{"providers":[...]}` on stdin. It returns
a JSON object keyed by provider containing the same native payloads described
above. No shell command string is executed. This interface permits a separately
maintained reader without embedding browser support or a provider SDK in SHOAL.
Cached fallback readings supply `observed_at` as their original Unix timestamp
in seconds. The collector preserves it instead of making an old observation
fresh, and refuses invalid or future observation times. Omitting the field means
the helper made a current account observation. A passive CLI reader can reuse a
timestamped report from an existing session without issuing a prompt or keeping
another agent process open.
Fallback observations are visibly labeled. If the chosen helper uses a browser
login, it inherits that login's expiry and host availability; it is not an API key
solution, even when no dashboard tab needs to remain open.

## The viewer: `shoal view`

```bash
shoal view <shoal> <role> [--stub] [--dry-run]
shoal view --harness claude|opencode --session <id> [--label NAME] [--stub] [--dry-run]
```

- **Resolution.** The session id is re-read from the live spec on every call, and checked against the
  harness's own listing. A dead or unlisted session is refused. Nothing is guessed.
- **The client.** `Backend.view_argv` supplies the argv.
  - opencode: the client gets the server password through its environment only; the password is
    never printed.
  - Claude: attach is exclusive, so detach another terminal first.
- **Environment.** `HERDR_*` and `ZELLIJ*` are stripped from the client's environment, so a Claude
  daemon first spawned from a pane cannot carry pane identity into every role's hooks.
- **Recaps.** A Claude attach sets `CLAUDE_CODE_ENABLE_AWAY_SUMMARY=0`. Claude Code can write an
  `away_summary` recap (a small model call, not a turn) when a client returns to a session with
  unseen activity. This was measured once on a testbed lane; `ui quiet-check` reports recaps
  separately from turns.
- **Stub mode.** `--stub` shows a role card that refreshes on each status rebuild (it looks for one every second,
  `ui_view.REBUILD_POLL_S`) and every 15 seconds at the latest. It attaches on Enter and comes back to the card on detach.
- **Detaching.** Inside an attached Claude session, Ctrl+Z detaches (`claude attach` exits 0); in an
  opencode session, Ctrl+C quits the client. Either way the session keeps running and the card comes
  back (both measured under a pty, 2026-10-12). Herdr's prefix keys do not detach: they go to Herdr,
  not to the attached client. `/bg` then `/exit` also returns to the card in a Claude session (seen
  by the maintainer); `/exit` on its own was not tested, so prefer Ctrl+Z.
- **A client left attached while you are offline.** An attached client holds a Codex or Muse role's wakes, and a
  Claude role's automatic restart, until it exits. While the maintainer is offline (`shoal offline on`), the health
  quorum ends a card's client left attached for an hour (15 min for a Claude context restart at 1.3x its limit; online
  too, 15 min after the page that says so) with a SIGTERM to that client only, and records it as a msg to `user` (`features.health.detach_cards`, default on;
  [architecture](architecture.md) §7). The session is untouched. A Codex or Muse card's observer says "The native
  client was ended (SIGTERM)" and is back on its live read-only view; a Claude card's `shoal view` says the client was
  ended, does not reattach, and the card comes back. A client started by hand in a terminal is never touched.

## The Herdr adapter

**Layout** (`shoal ui open`, idempotent):

- **One workspace per shoal**, named with the shoal's short tag: the id without its
  `-YYYYMMDD-HHMMSS` mint stamp (`alpha`), or the full id when two live (non-retired) shoals share a
  stem. A
  workspace still under the old `shoal:<id>` label (or a full id) is renamed in place. A retired
  shoal's workspace is released so its successor (same stem, same tag) never inherits it: `ui open`
  closes it when every pane is disposable, and otherwise (and in the feeder, which never closes)
  relabels it with the retired shoal's full id. `shoal retire` never calls the UI. It has two tabs:
  - **`overview`:** the `ui dash` status pane and a read-only `ui tail` of the ledger.
  - **`roles`:** one role card per role that has a session, each pane labelled `<id>/<role>`.
- **One workspace per standalone session**, named after the session (the old shared `standalone`
  workspace is retired by `ui open` when none of its sessions is attached).
- **Re-running.** Gaps are filled by re-applying a whole tab with `layout.apply` (cards hold no
  state). This covers a new role, and every pane coming back as a bare shell after a Herdr cold
  restore. It never happens while any pane in that tab is in use: a pane is replaceable only when
  every foreground process is this checkout's CLI running a card, stub or overview, or an idle
  shell (an allowlist). Cards started from a different checkout path (an old worktree, a stale
  install) therefore read as in use and the re-apply refuses. Detach and close those cards by
  hand; do not widen the match.

**Feed** (`shoal ui feed`, the `shoal-ui-feed` user unit, every 20 s after a pass of about 2 s, so a status rebuild
lands about every 22 s; a pass lists `claude agents` once per kind, `backends.agents_memo`):

- **Cards for other hosts' roles need a route.** A session resolves only through its own host's harness, so
  a role on another host is attached by running that host's `shoal view` over ssh. A role whose host has a
  route in this host's config (`[hosts.<server>] ssh = "<user>@<name>"`, `routes.py`) gets a card like a
  local one; Enter attaches over ssh. A Claude attach is exclusive, so an attach that fails within 15 s says
  why (host unreachable, connection ended, or attached elsewhere) and retries every 30 s instead of taking the session over
  (`q` gives up, `r` retries now). A role whose host has no route has no card: it stays in the overview table
  with its host, answered by mail.
- **Live state of routed remote roles.** Harness liveness and sidecars are readable only on the role's host,
  so the read model pulls that host's own rows (`shoal ui status --shoal <id> --rows-only --json`, which has
  no usage and never pulls further hosts) over the route, pulled again once older than 15 s
  (`routes.STATUS_PULL_S`, so each feeder rebuild reads it afresh; a pull that failed is retried after 30 s), and takes their live fields
  (`session`, `watcher`, `can_progress`, `needs_you`, `context_k`, `heartbeat_age_s`, and `agent`, the agents-list
  group: an open question is read from the role's transcript, which only its host has). Each merged row
  carries `remote_status` (`ok`, `age_s`, `stale`). A failed pull keeps the last good rows for up to 10 min,
  marked stale, and the card says so. Without a route a remote row reads `session: remote`.
- **Role panes:** each tick reports `pane.report_agent` (source `shoal`, agent = role slug, state
  `blocked` when the role needs you, `working` while the session is mid-turn, otherwise `idle`) and
  `pane.report_metadata` tokens. A pane whose foreground is a bare shell (a card laid out while its
  role was parked, or a cold restore) gets the tokens only: Herdr clears an agent report from such a
  pane within a fraction of a second, which made the role flash into the agents list every tick. The
  verbose feeder names those cards; `shoal ui open --reapply` brings them back. The tokens are `role`, `shoal` (the short tag), `host` (multi-host shoals only), `mode`, `status`,
  `item`, `next`, `wait`, `asks`, `offers`, `watch`, `age`, `why` and `harness`. The agents rows
  show the role name, then the `mode` glyph, then the shoal tag dim; Herdr's default `agent_panel_sort = "spaces"`
  already groups agents by workspace.
- **What the icons mean** (`shoal ui legend`, or the palette's `legend` entry). Herdr draws the state icon from the
  state shoal reports: half-moon yellow is `working`, hollow circle green is `idle`, red x is `blocked`, a grey dot is
  nothing reported. `working` covers two different things, so the `mode` token says which: `▶` a turn is running,
  `◷` the role waits on a job, CI build or lease running for it (by design it reads working, so a lane with a job in
  flight does not look stalled). No glyph means asleep, idle or needing you. The glyphs live in the client's sidebar
  rows: after an upgrade re-run `shoal ui herdr-config --client --write` (the laptop's `shoal-herdr-setup.sh`) and
  relaunch. **Standalone seats** get the same tokens (`status` is `working`, `attached` or `idle`). For a Muse or
  Codex seat the backend's `status:busy` is a running turn OR a client merely attached, so it also reports `active`
  or `quiet` by the age of the session's journal or rollout file (120 s): `attached quiet` reads idle, so a seat that
  waits for you is not drawn as working. Standalone registry labels are unique: two entries with one label share one
  pane label, and their equal-sequence reports collided (the first won), so the later registration is kept and
  `ui pick --save` replaces a same-label entry and says so.
- **Workspaces:** `workspace.report_metadata` tokens `shoal_mode`, `shoal_needs`, `shoal_stalled`,
  `shoal_asks`, `shoal_pages`, `shoal_conditions` and `shoal_tooling`. (`shoal_relay` went with the
  retired wake relay: a client block pasted before then still names it and draws nothing; refresh it
  with `shoal ui herdr-config --client`.)
- **Failure shape:** tokens carry a TTL of three intervals, so a dead feeder clears them instead of
  leaving stale data.
- **What you get from Herdr:** its own notifications on a role that turns `blocked`, and the agent
  list sorted by state.

**Actions** (plugin `shoal.herdr` in `plugins/shoal-herdr/`):

- **Keys:** prefix, then:
  - `Alt+s`: the action palette;
  - `Alt+a`: asks;
  - `Alt+n`: next `--peek` for the focused role;
  - `Alt+m`: message;
  - `Alt+o`: open a standalone session;
  - `Alt+t`: status;
  - `Alt+g`: metrics.
- **The palette** also offers offer, withdraw, offline, freeze, health conditions, re-apply layouts,
  and an upgrade preview (applying an upgrade stays a CLI step).
- **The focused card is the target.** An action passes the pane focused when the key was pressed
  to its popup (`SHOAL_UI_FOCUS_PANE`, from Herdr's `focused_pane_id`). `next` uses that role as is.
  A write verb offers Enter to keep it, `o` for another and Esc to cancel.
- **Popup keys.** Every prompt cancels on Esc. Every popup closes on Enter, Esc or `q`, including
  status and next when their text fits the popup. Text longer than the popup opens in `less`, which
  closes on `q` (or Ctrl+C) only, and its prompt line says so.
- **Panes reload on upgrade.** A role card, standalone stub, dashboard or ledger tail re-execs itself
  when shoal's code changes (the new modification stamp must hold for two checks), so an upgrade
  reaches open panes within about 30 seconds. Panes started before this existed need one
  `shoal ui open --reapply`, which re-applies the roles and overview tabs when nothing in them is in use.
- **Role cards** are a field and value table (status, session, watcher, last write, active, next,
  wait, asks, offers, attention), fitted to the pane.
- **Status layout.** `ui status` (and the status popup and dashboard) prints one ASCII table per
  shoal fitted to the terminal width: role, status, session, active, next, last write, attention.
  Below 110 columns, when that table would not fit, it splits into a status table and a work table.
  `--width N` sets the width; `--json` is unchanged.
- **Wake costs are named** before confirming: a `--wake`, an offer to an idle role, a mode toggle
  that wakes every role.

**Standalone sessions** (`shoal ui pick [--save]`, `shoal ui forget <id>`) list Claude background
sessions, OpenChamber sessions, recent interactive Codex threads, and stored Muse root sessions.
Discovery uses each registered backend's `sessions()` hook. A future ACP backend can supply that
hook without changing the picker; full ACP discovery conformance is deferred. Codex (`thread/list`)
and Muse (`session/list`) are queried through bounded local stdio servers:
listing does not resume a session, acquire its lock or request a model turn. Codex lists interactive
sources (`cli`, `vscode`); `--harness HARNESS --id ID` also opens a known session outside those lists.
A missing harness does not block the others; listing errors are reported. Palette option 12 and the
plugin picker keep empty-list and server errors visible until dismissed. Escape or empty input
cancels normally. Picking opens a card in its own workspace; the harness attaches only on Enter.
The picker preserves Claude job ids and available model metadata and leaves out harness subagents
and shoal's own named sessions: matcher runs (`shoal-matcher-*`) and every incarnation of a
role (`<slug>-r<N>`, current or retired). Saved ones (`~/.config/shoal/ui-standalone.json`) come back on `ui open`, except one that was closed in the viewer:
once a saved session has had a workspace, a later `ui open` that finds it gone (while Herdr still holds other
labelled panes, so not a fresh server) marks it `dismissed` and leaves it closed across viewer restarts.
`shoal ui open --show <label>` brings it back, `shoal ui pick --save` re-saves it, `shoal ui forget` drops it.

**Claude identity: job id, not conversation id.**
- **The two ids:** a Claude background session has a stable job id, the 8 hex that `claude attach`, `stop` and `respawn` take.
  It also has a conversation id, which a `/clear` replaces.
- **What shoal stores:** a spec keeps the id a role was launched with. The registry keeps the id current when the session was
  saved, plus its `job` (recorded by the next `ui open`).
- **How it resolves:** every lookup (cards, `shoal view`, health, watchers, the context advisory) matches a stored id against
  the job id, the current conversation id and the job's original one, from its `state.json`.
- **After a `/clear`:** nothing has to be refreshed. The card still attaches, and transcript reads follow the new
  conversation.
- **Retired sessions:** one with no worker (retired after 8 h idle, or stopped) is still attachable. `claude attach` wakes its
  worker without taking a turn.

**Backgrounding an interactive Claude session.** A card attaches background sessions only, so a seat picked
while it ran interactively in a terminal has to move to the background first. There are two ways, and they differ:

- **`claude --bg --resume <id> --name <label>`, after exiting the interactive window:** keeps the session id, so the
  card attaches it with nothing to follow. Never run it while the interactive window is still open: that starts a
  second copy of the conversation.
- **`/bg` inside the session:** Claude Code starts a new background job with a new session id (its
  `~/.claude/jobs/<job>/state.json` has `interactiveLineage: true` and the session's user-set name, but no field
  naming the session it came from). A second `/bg` makes another job named `<name> (2)`.

**How a card follows a `/bg` successor** (`ui_view.resolve_standalone`). Only when the recorded session is not
listed as a background job, the card looks for successors among `claude agents --json --all` rows: kind
`background`; named as the entry's label or the label followed by ` (N)`; in the entry's `cwd` (when it has one);
`interactiveLineage` in the job's `state.json` (an unreadable file is accepted and noted on the card); started after
the entry's `seen_utc`; not referenced (session or job) by another saved entry; and not finished (`stopped`, `failed`, `error`, `killed`, `cancelled`, `exited`). A `done` job is not
finished: it has ended its turn and sits idle, and `claude attach` reopens it.
`seen_utc` is refreshed by the card whenever the recorded session is listed (at most every 10 minutes). An entry
saved before that field existed falls back to its `saved_utc` (written by `ui pick --save` from now on), else the
first timestamp of the original session's transcript; with none of these the start time is not checked, and the card
says so.

| successors | original live interactively | card |
| --- | --- | --- |
| 0 | no | refuses: exit any terminal still running it, then `claude --bg --resume <id> --name <label>`; stopped copies are named (`claude attach <id>` resumes one) |
| 0 | yes | refuses: where it runs (pid, tty), and the same exit-first advice |
| 1 | no | follows it: the entry gets the successor's `session` and `job` by atomic replace, after a one-time backup `ui-standalone.json.bak-bgfollow-<utc>`; the card shows `followed /bg successor <job> (was <old>)`, then attaches as usual on Enter |
| 1 or more | yes | warns `two live copies`, naming the interactive original and each successor (job, start time, state), and does nothing else |
| 2 or more | no | lists them numbered; a digit key follows that one, as for a unique successor; nothing is guessed |

A card never stops, kills or resumes a session. `shoal view` without a card gives the same answers.

**Context handover for standalone sessions** (opt-in): `shoal ui handover <label|job> --doc <plan_state> --mode
advise|auto`, `--list`. The feeder advises a session past its limit to refresh its start doc and, in `auto` mode, resets it
onto that doc in a new conversation (for Claude, a new job, which the registry follows). See [`handover.md`](handover.md).

**Standalone sessions on other hosts.** On `ui open`, each host with a route is asked for its own saved
standalone sessions (`shoal ui standalone --json` over the route, cached 30 s), and they are mirrored into this
registry with `host` set and the label suffixed `@<host>`. They get cards like local ones, and Enter attaches
over ssh, with the same 30 s retry when the attach fails fast. Their turn state comes from that host's own probe.
A reachable host's list is the truth (a session it no longer saves is dropped here); an unreachable host's
mirrored entries stay as they were. Closing a mirrored card dismisses it here only. `shoal ui standalone`
reports only a host's own open sessions, never its mirrors, so hosts never re-export each other.

**Quiet check** (`shoal ui quiet-check --save F`, then `--compare F`). It proves that viewing woke
nobody. For each Claude role it classifies what the transcript gained:

- **`turns`:** completed turns. This is the number to judge: a turn the period's offers and wakes
  do not explain is a wake to investigate.
- **`turn_inputs`:** the prompts and notifications that started those turns.
- **`recaps`:** `away_summary` model calls, which are not turns.
- **`other`:** assistant messages and tool results inside those turns. It is context: `other > 0`
  is not an extra wake.

For opencode roles it reports whether the session's `updated` time moved, and for every role,
ledger lines.

**Metrics** (`shoal ui metrics`) prints failure-mode counts from the health review's `fm_report.py`
and the latest report files. The browser view is the report HTML (`shoal report --out-html`).

## The Herdr app

The [Herdr app](https://github.com/herdrdev/herdr-app) is a desktop window over herdr-core (`hc`), which
runs every terminal on its own machine. The same three rules hold. herdr 0.9.3 and everything above keep
working beside it.

```bash
shoal ui app-start [--write-desktop] [--dry-run]   # start the app with the pin held, open the home space
shoal ui app-open [--machine NAME|local] [--shoal ID] [--show LABEL] [--no-standalone] [--no-focus] [--dry-run]
shoal ui app-plan [--shoal ID] [--json]            # this host's wanted spaces (read-only)
shoal ui app-install [--check] [--dry-run]         # the graph feed unit here; the mods installed into the app
shoal ui launch --list [--json] [--summary]        # the hosts the chooser offers, as data
shoal ui act-plan VERB --shoal ID [...] [--json]   # an action's preview and one-time token, or a view
shoal ui act-plan offline|freeze --scope all|host:NAME --state on|off [...]  # the same across shoals
shoal ui app-sync                                  # copied mods and clones refreshed here, roles tabs follow the roles everywhere
shoal ui app-lattice                               # show this space's lattice, or the global one
shoal ui app-home                                  # show the SHOAL-home space on this machine
shoal ui app-usage                                 # show the usage dashboard (SHOAL-home's usage tab)
shoal ui app-board                                 # show the board graphs (SHOAL-home's board tab)
shoal ui board [--shoal ID] [--json]               # each shoal's boards as dependency graphs, with metrics (read-only)
shoal ui act-apply --token T [--json]              # run a planned action once
shoal ui act-apply --token T --background --json   # detach a confirmed Handoff, Offline or Freeze
shoal ui act-status --run RUN --json              # follow its retained progress log
shoal ui act-status --verb offline --scope all --json  # the newest Offline across shoals (or --shoal ID; Freeze alike)
shoal ui queue --shoal ID --role R [--json]        # one role's queue and its candidates (read-only)
```

- **The queue manager.** `Queue`, on a role in the actions dialog (right-click a role in the lattice, or Actions in the
  top bar), lays out that role's queue before anything is written:
  - **Its queue:** the active item stays pinned, with the next items below it. Each row has ↑ and ↓, Move, Blocked
    (what it waits on, sent to the role) and Drop.
  - **Candidates:** open offers it has not queued, claims it holds outside its queue, board items in its scope (the
    digest's, else the board's for a fill=self role), items another role released lately (blocked, lost-race or
    declined, in the last 14 days), and items another role holds or was offered. That last group is drawn in violet,
    with its owner, so a move between roles is plain. A filter narrows the list.
  - **Placing:** click Pick on a candidate (or Move on a row), then one of the `place here` slots that open between
    the rows. `+ end` adds a candidate at the end. Element drag and drop is not in the app's mod kit (its only drop
    takes files from outside the app), so a pick and a slot stand in for it.
  - **Preview** plans the whole layout as one batch (`ui act-plan queue-set --order=a,b --drop=c --blocked=item=why`).
    Each step runs its own `--dry-run` under one token, and Confirm runs them in order, stopping at the first
    failure:
    - `withdraw`, for an open offer you drop;
    - `offer`, for a board item a fill=assigned role cannot take itself, or a released item, with its position as
      the prio;
    - `transfer`, for another role's item: the holder is asked to hand it off, and it joins this queue once that
      lands, so it is left out of this order;
    - one `msg --kind request`, to release a dropped claim and to record blockers;
    - `queue-order`, last, which the role applies at once and acks.
  - **Refusals:** a dry run's `REFUSED <item>: <reason>` lines come back by item and mark their rows. No token is
    handed out, and the layout stays to fix. `queue-order` refusing an item this same plan offers first is expected,
    and not counted.
  - **Pending state:** a pending transfer and an order not acked yet show above the queue.
  - **On an older shoal:** `transfer` and `queue-order` are their own shoal verbs. A host whose shoal lacks them says
    so and plans nothing, and `ui queue` reports `verbs`.

- **Dismissing a needs-you.** Right-click a role's row in the agents list, or use `Dismiss` in the actions dialog (`shoal dismiss --shoal ID
  --role R [--undo]`). The row leaves "needs you" until something new is raised: a change to its reasons, its asks, what its session says, or its
  status. The role stays as it is, its watcher runs and its work accumulates, and health's conditions and pages are unchanged. A dismissal over a
  dead session or a dead watcher lapses after 24 hours. It is a `user` event in the shoal's ledger, so every host and app agrees, and a role's own
  host decides, so a row can take a minute to clear. Being display only, it does not change auto-park: a role whose session reads `blocked` stays out
  of it, dismissed or not. To stop a role instead, park it.
- **Zoom.** `ctrl-shift-z` (`pane.zoom`) zooms the focused pane and back. Clicking a row in the agents list while zoomed zooms that
  role, in its own tab if need be, and the key gives the grid back.
- **The agents list's sections.** In the Priority view the rows sit in **Needs you**, **Working**, **Waiting**,
  **Blocked**, **Held** and **Dismissed**, by each card's derived agent state (the `shoal.group` and `shoal.state` labels
  its card writes; a role's from its status row's `agent`, a dismissed one in Dismissed with its state still shown).
  A role row reads `space · state`, and its tooltip is the state's detail (`shoal.detail`). A row with no derived state
  (a status from before it, another agent in the app) keeps Herdr's own sections by its reported status, so Done
  (finished, not looked at) and Idle appear only for those; a card's done is its status dot. Only a local Claude seat's
  card reports, so a seat on Codex, Muse or OpenCode, or one on another host, stays in Herdr's own sections. A role
  card keeps its labels current through an attach, whatever its harness, since they go through the core and not the
  terminal.
- **A row that is not live yet.** A card sets `shoal.stale` while its role's row is not live, and the row is greyed
  with `⟳` after its name, its tooltip saying why before the last known detail: `starting` from the card's start
  until its first status read lands (a card back after a restart reads the status cold, for tens of seconds, under the
  labels its last run left), `unread` or `host-old` for a role on another host whose live state was not read or did not
  answer (`ui_status.row_stale`). The card clears it with its other labels when it is left with `q`.
- **Answered, refreshing.** A card shows a row ahead of the status it read while that row still says the role needs
  your answer, from what it knows sooner (`ui_status.card_view`): your reply written on this host (`shoal msg --by
  user` leaves a hint, `/tmp/<id>/reply-<role>.json`, beside the ledger event) moves it to Waiting, state `answered`;
  its session running again (Claude from the listing the host's cards share, at most 25 s old, with its open question
  read from the transcript; Codex and Muse from their turn lock; for any role, the client attached under its card
  reporting its turn, `ui_view._client_turn`, the only read for a role on another host) moves it to Working, state
  `answered` when its open question closed and `working` otherwise. Each sets `shoal.stale` (`answered` or `resumed`),
  so the row is greyed with `⟳` and its tooltip says "you answered; the status is catching up". So the row leaves Needs
  you within one card refresh (15 s) of your answer, and a row still in Needs you, not greyed, is one the status says
  asks you now (a new ask, or one you have not answered). Only rows that need your answer are read this way, and never
  toward needs you: a new ask is the status's to show. The status catches up within one rebuild and the mark goes.
- **A status read before your answer.** What a row is compared with is when its live fields were read
  (`ui_status.row_read_at`), not its status time: that time less the age of its host's pull for a role on another
  host, less `READ_LAG_S` (10 s: a status pass lists the sessions before it builds the rows, the other host listed them
  before the pull returned, and Claude's service writes a turn's state seconds after it changes). A question answered
  in the client attached under a role's card on another host came back as asking on the next rebuild, and the card
  wrote `blocked` over the client's `working`: the row went back to Needs you, with the chime, for about ten seconds
  (2026-11-26). Now the card reads the client's report each second through an attach (a turn that moves sends a report
  at once) and once more as the attach ends, and keeps it after: read only at each refresh, a detach seconds after the
  answer left the card with the question still open (2026-11-26). So that row reads Working, `answered`, and the card
  reports `working`, until a status read after the answer shows the role as it is.
- **Asked in an open client.** A row moves to Needs you at once when the client attached under its card (Claude Code's
  own, `app=claude-code`) reports itself blocked on a question or a permission prompt: that report is live, while the
  card's labels wait for the next rebuild (a row read `working · attached` while its session sat in its question,
  2026-11-25). Its state reads `question` (or `permission`), its tooltip says `its session asks you now`, and it goes
  back to its labels when the client reports something else. The other way too: when that client reports its turn
  running while the labels still say `asked` or `question` (you answered there), the row is in Working at once, state
  `answered`, greyed "you answered; the status is catching up" (`shoalClientAnswered`), until the card's next read. A
  dismissed row stays dismissed, and a seat's card reports its own.
- **A row's time** (`6m`) is how long the row has been in its section, as the list saw it move there (Herdr's own
  stamp, kept across app restarts, keyed in `shoal-agents` on the card's `shoal.group`, `shoalTrackKey`): in Needs you,
  how long it has needed you since it last went there; it starts again when the row moves (answered, working, asked
  again). Herdr keyed it on the reported status, so a row moving between sections with the same status (needs you as
  asked, then as a question) kept its time. Its tooltip says what it is. A row with no group keeps Herdr's key. The
  first app start after the change starts every role row's clock once.
- **How old the status is.** The list's header says `status 20s ago`: the first card on a machine to read each status
  rebuild labels its time (`shoal.asof`, `ui_status.claim_asof`: one label per machine per rebuild, since each label
  written redraws the app), and the header shows the oldest machine's newest, in the warning color past 90 s; its
  tooltip gives each machine's and the cadence. The status is rebuilt by the feeder every 20 s plus the pass itself (about
  2 s since the pass lists `claude agents` once, `backends.agents_memo`: it listed 26 to 28 times and took 18 s, so a
  rebuild came every 38 s, measured 2026-11-24), and each card re-reads it on the rebuild
  (every 15 s at the latest, `ui_view.rebuilt_since`), so a row can lag its host's ledger
  and sessions by up to one rebuild interval plus about a second (about 13 s on average and 25 s at most at the rebuild
  every 23 s measured 2026-11-25; before, a role on another host, pulled once in 46 s, took 32 s and 62 s, and one here 20 s
  and 39 s, since a card had a 15 s timer of its own). The header counts up by the
  second from the label's time (`shoalTick`): it stayed at `0s` and then jumped to `20s` or more, since it moved only when
  the list redrew (a label changes, else Herdr's 30 s tick). Every redraw of a mod redraws the whole window and reruns
  the list's render (about 2,000 element calls for 25 rows: with stub elements, 0.4 ms in V8 and 1.9 ms with its JIT
  off; the clone's render also took the whole terminal list 320 times for 24 rows, 44 to 55 ms, against 1 time and 3 ms
  for Herdr's own mod and, since, for the clone: `shoalIndex` reads it once a render and the tick reads none), so the tick is a data timer that checks twice
  a second (no second is skipped) and redraws only when the header
  would read or color differently: once a second for the first minute, then once a minute, and once when it passes 90 s
  and turns the warning color. It ticks at the lattice's full and balanced motion tiers (its `motion.js`, copied beside
  the clone, auto: the renderer and the window's focus). At low and off (a software renderer, where a frame costs most
  of a core, or an unknown renderer in a window without focus) it redraws once to say so on its tooltip and then moves
  only when the list redraws, as before. It asks for nothing while it has no header (the rail collapsed, the sidebar
  hidden) or while the redraw it last asked for is undrawn (a copy in a hidden block or another tab keeps its timers but
  is not drawn), and the copy's `close` cancels it (the timer is the copy's task, so it ends with the copy anyway).
- **What the agents list says.** A turn in progress reads working. A role waiting on its own job or CI reads working too, unless it also has
  an ask or an open question: then it needs you, and its message says the job runs on. A job that has ended no longer reads as working, though the role
  has yet to take it up. A role whose host cannot be reached reads idle, with its last known state in its message.
- **Leaving an attach.** A card restores the terminal's modes after every attach, since a client that dies with its link cannot, and a Ctrl-C
  returns to the card: `q` is what leaves one.
- **The lattice in depth (`d`).** The lattice opens in depth (iso): one press of `d` gives the flat view, the next
  tilt and the next iso again. The shoal globes are see-through glass, fainter until you point at one, with their
  names larger and clear above them, and their role names switch between light and dark ink to stay readable on the
  globe. The canvas behind them is a lighter slate than the flat view's, so shadows read on it. One light, a lamp high
  over the scene, up and to the left at the opening views, lights every globe and casts the shadows, each where the
  light's ray through its caster meets the floor below: each globe's and yours. You hover close to the floor,
  so your shadow sits
  just beneath you, down and to the right. The lowest globe has more clearance above its floor shadow, and the
  broad blue-violet glow around you fades continuously instead of showing concentric bands. Roles, outside seats
  and arrows cast none (2026-11-14:
  dropped for simplicity after two passes at making them agree with their casters in every view). Everything has one
  place in the world: a role is drawn where that place projects and its arrows run to it, and a globe is drawn as the
  outline a camera would see. The globes' stems and yours cast a faint shadow too, from the foot to the caster's
  shadow, so each shadow on the floor is tied to what casts it. Every shadow falls off smoothly from a dark middle
  to nothing at its edge, with no rings, and lies under every role, arrow and label, never over them. The lamp
  stays put as you orbit, so the lit sides and the shadows turn with the scene. Drilled into one shoal, depth mode
  shows a bowl turned far round to the side, its opening to your left and its bottom to the right, a little below
  level, with you floating out in front of the opening, nearest and largest, and a dashed plumb line from you to the
  bottom: the opening has a lit lip, the sphere's outer wall shows beside it as a broad shell from its top round to its
  bottom, and the far wall rises well past the rim of a hemisphere, so you see more of the back and the outside. The
  bowl has its own lamp, fixed to the bowl above and behind the opening, so it shines in through the opening onto the
  bottom and the bowl's shadow falls on the floor to its right; the inside darkens smoothly toward the bottom (no rings
  of shading; faint rings of latitude stay as guides), and the wall under the lip on the lamp's side is in a soft shade
  that grows in gradually as you orbit and fades to nothing at its edge. You are a small magenta glass ball (always
  magenta, the needs-you color, so you stand out against the bowl and every host's color) lit from that lamp; your soft shadow falls on the
  wall where its rays put it, so you read as floating in front of the wall (the roles and arrows cast none). The hosts' regions share the whole inside of the bowl in clearly different colors (azure, then amber, green,
  rose and on), each host's roles sit on its region behind you, well inside it (a fifth of its width from either edge,
  so which host a role runs on reads at a glance), where the inside is seen and clear of one another, and
  the arrows, light cores
  over dark casings, taper from you as they go: wide and bright at you, thin and dimmer at the far wall, with smaller
  heads there, so they run away from you into the bowl. A role that needs you or asks you pulses in depth as in flat
  (breathing at `full`, blinking at `balanced`, still at `low` and `off`).
  - **Keys in depth:** the arrow keys orbit the camera (8 degrees a press), keeping the zoom (only the wheel, `+`
    and `-`, and a fit change it). The global view opens turned 95 degrees round and 10 degrees higher than the iso
    preset, framed a little zoomed out and up, the view the maintainer picked (2026-11-18); `f` frames it again. The bowl opens turned far round to the side
    (-14 degrees tilt, -70 turn), with you floating out in front of its opening. Up and down raise and lower the
    camera (-30 to 64 degrees), and left and right take it round the bowl's side (-78 to 78 degrees), together never
    more than 78 degrees off the bowl's axis: a press that would pass a limit goes to it and no further, so the bowl
    never goes edge-on or flips and the way back answers at once, from a corner too. From every angle you are seen to
    stand off the wall behind you. In the global view left and right turn the shoals' planes and up and down raise and
    lower the camera. Shift with an arrow pans (a plain arrow pans in the flat view), drag pans in any view, and `,` and `.`
    turn the view 15 degrees: the planes in the global view, the bowl round its own axis per shoal. Going to flat
    (`d`) resets the orbit and the turn.
- **The Board tab** (`shoal-board`, the top bar's Board, on the shoal of the space in view): a shoal's priority board
  as a dependency graph, read from the board digest on the shoal's home host and the roles' queues.
  - **The graph:** the board at the top, prerequisites below, so it reads one way throughout: every item waits on what
    is under it. A sub-issue sits a layer below its parent, and a blocker a layer below every item it blocks. A gray
    curve joins an issue to its sub-issue; a dashed orange arrow rises from a blocker into what it unblocks
    ("unblocks" in the legend). A parent or blocker not
    open on the board is drawn dashed, `off board`. Items with no parent, sub-issue or blocker on the board are in a
    grid below the graph, in progress and queued first. The root reads `closed` once nothing on the board is open.
  - **Where each issue stands:** in progress (amber, `▶ role`, or `▶ CI · role` and the like while its own CI, job,
    research or window runs: a role waiting on its own work is progress), queued (blue, `role #n`: its place in that
    role's queue), blocked (red border: `⛔ #923` on another issue, `⛔ upstream`, `⛔ maintainer`; dashed, `⛔ hold`,
    when only a free-text hold says so and nothing is named), not queued (gray). The hover card says it in full
    (`in progress: CI`, `blocked on #923`, `blocked: free-text hold`), and `(read from its hold)` when a hold's words
    were what showed the item's own run.
  - **Cards or Nodes:** the toolbar switches between the cards (key, where it stands, title) and nodes: a shape per
    repo (named in the legend), its issue number under it, colored and outlined as the cards are, for the relationships
    at a glance. **Zoom** with - and + (or the keys), **Fit** (0) puts the whole tree in view, and `n` switches the
    drawing; the choice of drawing is remembered.
  - **Hover** an item for its status, role and place, blocker, parent, sub-issues, assignees, labels, PRs, last update
    and a body excerpt. The card stays on screen: beside the item, flipped up near the bottom of the view, inside its
    edges, and scrolling when taller than the room it has; move onto it to read or scroll it. **Click** it to select
    it: the side panel (its x, or Esc, closes it) has **Open on GitHub**,
    and **Offer to a role** (an item no role holds: the actions dialog's Offer, filled in) or **Move to a role** (an
    item another role holds: that role's Queue opens with it picked; place it and the plan transfers it), each a list of
    the roles to pick from. Both preview and confirm.
  - **Metrics** above the graph: open (and its change over 14 days), done (percent and count), in progress, queued,
    blocked (and how many on a free-text hold), not queued (and how many wait on a blocker), closed in 7 days, the days until none are open at that
    pace, and 14-day trends of open items and closes a day. The trends come from `board_history.jsonl`, written with
    each digest, so they fill in from the first digest after an upgrade.
- **Status while attached.** A role card reports its state to the agents list through OSC 7501 (`kind` picks the badge: `?` a
  question, `\u26b7` auth, `\u2691` a grant). herdr-core drops a report whose `kind` it does not know, so a cause with no
  kind goes in the card's `shoal.need` label instead: `\u2715` its session is dead, `\u25cc` its watcher is dead and it has
  work, `\u2298` a blocker that cannot move; `!` is left for a cause nobody named. A session that says it waits on you reads `?`. A Claude role's card keeps reporting during an attach, writing only a changed report, since the attached client
  draws on the same terminal. Other harnesses keep the report from before the attach: Codex's client reports itself (`app=codex`), and two
  reporters would take turns on one row. A report the attached client takes away is written again, whatever the harness: each
  refresh the card reads what the core holds for its terminal (`hc get`), and when it holds no report the card writes its own,
  then keeps it current while the core holds it, until the client reports again. Claude Code's attach client reports its
  session itself (`app=claude-code`) and clears that report when the background service restarts under it (an update),
  while it stays attached; the app then lists no agent for the terminal, so the row left the agents list exactly while you
  worked in that session (measured 2026-11-25). While it holds a report of its own, a Claude card writes its report back
  within a second (after reading the client's turn from it): the client's report names no role, so the row drew the
  provider's plain icon and the card's machine in place of the role's account badge and host, flipping at each turn
  boundary (measured 2026-11-27: two roles read as moving between accounts). A client at a question or permission prompt
  keeps its report, the live signal of a question asked there; the row's host then comes from the card's `shoal.host`
  label (the card's own report says it in its title, `@<host>`). An attached row says so: `attached` after its state in the priority view
  and before its tooltip (the `shoal.attached` label). After any attach the card writes its report again, even an unchanged `done` (a Codex client takes its own report with it when it exits, which left the role off the agents list), and clears the `shoal.attached` label again. The card also sets the `shoal.attached` label for the length of an attach, since the core still names
  the card as the terminal's foreground under a client, and a rebuild of the roles tabs leaves a labelled pane open. That hold covers every roles tab of the shoal until the last card detaches: a
  removed role's card goes, and the grid is laid out again, at the next sync after that, so the grid can change just after a detach.
  A new role's card (a role unparked, or added) does not wait: it appears at once in a roles tab of its own (the next free
  `roles N`), and nothing else changes until the detach.
- **Spaces.** `app-open` gives each shoal a space named with its short tag, on its home host (the spec's
  `home`; a shoal with none counts as every host's), with the tabs the 0.9.3 adapter uses: `overview` (the dashboard; its `l` shows the ledger tail in
  place, and `overview_ledger: true` in `ui.json` keeps a tail pane, while with it off the sync closes one), `roles` (four cards a tab
  in a 2x2 grid, two or three stacked, more roles in `roles 2` and on) and `lattice` (the `lattice-view` mod on that
  shoal, reading the feed of the space's machine). `shoal ui app-sync`, run every two minutes by the `shoal-status` bar,
  lays roles tabs out again when roles are added or removed, unless a card is in use (then only a new role's card is
  added, in a tab of its own), and leaves anything you closed
  closed. A shoal's or a seat's space has no lattice tab of its own any more (one shoal made before is closed, once): the
  top bar's **Lattice** (Ctrl+Alt+L) opens the global lattice in `SHOAL-home`, drilled into the shoal of the space in view,
  and `shoal ui app-lattice` does the same from a terminal, and
  `shoal ui app-home` (the bar's Home button) goes to the `SHOAL-home` space from anywhere, at once when its tabs are current,
  `shoal ui app-usage` (the bar's Usage) to its usage dashboard, and `shoal ui app-board` (the bar's Board) to its
  board graphs.
  Ctrl+Alt+N (or a click on the bar's `need you` count) focuses the next agent that needs you, in the lattice pulse's order:
  a sign-in first, then a question, then a grant, the longest unchanged first; a zoomed view stays zoomed, on it. Each
  saved standalone session gets a space as well, with its `session` tab. A standalone space closed in the app stays closed
  (`--show <label>` reopens it); this record (`$XDG_STATE_HOME/shoal/app-open.json`) is the app's own, apart
  from the 0.9.3 registry flags. Every host reads every shoal through the shared ledger, so a host plans only
  the shoals whose home it is (`--shoal ID` names one anyway); a role that runs on another host keeps its
  routed card in its home's space, as in 0.9.3.
- **Another host** is a herdr-core machine named after its route (`[hosts.<server>]`). The first
  `app-open --machine <server>` adds it (`hc machine add <route ssh> --name <server>`); the host must have
  herdr-core installed. The plan comes from that host (`shoal ui app-plan --json` over the route), and its
  cards run there, so they keep running when this machine sleeps or the app closes.
- **Re-running** adds only what is missing. Every terminal it starts carries the label
  `shoal.pane=<label>`; a labelled pane whose terminal came back as a bare shell (a lost terminal comes back
  as a shell in its last folder, and its command is never run again) is replaced. The two-minute `app-sync` does
  the same for a pane restored after a reboot or whose card died (its status labels still set), so cards come back
  by themselves; a card you left with `q` stays a shell until `app-open`. A pane running anything
  else, an attached client for example, is left as it is. One whose card is gone and that you took over (a command
  typed into the shell it came back as) keeps running untouched: its label moves to `shoal.pane.taken=<label>`, the
  card's old `shoal.*` status labels are cleared, and a fresh card opens beside it. Spaces of hidden or retired shoals are named, never
  closed.
- **A new role from the app.** The actions dialog's **New role**, on a shoal, starts a new session as one of its roles:
  a name (slug), a title, its start doc (a `plan_state.md` on the shoal's home host with `## Mission`, `## Current state`
  and a grants-needed section), a provider, and optionally a model, an effort, the host it runs on (a `[hosts.*]` route;
  empty is the home) and where its work comes from. It is `shoal add-role --spec <spec> --role=<slug,provider,title,doc,...>`
  run on the shoal's home host, previewed first; a name the shoal has, a comma in a field, or a start doc that is missing or
  would not brief is refused before the plan. The new session asks for its grants as any new role does. With **Write a
  starter doc**, the form also asks for guidance (its mission), first steps and the grants it needs, and the plan first
  runs `shoal doc-init <path>`, which writes a `plan_state.md` meeting the start-doc contract from them (never over an
  existing file; not committed), then `add-role`; both are previewed, and a failed doc stops the plan before the role. A
  starter doc is for a role on the home host only: a role on another host reads its doc from that host's checkout, so
  write and commit it first (its cost note says so for an existing doc too).
- **Adding a standalone seat in the app.** The actions dialog's **Add session** (any target, also on a machine's
  right-click in Spaces) lists the sessions a seat could be made of on every machine; the seat name, typed first, filters
  that list as it is typed (label, folder, harness or machine) (`ui act-plan sessions`, the 0.9.3
  picker's own discovery: Claude background sessions, opencode, Codex and Muse, with roles, old incarnations and internal
  runs left out), saves the one picked on its own machine (`ui pick --harness --id --label --save --no-open`, so its
  `/bg` following, model and effort work as for any seat; a name another seat holds is refused), and opens its space
  here (`ui app-open --show <label> --machine <its machine>`). By
  hand: `shoal ui pick --harness <h> --id <id> --label <name> --save` on the session's host, then `shoal ui app-open`
  where the app runs. `ui pick` opens a 0.9.3 card only from a 0.9.3 pane (or with `--open`); elsewhere it saves and
  says so, and `--dry-run` saves nothing.
- **Standalone state in the app.** A local standalone Claude card reports its session the same way (`app=<label>`,
  title `standalone/<label>`), read from `claude agents` as its lattice card is: `working` for a turn, `blocked` with
  `question` only while a prompt is open (a seat's reply ending reads `blocked` in the listing: that is your turn to
  type, so it reads `idle`, "your turn: waiting for a reply", and stays out of Needs you), else `idle` (`stopped` in the message when no
  worker runs). A turn the card saw running that has ended is a `done`, written once, as a role's. It keeps reporting
  through an attach, and writes its report again when the attached client clears it (see "Status while attached"), and while it reports it refreshes every 30 s instead
  of every 5 minutes. Every reporting card on a host shares one `claude agents` listing (`claude-rows.json` beside the
  status cache, at most 25 s old, refreshed under a lock by the first card to find it stale), which serves both a card's
  `/bg` check and its report. A remote, opencode, Muse or Codex seat's card reports nothing.
- **Role state in the app.** A role card in a herdr-core terminal prints its own OSC 7501 status on every
  refresh (`app=<role>`, which heads the pane, and title `<tag>/<role>`): `working` for a turn or a job wait (the message names the
  wait, such as `◷ local-ci`), `blocked` when the role needs you or cannot be woken, else `idle`. A blocked
  report carries `kind`: `auth` for `user-auth`, `question` for an open prompt, an ask (the message then leads with the
  asks themselves, `2 asks: <first>; <second> ·`, so the badge's question can be read, and labels the count, `shoal.asks`:
  a click on the badge opens the actions dialog's Asks on that role, every ask in full (read from the role's queue, not
  the status row's 160-character cut) with its answer set as one choice per button (grant, defer, delegate review and
  deny for a merge; grant, defer and deny otherwise; a click picks it, a second click clears it) and a note box for any
  text, which is how a headless role (a Codex or Muse turn, with no question tool) is answered, and a link to each issue
  or PR it names (a bare `#n` after "gate", "build", "run" and the like is a build number and links nothing). Answers
  are collected across the asks, the way a question tool collects every question's before one submit: the role's
  **Submit answers** sends them as one message with a wake, a line per answered ask
  (`re ask <n> [<id>] "<opening words>": <choice>. <note>`, the ask named by its id from the CLI and its opening words,
  so the role's `next` matches it to the ask it answers even after the role reorders or replaces its asks; either part
  alone when only one is given; unanswered asks are left out), planned as a dry run first so the message is
  read before Confirm, and the answers stay until it lands (Clear drops them). A role whose session holds an open
  question is still answered here: its watcher closes that question and resumes the turn with the answer, unless a
  client is attached (the line under the role's name says so, and the sent message's result says what becomes of it). **The dialog's size**: the platform
  sizes an overlay only when it opens and has no resize by drag, so the header's `⤢` cycles normal (640 by 600),
  large and full (shares of the window), reopening the dialog at the new size on the same target and verb with the
  answers kept; the size is remembered for the person (`shared("shoal-actions.size")`), and a dialog opened at another
  size (a card's right-click, `mod.open`, a row's badge) reopens itself at the remembered one, keeping the verb it was
  opened on (`resizeVerb`: that reopen comes before the roles list, and it dropped the verb, so the badge showed the
  plain actions list until 2026-11-24). A role row's badge opens that role's Asks whatever made it ask: ledger asks (a
  headless role's included), a question open in its session (the view says to attach to its card to answer there),
  words that wait on you, or a wait it recorded on you (`user-auth` and the like, with what it holds); only a role with
  none of these falls back to the dialog's actions, with a line saying so (`asksFallback`). The row's right-click
  still opens its asks when it has some) or a design or
  feedback wait, `permission` for a pending grant. When the role finishes an item (a `done` newer than any the card has
  seen) and rests, the card says `done` instead of `idle`, once: the app shows it as finished (silently: Sounds below), until
  you look, and anything the role does next, or an attach, replaces it. A card restarted by an upgrade (every card re-execs
  on new code) forgets a `done` not yet seen. The app then shows the role in its agents list, colors the
  space and tab dots, plays its sound (the needs-you one; a finished one is silent) and posts a toast that leads to the card. Leaving the card clears the
  report. The 0.9.3 feeder's socket push is not used here.
- **The pin.** The app downloads its own updates and then updates herdr-core, and no setting stops it.
  `app-start` runs it as the user unit `shoal-herdr-app` with `HERDR_RELEASES_URL` pointed at a dead loopback
  address, passing through only the display variables. Pins: app 0.1.13, core 0.1.18 (`ui_app.APP_PIN`,
  `CORE_PIN`); upgrade deliberately by rerunning the installer, lane host first. Herdr's own desktop entry starts
  the app without the pin held, so it can stage and apply an update by itself. `--write-desktop` replaces that entry (`herdr-app.desktop`, the name a
  desktop shell matches the window to, so it pins and groups; the original is kept as `.bak-shoal`) with one named
  Shoal that runs `app-start`, using the `shoal` icon. `app-install` puts that icon (a fish before the splash's
  seaweed lattice, `shoal/ui/assets/shoal.svg` with 64 and 256 px renders) into the user's `hicolor` theme, so every
  launcher with `Icon=shoal` shows it; a different icon already there is kept once as `.bak-shoal`.
- **The `shoal` layout.** `app-install` writes it from Herdr's `main` and switches to it (Ctrl+Shift+L cycles
  back): Agents lists role names in their shoal's color and the host each really runs on, a right-click on a machine in
  Spaces acts on that host's shoals, the top bar has **Home**, **Lattice**, **Board**, **Actions** and **Usage** beside Files and Changes
  (`shoal-tabs`, a patched copy of Herdr's `tabs`; the status bar shows them instead only when that copy is not in the
  layout), the status bar shows each provider's usage (bars and percentages; hovering a provider gives each window's
  time to its reset, such as `claude: 5h resets in 2h 14m, wk resets in 3d 4h`), and the window's mark (top left) is
  SHOAL and its icon (`shoal-chrome`, a patched copy of `workspace-chrome` that carries the icon file, since the app
  loads no picture through a link).
- **Usage in the status bar.** The bar asks every machine for `shoal ui usage --json` at once and keeps the best
  answer (most accounts read, then most fresh), drawing the first complete one as it lands; until a reading lands it
  says a pulsing `usage …`, never a cached value. A reading over an hour old (`USAGE_MAX_AGE_S`, twelve polls) is drawn
  as an empty bar and `…`, its hover saying how old it is; a reading only a little old (past the 15-minute stale mark)
  is dimmed. In the first two minutes a pass with no complete answer is tried again every 5 s: the other machines'
  connections are still coming up, and the first pass hears only this computer, whose cache no feed may refresh
  (measured 2026-11-26: the app's own computer held readings 6 and 8 days old, and the bar showed them while the other
  machines' answers were on their way). The `usage` dashboard reads the same way (its Refresh asks one machine at a
  time, since it reads the account APIs) and shows such an account's card as waiting.
- **The home space** (`SHOAL-home`, on this machine): the `shoal-home` mod when it is installed, else the terminal
  chooser, plus the global `lattice` tab, which reads a shoal home's feed when this host is no shoal's home, and the
  `usage` tab (the `shoal-usage` dashboard: a card per provider with each window's meter, reset and pace, the tightest
  meter first, and panels by shoal, role and standalone seat that fill in once per-session usage is recorded), and the
  `board` tab (below). The sidebar does not list it: the top bar's Home, Lattice, Board and Usage reach it, and for the
  same reason its tab strip shows only `home` (the `lattice`, `board` and `usage` tabs stay, out of the strip, and those
  buttons select them). The terminal a new space must start with
  (`launch`) is closed once Home stands in for it. In a Herdr app terminal, `shoal ui launch` opens the chosen host in
  the app instead of starting a 0.9.3 client.
- **The graph feed.** `app-install` writes and starts `shoal-graph-feed.service` (`shoal ui graph-feed`);
  lattice tabs then read its `graph.json` on their machine. Without it they run `shoal ui graph` every 15 s.
  Like `herdr-install --local`, it refuses to write from a git worktree. It also puts the shoal mods into the
  app's `mods/` folder: a link to this checkout, except a mod with pictures (an `assets/` folder), which is
  copied (marked `.shoal-copy`), because the app does not load a PNG through a linked mod folder. Rerun it
  after an upgrade; the doctor row says when a copy is stale. A folder there that shoal did not make is left
  alone.
- **Copy and paste.** The app copies with Ctrl+Shift+C (or a program's OSC 52), and pastes an image into a
  terminal on another machine by uploading it there and pasting its path, which Claude Code, Codex, opencode
  and Muse take as an attachment. `app-install` keeps the 0.9.3 keys in the app's `keys.json`: Ctrl+V pastes
  text, Ctrl+Shift+V pastes an image (the app ships them the other way round). Only an unbound key or the
  app's own default is changed. The kitty window wrapper and `shoal-paste-image` are not needed here.
- **Look.** `app-install` puts `shoal-ocean` into the app's `themes/` (white text on a black terminal, deep
  navy chrome, a cyan accent) and sets it in `settings.json` when no theme is set there (a one-time
  `settings.json.bak-shoal-theme` first); a theme you picked stays. Only one app should show a machine's
  spaces at a time: herdr-core sizes each terminal for one viewer, so a second window on the same spaces makes
  the cards wrap at its size.
- **Sounds.** Herdr plays `done` when an agent finishes while you are not looking at it and `blocked` when one needs
  you, and a running shoal finishes things all day (a role's card at each item, a standalone seat's after each reply,
  an attached client at each turn that ends unwatched). Herdr has no switch per kind, only files that replace its sounds, so `app-install` writes a
  silent MP3 to the app's `shoal-sounds/silent.mp3` and points `settings.json`'s `sounds.done` at it (a one-time
  `settings.json.bak-shoal-sounds` first; every other key, `sounds.enabled` and `sounds.blocked` included, is kept). A
  finished agent then makes no sound but still posts its toast (Herdr has no setting for the toast); one that needs you
  keeps Herdr's sound. A `done` file you chose stays. `shoal ui set app_done_sound herdr` and a rerun of `app-install`
  restore Herdr's own sound (`silent` is the default). A card writes the same report on every refresh, and Herdr keeps
  attention per tab by its state word (`view.json` `attention`: `{word, seen}`), so a role still needing you does not
  ding again on a refresh, nor when what it needs changes (a question, then a grant, stays `blocked`): it dings when
  its tab turns `blocked`.
- **Actions.** Ctrl+Alt+S (set by `app-install` when free) opens the `shoal-actions` dialog, as does a
  right-click in the lattice: on a role, on a shoal's circle (that shoal), or anywhere else (every shoal), or on a machine
  in Spaces (its shoals). **Handoff** first requests a restart-ready checkpoint if needed, then hands the role
  to a fresh session from its start doc, optionally on another provider or model, on its own host. The checkpoint
  toggle defaults on. It waits up to one hour for the role's own new queue checkpoint and completed turn, then
  uses the ordinary unforced handoff gate. A saved standalone seat has Handoff too: its current session refreshes
  the start doc and finishes its turn before replacement. Supply its start-doc path when none is saved.
  Turn off the checkpoint toggle to use an existing standalone checkpoint: its doc must validate, have no live
  job-folder references, and the source must already be idle.
  With the same provider, recorded model and effort stay the same; changing provider uses this host's default for that
  provider (`[provider_defaults.<provider>]` in `config/host.toml`, recorded on the role so a later handoff keeps it)
  unless explicit values are entered, and with none configured, the provider's own. Its own can be whatever its client
  last saved (2026-11-14: Codex keeps a model picked in its interactive client as its default, so a seat moved to Codex
  ran that one), and moving back to a provider gets the configured default again, not an earlier session's choice.
  A provider with two or more accounts on the host (`[accounts.<name>]`, `shoal accounts`) adds an **Account** choice
  (pick the provider to see it; with one account per provider nothing new shows): unpicked keeps the current account on
  the same provider, else the provider's default; picking one hands off to a fresh session on that account, same host,
  model and effort (`shoal handoff --account`). The dialog pre-selects the account with the most paced quota headroom
  (free share over the share of its window still to run; `[accounts] select`, `shoal accounts` shows every score) and
  says why under the choice; clear it to keep the current one. **New role** has the same choice, **Add session** lists each candidate
  under its account, and a second account's card shows its provider's icon with the account's badge (a subscript digit,
  written by the app sync under the app's `shoal-icons/`); the lattice inspector reads `claude-2 · <model>`.
  Moving a role whose session stopped on its provider's usage limit needs no checkpoint and no Force: it can never
  write one, so the handoff detects the stop, sends no checkpoint request, and hands the new session a reconciliation
  brief in its first prompt (the start doc's stamp, the predecessor's transcript and how many tool calls it made after
  that stamp), to read and record in a "Handoff reconciliation" section of its start doc before new work. The result
  shows the same lines. A stale preview cannot hand off a different session that took the seat meanwhile.
  The dialog streams progress; **Continue in background** closes it without cancelling the operation. Reopen
  Handoff on the same target to reconnect, including after completion. **Offline** and **Freeze** run the same way
  (an `offline off` across shoals wakes every role holding asks and lists every ask, which ran past the app's 30 s
  call limit on 2026-11-20): the dialog follows the log, then shows the whole result, who was woken and every ask with
  its hint. Reopened on the same shoal or scope, it follows a run still going, or shows once how one that ended in the
  last ten minutes went. A failed checkpoint or disappeared worker
  is reported in the retained log. Role handoffs follow the restart log until the new session is recorded.
  The form and the result say how long to expect, so the dialog can be closed rather than watched (measured
  2026-11-14): a standalone seat's new session starts about 2 s after Apply; a role's restart records the new session
  in about 40 s and proves its watcher within a minute; either card shows in the lattice within about 10 s after (the
  graph is written every 5 s, and the view polls every 4 s). A checkpoint first adds the wait for the current turn.
  Detach a held native client before Handoff so the checkpoint can run and the source session can be retired: a role
  whose Claude session has a client attached is refused, and says so. The form says which start doc the new session
  resumes from (a role's path, or the seat's saved one when none is typed), and its optional **Restart note** goes
  into the new session's first prompt after the start doc, for that handoff only. A standalone seat stopped at an
  open question can be handed off **Through an open question** (checkpoint-first only; the question is dropped).
  The verbs sit in labeled rows: Talk (Message, Asks), Work (Next, Queue, Offer, Withdraw, Fill), Seat (Handoff,
  Dismiss, New role, Add session, Remove), Shoal (Offline, Freeze) and Inspect (Status, Conditions, Upgrade preview).
  Remove takes a role out for good (`shoal remove-role`): it stops the session and watchers, removes the role from the shoal and its
  incarnations from the harness's list, and keeps its ledger history. A pinned card on a parked role or an outside writer offers
  Handoff for a saved standalone seat, Remove (for a writer, `shoal remove-session` on the saved standalone session
  of that name) and Hide, which only takes it off the picture. Handoff opens its checkpoint-first Actions flow.
  It does what the 0.9.3 palette did, with buttons, through two verbs:

  ```bash
  shoal ui act-plan VERB --shoal ID [--role SLUG] [--text T] [--item I] [--note N] [--wake]
                    [--state on|off] [--until ISO] [--no-wake] [--json]
  shoal ui act-apply --token T [--json]
  ```

  For a write verb (`msg`, `offer`, `withdraw`, `offline`, `freeze`, `handoff`, `remove-role`, `remove-session`, `fill`,
  and `queue-set`, the queue manager's batch), `act-plan`
  builds the argv the palette builds, runs its `--dry-run`, names the wake cost, and keeps the argv under a one-time token (mode
  0600 in the runtime dir, ten minutes): `{"kind": "shoal-act-plan", "command", "preview", "cost", "confirm", "token",
  "progress", "expires_s"}`. `--no-preview` skips the dry run (`preview` is empty), so a verb that would refuse reports it from the
  apply; across shoals each then runs and the output says which refused. `act-apply` runs exactly that argv once, logs it to
  `ui-actions.jsonl`, forgets the token, and writes the output to the `progress` file as it runs, for a caller to follow, since
  the app's `exec` returns only at the end (`{"kind": "shoal-act-result", "rc", "command", "output"}`). It returns when the verb
  ends, or kills it at its timeout, even if a detached child still holds the pipe. The dialog decides from its `Preview first`
  toggle: on, it shows the plan and waits for a confirm; off, the click was the go, and across shoals only the button
  (`Run on every shoal`) runs it, not Enter. The toggle starts on for `freeze`, `handoff`, `remove-role` and `remove-session`.
  `confirm` remains for other callers (false for `offline` on one shoal). Views store nothing:
  `next`, `status`, `conditions`, `asks` (also as data), `queue` (as data, as `ui queue --json`) and `upgrade` (a preview) return
  `{"kind": "shoal-act-view", "text"}`, and `roles` lists the shoals and their roles with a session. An
  error is `{"kind": "shoal-act-error", "error"}` with exit 2, and so is a write whose own `--dry-run` refuses
  it (no token is handed out). A preview syncs the ledger clone the way the
  verb itself would, as the palette's preview does.
- **`launch --list --json`** prints `{"v": 1, "kind": "shoal-launch-list", "last", "hosts": [{"name", "local",
  "server", "route", "reachable", "hint", "machine", "summary"}]}`. With `--summary`, each reachable host's
  shoals (`id`, `tag`, `roles`, `working`, `waiting`, `needs_you`), its total needs-you and `pin_ok` (its
  herdr-core at the pin), from its `ui graph`, cached 30 s. `here` is what is particular to that host: the roles
  that run on it in any shoal (`shoal`, `tag`, `slug`, `ecosystem`, `account`, `state`, `event`, `needs_you`), its
  standalone seats (`label`, `harness`, `account`, `state`), their needs-you, and `ledger_age_s` (how long ago its
  ledger clone last fetched). The app's home cards show `here`: every host reads the same shoals from its own clone,
  so the shoal-wide totals repeat on every card, and a host that runs no role (a laptop) never pulls its clone.
  `fresh` is that host's graph doc's freshness block, as it is (see the graph feed's Freshness): a card past it greys
  its lines and says `updating`.
- **Doctor.** `shoal ui doctor` adds informational `app:` rows (app and core at the pin, mods installed from this
  checkout, machines, the feed unit); `ui app-install --check` prints the same rows and fails on them.

## Configuration and install

Herdr splits its configuration by machine:

- **Presentation lives on the viewing machine:** theme and sidebar rows.
- **Execution lives on the server:** restore policy, keys, plugins, and the tab-bar status line
  (`tab_bar_right`). A `herdr --remote` client draws the server's status line, not its own
  (measured in the laptop pilot), so the server block carries it. Herdr clips that command segment near 80
  columns (measured 2026-10-19), so `shoal ui line` fits itself to the `line_width` setting (default 80; `shoal ui set
  line_width 120` if your bar shows more): the usage quick view stays in full and quiet shoals drop out first, then the
  usage turns compact (`cx 4/23/- cl 10/87/-`). The launcher attaches with
  `--remote-keybindings server`, so the server's keys reach the client.

**Setting up a host.** Every host first installs the pinned Herdr (`shoal ui herdr-get`); `shoal ui doctor`
then checks it. After that, the steps depend on what the host is for:

| Host | One-time setup | Every day |
|---|---|---|
| **Lane host** (runs roles; serves the view) | `shoal ui herdr-install` (server block in `config.toml`, plugin, the `shoal-herdr` and `shoal-ui-feed` units, layouts) | `shoal ui launch` in a terminal there (local: the running server, a client with the client rows), or from another host |
| **Viewing machine** (draws another host's view) | `shoal ui herdr-config --client --write` (sidebar rows in `config.toml`), and a `[hosts.<server>]` route per lane host in the host config | `shoal ui launch --server <server>`, or pick it in the chooser |
| **Local-view host** (its own terminal also serves; a laptop that runs roles) | nothing extra: the first `shoal ui launch` (local) runs `herdr-install --local` | `shoal ui launch` (local), with routes to reach other hosts' roles |

A host can be more than one of these. A laptop is a viewing machine for the lane host and a local-view host for its
own roles, and the chooser picks per launch. Routes are private host configuration (`config/host.toml`,
gitignored), and the ssh key is the only authority.

```bash
shoal ui herdr-config --server [--write]   # lane host: restore off, version checks off, keys
shoal ui herdr-config --client [--write]   # viewing machine: sidebar rows, the `ui line` segment
shoal ui herdr-install [--dry-run]         # lane host: server block, plugin link, units, open
shoal ui herdr-config --local [--write]    # local view: both blocks merged, in its own file
shoal ui herdr-install --local [--check]   # local view: that file, units carrying it, plugin, open
shoal ui herdr-get [--check]               # the pinned Herdr release, checksum-verified (no sudo)
shoal ui launch [--server S|local]         # open the view from this terminal (the host chooser, below)
shoal ui doctor                            # preflight (also: herdr at the pin)
```

**The local view** is for a host whose own terminal client also serves the view (a laptop that also runs
roles). The server and client blocks both define `[ui]`, so they cannot share one managed block. The local
server and its client read a file of their own instead, `~/.config/herdr/shoal-local.toml`
(`SHOAL_LOCAL_HERDR_CONF`), through `HERDR_CONFIG_PATH`. That file holds the user's own `config.toml` settings
plus one merged block, in which the client's `[ui]` keys win and the server's status line stays. `config.toml`
stays the remote client's file. `herdr-install --local` writes that file and both user units with
`Environment=HERDR_CONFIG_PATH=...`, links the plugin, starts the server and the feeder (restarting what a
change touched), then runs `ui open`. `--check` runs `ui doctor` against the local file. Run it from the checkout
the live install uses: the feeder unit and the status line name the shoal wrapper of the code that writes them, so
`herdr-install --local` refuses to write from a git worktree (`--dry-run` and `--check` still work there), and
`ui doctor`'s plugin line names the path the plugin is really linked from.

**Launching the view** (`shoal ui launch [--server SERVER|local] [--no-open] [--no-choose] [--shoal ID]`).
- **The chooser.** A full-screen start screen with five transparent colorful creatures (three fish, a seahorse and a blowfish) around the SHOAL
  logo above the menu. A Kitty graphics PNG capability probe enables images behind the text, including
  over SSH when the complete terminal path supports it. The creatures render at twice their base size
  when the window has room (93 by 33 for two hosts), else at 1.5x, else at base size. The fish are uploaded once and repositioned
  on resize. Unsupported paths use three colored ASCII fish, one reversed, only when all three fit
  intact: at least 121 columns by 40 rows for two hosts. Smaller windows show the logo/menu without fish.
  Set `SHOAL_SPLASH_ART=ascii` to inspect the fallback, or `SHOAL_SPLASH_ART=none` to disable art;
  `auto` is the default. Images require no external helper.
  The whole composition re-centers within a tick on resize using measured tty dimensions, even when
  inherited `COLUMNS` and `LINES` are stale. Explicit row positioning and clearing prevent stale text
  or line wrapping from garbling the menu. For two hosts, the compact minimum is 72 columns by 23 rows;
  below that, a bounded guard asks for room and requests terminal enlargement once per small-window
  episode. A hard window minimum depends on the terminal and window manager.
  It asks which host serves the view, this host (`local`) first, then every host with an ssh route,
  each marked when its cached probe fails. Arrow keys or `j`/`k` move the selection (the last choice
  that worked starts highlighted, `$XDG_STATE_HOME/shoal/launch.last`; a host that just failed is
  never remembered), Enter chooses, a digit jumps straight to its host, `t` types an ssh target,
  `q` quits. Typing during graphics negotiation is preserved. Off a terminal it falls back to a
  numbered prompt. `--server` never asks, and neither does a launch without a terminal (it takes
  the last choice, else local).
- **No configuration beyond the routes,** by design until Herdr machines are in use.
- **local:**
  - a host provisioned for the local view keeps its file;
  - a lane host is started as it is, and its terminal client reads the merged local file (the client rows too;
    the server never reads it);
  - a host with neither is provisioned with `herdr-install --local`;
  - then `ui open`, and a local client.
- **remote:** over the route, the host's `shoal-herdr` unit is started and its `ui open` run, then
  `herdr --remote <route> --session shoal --remote-keybindings server`. The route's MagicDNS name is resolved
  through tailscale like every route, so the view works off the home network too.
- **An unreachable host** fails at once, with the `doctor` hint, and returns to the chooser, in every mode (`--no-open` probes too). It never attaches a
  client that hangs.
- **Width and guard:** a percent sidebar width is resolved from the window just before the client starts. The
  command refuses to run inside a Herdr pane.

**The Herdr pin.** Every shoal host runs one pinned Herdr release (`PIN` in `shoal/ui/herdr_pin.py`, equal to
the plugin's `min_herdr_version`), never `herdr update`. Herdr ships often, with breaking socket changes, and
the adapter checks the methods it uses. `shoal ui herdr-get` installs the pin under `~/.local/share/herdr/<version>/`
with a `~/.local/bin/herdr` symlink, after verifying the release asset's sha256 digest from the GitHub release
API. Bump the pin deliberately: lane host first, then `ui doctor`.

`--write` merges a delimited managed block into `~/.config/herdr/config.toml`, or into the file
`HERDR_CONFIG_PATH` names (Herdr's own override; `--path` wins over both). A backup is kept, and
the merge refuses when the file already defines a table the block defines. `ui doctor` reads the same
file, and counts a Claude integration only when `~/.claude/settings.json` has a `hooks` section that
mentions Herdr (a permission rule or plugin name that contains "herdr" is not one).

**Hazards the configuration closes:**

- **Restore.** Herdr resumes agents on a cold restore by default, from session references that its
  own integrations record. On a lane host that would fork a Claude role or start a second opencode
  server. The server block sets `[session] resume_agents_on_restore = false`.
- **Integrations.** Herdr's Claude and opencode integrations must not be installed on a lane host.
  `ui doctor` checks both.
- **The socket.** Herdr's socket has no authentication beyond file mode. Lanes are launched outside
  Herdr, so they carry no `HERDR_SOCKET_PATH`. The protocol skill forbids driving another session's
  terminal.

**Pin Herdr.** It ships often and breaks its socket API between minor versions. `ui doctor` checks
that every method the adapter calls is still in `herdr api schema`.
