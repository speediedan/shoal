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
   - The UI never resumes a session (a resume of a running Claude session forks it), never starts an
     opencode server, and never restarts a role.
3. **Every write is a shoal verb.** Actions run `shoal msg`, `offer`, `withdraw`, `offline` or
   `freeze` after a `--dry-run` preview and an explicit confirm, and are logged to
   `<data_root>/ui-actions.jsonl`. The adapter never types into a pane.

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
   "relay": {"alive": true}, "pages_24h": 0, "last_page": null,
   "asks_open": 0, "needs_you": 1, "stalled": 1, "violations": 0, "contested": 0,
   "tooling": {"sha": "<12>", "head": "<12>", "current": true}, "features": ["..."],
   "conditions": {"source": "health-dry-run", "ts": "<utc>", "age_s": 40,
                  "open": [{"slug": "<role>", "condition": "parked-work", "since": "<utc>", "detail": "..."}]},
   "roles": [{
     "slug": "<role>", "tag": "<role>-r3", "harness": "claude", "model": "<model>", "fill": "self",
     "server": "", "aux": false, "relay": false, "session_id": "<id>",
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
- `blocked-on-question`: the session waits on a pending question to the maintainer.
- `watcher-dead`: the session is between turns with no `ledger-watch`.
- `blocker-role-cannot-take`: a next item is blocked on a role that cannot take it.
- `parked-work`: the role sleeps while a next item has no current blocker.
- `waiting-on-you`: the role has recorded asks, or a user wait.

`parked` is informational. `ok` is false for any reason other than `waiting-on-you` and `parked`.

`watcher-dead` and `parked-work` come from the health quorum's own predicates
(`health.watcher_dead`, `health.parked_free`). Blocker checks there run with `live=False` (no
provider calls), so a provider-checkable blocker (`upstream:`, `lease:`) that resolved minutes ago
still reads as current until the quorum's next run marks it stale. This is deliberate: never turn
provider calls on in the 20-second feed. `--conditions` (the quorum's own `evaluate()`, live, cached
five minutes) is the up-to-date view.

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

## The Herdr adapter

**Layout** (`shoal ui open`, idempotent):

- **One workspace per shoal**, named `shoal:<id>`, with two tabs:
  - **`overview`:** the `ui dash` status pane and a read-only `ui tail` of the ledger.
  - **`roles`:** one role card per role that has a session, each pane labelled `<id>/<role>`.
- **One `standalone` workspace**, with a tab per saved non-shoal session.
- **Re-running.** Gaps are filled by re-applying a whole tab with `layout.apply` (cards hold no
  state). This covers a new role, and every pane coming back as a bare shell after a Herdr cold
  restore. It never happens while an operator is attached to a role in that tab.

**Feed** (`shoal ui feed`, the `shoal-ui-feed` user unit, every 20 s):

- **Role panes:** each tick reports `pane.report_agent` (source `shoal`, agent = role slug, state
  `blocked` when the role needs you, `working` while the session is mid-turn, otherwise `idle`) and
  `pane.report_metadata` tokens. The tokens are `role`, `posture`, `item`, `next`, `wait`, `asks`,
  `offers`, `watch`, `age`, `why` and `harness`.
- **Workspaces:** `workspace.report_metadata` tokens `shoal_mode`, `shoal_needs`, `shoal_stalled`,
  `shoal_asks`, `shoal_pages`, `shoal_relay`, `shoal_conditions` and `shoal_tooling`.
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
- **Wake costs are named** before confirming: a `--wake`, an offer to an idle role, a mode toggle
  that wakes every role.

**Standalone sessions** (`shoal ui pick [--save]`, `shoal ui forget <id>`) list Claude background
sessions and OpenChamber sessions that are not shoal roles, and open one in the `standalone`
workspace. Saved ones (`~/.config/shoal/ui-standalone.json`) come back on every `ui open`.

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

## Configuration and install

Herdr splits its configuration by machine:

- **Presentation lives on the viewing machine:** theme, sidebar rows, status line layout.
- **Execution lives on the server:** restore policy, keys, plugins. The launcher attaches with
  `--remote-keybindings server`, so the server's keys reach the client.

```bash
shoal ui herdr-config --server [--write]   # lane host: restore off, version checks off, keys
shoal ui herdr-config --client [--write]   # viewing machine: sidebar rows, the `ui line` segment
shoal ui herdr-install [--dry-run]         # lane host: server block, plugin link, units, open
shoal ui doctor                            # lane host preflight
```

`--write` merges a delimited managed block into `~/.config/herdr/config.toml`. A backup is kept, and
the merge refuses when the file already defines a table the block defines.

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
