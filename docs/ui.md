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
     "posture": "active|wait|idle|offline|parked", "sub": "local-ci", "posture_age_s": 600,
     "wait": {"sub": "...", "target": "...", "holder": "...", "rescan_utc": "..."},
     "active": "<repo>#<n>:fix",
     "next": [{"item": "<repo>#<m>:review", "blocker": "<role>:<repo>#<k>", "stale": true}],
     "blocker_stale": true, "offers_open": 0, "mail_since_boundary": 1, "asks": ["..."],
     "watcher": "alive|dead|none", "session": "working|blocked|idle|dead|unknown|parked",
     "last_write_age_s": 900, "heartbeat_age_s": 60,
     "can_progress": {"ok": false, "reasons": ["blocked-on-question"]}, "needs_you": true,
     "view": ["shoal", "view", "<id>", "<role>"]}]}]}
```

**Sources.** Each field is derived once, from the code that already owns it:

- Posture, items, blockers, offers and mail come from `ledger.View`.
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
- `context-large`: the session's context is past its `context_restart_k` limit (informational; the row's
  `context_k` holds the size in thousands of tokens).
- `dormant`: a Claude session with no worker (the background service retired it idle) behind a live
  detached watcher. The watcher resumes it directly when work arrives, so it is not blocking and shows
  as `DORMANT`.
- `blocked-on-question`: the session waits on a pending question to the maintainer.
- `watcher-dead`: the session is between turns with no `ledger-watch`.
- `blocker-role-cannot-take`: a next item is blocked on a role that cannot take it.
- `parked-work`: the role sleeps while a next item has no current blocker.
- `waiting-on-you`: the role has recorded asks, or a user wait.

`parked`, `dormant` and `context-large` are informational. `ok` is false for any other reason except `waiting-on-you`.

`watcher-dead` and `parked-work` come from the health quorum's own predicates
(`health.watcher_dead`, `health.parked_free`). Blocker checks there run with `live=False` (no
provider calls), so a provider-checkable blocker (`upstream:`, `lease:`) that resolved minutes ago
still reads as current until the quorum's next run marks it stale. This is deliberate: never turn
provider calls on in the 20-second feed. `--conditions` (the quorum's own `evaluate()`, live, cached
five minutes) is the up-to-date view.

## The graph feed: `shoal ui graph`

```bash
shoal ui graph [--shoal <id> ...] [--json] [--since <cursor>] [--events N] [--window-rate S]
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
              "posture": "wait", "sub": "local-ci", "session": "working", "since_s": 900,
              "needs_you": false, "reasons": [], "shoal_offline": false, "restart_ready": false,
              "job_host": "<pool host>",
              "job": {"unit": "<unit>", "host": "<pool host>", "placed": true, "granted": true,
                      "started_age_s": 840}},
    "work": {"active": {"item": "<repo>#103:fix", "url": "https://github.com/<owner>/<repo>/issues/103"},
             "next": [{"item": "<repo>#104:review", "url": "...", "blocker": "<role>:<repo>#103", "stale": false}],
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

- `role`: one per spec role. Its row (posture, sub, session, `since_s` from `posture_age_s`,
  `needs_you`, reasons, active and next items, asks, offers) is the `ui status` row. Every role node
  carries `host`: the role's server, else the spec's home, else this host.
- `user` (the maintainer, one for all shoals): open asks summed, and `grants_pending` summed from the
  status rows' `grants_pending` lists (0 while no row carries one; grant events are not folded).
- `system` (`<id>:shoal`, the `shoal` writer), with the shoal's home as `host`. The retired wake
  relay's writer folds into it, so its historical traffic stays counted without a node of its own.
- `external` (`ext:<slug>`): an outside seat, from an `ext-<slug>.jsonl` writer file or an `ext:<slug>`
  target. `alias_of` names `<other-shoal>:<slug>` when that slug is a role in another discovered shoal;
  the seat then takes that role's state and host.
- `writer`: any other edge or event endpoint (a removed role, a tool's own writer file), state unknown.

`<id>:*` (a broadcast target) is not a node.

**Role state.** `state.class`, `state.event` and `state.group` are `role_time.classify` over a
snapshot built by `role_time_adapters.build_snapshots`: the ledger part from the View, the native part
only from the role-time cache files (`role-time-native.json` in the shoal's state directory, plus the
`role-time-native-<host>.json` copies `role-time observe` keeps there). `event` is the raw role-time
event (`turn`, `local-ci`, `remote-ci`, `lease`, `user-auth`, `user-ask`, `user-prompt`,
`user-design`, `assignment`, `sources`, `upstream`, `session-blocked`, `self`, `hold`, `handover`,
`refill` and the rest of the vocabulary), passed through as it is, so a renderer can key encodings
off it; `group` is `WAIT_GROUP` for waits, else empty; `quality` is the classifier's quality flags.
`state.source` is `role-time` for that reading. A role with no native observation (no cache, or a
background role) gets a best-effort reading of its status row in the same vocabulary, marked
`source: posture`. As in role-time accounting, a source is believed only within its budget (the
ledger half only while the clone's last fetch is fresh). Where accounting would read
`unknown:stale-source` or `unknown:missing-source`, the graph falls back to the posture reading
instead, marks it `source: posture`, and adds `role-time:<event>` to `quality`, so a host whose clone
stopped fetching still shows what its roles last declared.

For `wait:local-ci`, `wait:remote-ci` and `wait:lease`, `state.job` describes the job a `job:<name>`
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

`n_rate`, `n_1h` and `n_24h` count events within the rate window (`--window-rate`, default 300 s),
an hour and a day. `rate_per_h` is an exponentially weighted rate with a one-hour half-life
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

**Hosts** are this host, every role's host, every shoal's home and every job host, with `home` true
for this host and per-host role aggregates: `roles`, `working`, `waiting`, `needs_you` and `msgs_1h`
(messages written by the roles there in the last hour).

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

The overview always displays the meter block above the role tables: a boxed
table with one row per provider and its 5h, week and month windows side by side.
Each reported window is a short bar, its used percentage and its reset date/time
in the host's configured timezone (`SHOAL_TZ` or `timezone` in host.toml), and
the last column says how old the reading is. A reached limit, a fallback reader
or a read error is named on a line under the table. The bars shrink (8, 5, then 3
cells) to fit the terminal, and a terminal too narrow for the table gets one text
line per provider instead. The status line (the quick view in the tab bar)
shows each provider's used percent for 5h, week and month, for example
`codex: 4%/23%/- | claude: 10%/87%/- | opencode: 4%/10%/13%`, with `-` for a window the provider does not report and no reset times. Use `shoal ui usage` for the
same snapshot outside Herdr. `--providers` temporarily chooses a comma-separated
list; an empty `usage_providers` setting disables background collection.

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
- **Stub mode.** `--stub` shows a role card that refreshes every 15 seconds. It attaches on Enter and
  comes back to the card on detach.
- **Detaching.** Inside an attached Claude session, Ctrl+Z detaches (`claude attach` exits 0); in an
  opencode session, Ctrl+C quits the client. Either way the session keeps running and the card comes
  back (both measured under a pty, 2026-10-12). Herdr's prefix keys do not detach: they go to Herdr,
  not to the attached client. `/bg` then `/exit` also returns to the card in a Claude session (seen
  by the maintainer); `/exit` on its own was not tested, so prefer Ctrl+Z.

## The Herdr adapter

**Layout** (`shoal ui open`, idempotent):

- **One workspace per shoal**, named with the shoal's short tag: the id without its
  `-YYYYMMDD-HHMMSS` mint stamp (`it-mvp`), or the full id when two live (non-retired) shoals share a
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

**Feed** (`shoal ui feed`, the `shoal-ui-feed` user unit, every 20 s):

- **Cards for other hosts' roles need a route.** A session resolves only through its own host's harness, so
  a role on another host is attached by running that host's `shoal view` over ssh. A role whose host has a
  route in this host's config (`[hosts.<server>] ssh = "<user>@<name>"`, `routes.py`) gets a card like a
  local one; Enter attaches over ssh. A Claude attach is exclusive, so an attach that fails within 15 s says
  why (host unreachable, connection ended, or attached elsewhere) and retries every 30 s instead of taking the session over
  (`q` gives up, `r` retries now). A role whose host has no route has no card: it stays in the overview table
  with its host, answered by mail.
- **Live state of routed remote roles.** Harness liveness and sidecars are readable only on the role's host,
  so the read model pulls that host's own rows (`shoal ui status --shoal <id> --rows-only --json`, which has
  no usage and never pulls further hosts) over the route, cached 30 s, and takes their live fields
  (`session`, `watcher`, `can_progress`, `needs_you`, `context_k`, `heartbeat_age_s`). Each merged row
  carries `remote_status` (`ok`, `age_s`, `stale`). A failed pull keeps the last good rows for up to 10 min,
  marked stale, and the card says so. Without a route a remote row reads `session: remote`.
- **Role panes:** each tick reports `pane.report_agent` (source `shoal`, agent = role slug, state
  `blocked` when the role needs you, `working` while the session is mid-turn, otherwise `idle`) and
  `pane.report_metadata` tokens. A pane whose foreground is a bare shell (a card laid out while its
  role was parked, or a cold restore) gets the tokens only: Herdr clears an agent report from such a
  pane within a fraction of a second, which made the role flash into the agents list every tick. The
  verbose feeder names those cards; `shoal ui open --reapply` brings them back. The tokens are `role`, `shoal` (the short tag), `host` (multi-host shoals only), `mode`, `posture`,
  `item`, `next`, `wait`, `asks`, `offers`, `watch`, `age`, `why` and `harness`. The agents rows
  show the role name, then the `mode` glyph, then the shoal tag dim; Herdr's default `agent_panel_sort = "spaces"`
  already groups agents by workspace.
- **What the icons mean** (`shoal ui legend`, or the palette's `legend` entry). Herdr draws the state icon from the
  state shoal reports: half-moon yellow is `working`, hollow circle green is `idle`, red x is `blocked`, a grey dot is
  nothing reported. `working` covers two different things, so the `mode` token says which: `▶` a turn is running,
  `◷` the role waits on a job, CI build or lease running for it (by design it reads working, so a lane with a job in
  flight does not look stalled). No glyph means asleep, idle or needing you. The glyphs live in the client's sidebar
  rows: after an upgrade re-run `shoal ui herdr-config --client --write` (the laptop's `shoal-herdr-setup.sh`) and
  relaunch. **Standalone seats** get the same tokens (`posture` is `working`, `attached` or `idle`). For a Muse or
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
- **Role cards** are a field and value table (posture, session, watcher, last write, active, next,
  wait, asks, offers, attention), fitted to the pane.
- **Status layout.** `ui status` (and the status popup and dashboard) prints one ASCII table per
  shoal fitted to the terminal width: role, posture, session, active, next, last write, attention.
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
shoal ui act-plan offline|freeze --scope all|host:NAME --state on|off [...]  # the same across shoals, always confirmed
shoal ui app-sync                                  # roles tabs follow the roles, everywhere; nothing else changes
shoal ui app-lattice                               # show this space's lattice, or the global one
shoal ui app-home                                  # show the SHOAL-home space on this machine
shoal ui act-apply --token T [--json]              # run a planned action once
```

- **Spaces.** `app-open` gives each shoal a space named with its short tag, on its home host (the spec's
  `home`; a shoal with none counts as every host's), with the tabs the 0.9.3 adapter uses: `overview` (the dashboard; its `l` shows the ledger tail in
  place, and `overview_ledger: true` in `ui.json` keeps a tail pane, while with it off the sync closes one), `roles` (four cards a tab
  in a 2x2 grid, two or three stacked, more roles in `roles 2` and on) and `lattice` (the `lattice-view` mod on that
  shoal, reading the feed of the space's machine). `shoal ui app-sync`, run every two minutes by the `shoal-status` bar,
  lays roles tabs out again when roles are added or removed, unless a card is in use, and leaves anything you closed
  closed. `shoal ui app-lattice` (the bar's Lattice button, Ctrl+Alt+L) brings a closed lattice tab back, and
  `shoal ui app-home` (the bar's Home button) goes to the `SHOAL-home` space from anywhere, at once when its tabs are current. Each
  saved standalone session gets a space as well. A standalone space closed in the app stays closed
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
  as a shell in its last folder, and its command is never run again) is replaced. A pane running anything
  else, an attached client for example, is left as it is. Spaces of hidden or retired shoals are named, never
  closed.
- **Role state in the app.** A role card in a herdr-core terminal prints its own OSC 7501 status on every
  refresh (`app=<role>`, which heads the pane, and title `<tag>/<role>`): `working` for a turn or a job wait (the message names the
  wait, such as `◷ local-ci`), `blocked` when the role needs you or cannot be woken, else `idle`. A blocked
  report carries `kind`: `auth` for `user-auth`, `question` for an open prompt, an ask or a design or
  feedback wait, `permission` for a pending grant. The app then shows the role in its agents list, colors the
  space and tab dots, plays its sound and posts a toast that leads to the card. Leaving the card clears the
  report. The 0.9.3 feeder's socket push is not used here.
- **The pin.** The app downloads its own updates and then updates herdr-core, and no setting stops it.
  `app-start` runs it as the user unit `shoal-herdr-app` with `HERDR_RELEASES_URL` pointed at a dead loopback
  address, passing through only the display variables. Pins: app 0.1.13, core 0.1.18 (`ui_app.APP_PIN`,
  `CORE_PIN`); upgrade deliberately by rerunning the installer, lane host first. Herdr's own desktop entry starts
  the app without the pin held, so it can stage and apply an update by itself. `--write-desktop` replaces that entry (`herdr-app.desktop`, the name a
  desktop shell matches the window to, so it pins and groups; the original is kept as `.bak-shoal`) with one named
  Shoal that runs `app-start`, using the `shoal` icon where the theme has one.
- **The `shoal` layout.** `app-install` writes it from Herdr's `main` and switches to it (Ctrl+Shift+L cycles
  back): Agents lists role names in their shoal's color and the host each really runs on, a right-click on a machine in
  Spaces acts on that host's shoals, and the status bar shows each provider's usage.
- **The home space** (`SHOAL-home`, on this machine): the `shoal-home` mod when it is installed, else the terminal
  chooser, plus the global `lattice` tab, which reads a shoal home's feed when this host is no shoal's home. In a Herdr app terminal, `shoal ui launch` opens the chosen host in
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
- **Actions.** Ctrl+Alt+S (set by `app-install` when free) opens the `shoal-actions` dialog, as does a
  right-click in the lattice: on a role, on a shoal's circle (that shoal), or anywhere else (every shoal), or on a machine
  in Spaces (its shoals). Hand off restarts a role in place from its start doc (`shoal handoff`), optionally on another
  provider or model, on the role's own host, and refuses a role that has not said it is restart-ready unless forced.
  Remove takes a role out for good (`shoal remove-role`): it stops the session and watchers, removes the role from the shoal and its
  incarnations from the harness's list, and keeps its ledger history. A pinned card on a parked role or an outside writer offers
  Remove (for a writer, `shoal remove-session` on the saved standalone session of that name) and Hide, which only takes it off the picture. It does what the 0.9.3 palette did, with buttons, through two verbs:

  ```bash
  shoal ui act-plan VERB --shoal ID [--role SLUG] [--text T] [--item I] [--note N] [--wake]
                    [--state on|off] [--until ISO] [--no-wake] [--json]
  shoal ui act-apply --token T [--json]
  ```

  For a write verb (`msg`, `offer`, `withdraw`, `offline`, `freeze`), `act-plan` builds the argv the palette
  builds, runs its `--dry-run`, names the wake cost, and keeps the argv under a one-time token (mode 0600 in
  the runtime dir, ten minutes): `{"kind": "shoal-act-plan", "command", "preview", "cost", "confirm",
  "token", "expires_s"}`. `act-apply` runs exactly that argv once, logs it to `ui-actions.jsonl` and forgets
  the token (`{"kind": "shoal-act-result", "rc", "command", "output"}`); the caller can confirm only a plan it
  was shown. `offline` carries `confirm: false` (the palette runs it without a confirm). Views store nothing:
  `next`, `status`, `conditions`, `asks` (also as data) and `upgrade` (a preview) return
  `{"kind": "shoal-act-view", "text"}`, and `roles` lists the shoals and their roles with a session. An
  error is `{"kind": "shoal-act-error", "error"}` with exit 2, and so is a write whose own `--dry-run` refuses
  it (no token is handed out). A preview syncs the ledger clone the way the
  verb itself would, as the palette's preview does.
- **`launch --list --json`** prints `{"v": 1, "kind": "shoal-launch-list", "last", "hosts": [{"name", "local",
  "server", "route", "reachable", "hint", "machine", "summary"}]}`. With `--summary`, each reachable host's
  shoals (`id`, `tag`, `roles`, `working`, `waiting`, `needs_you`), its total needs-you and `pin_ok` (its
  herdr-core at the pin), from its `ui graph`, cached 30 s.
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
