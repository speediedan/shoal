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
- `context-large`: the session's context is past its `context_restart_k` limit (informational; the row's
  `context_k` holds the size in thousands of tokens).
- `dormant`: a Claude session with no worker (the background service retired it idle) behind a live
  detached watcher, with `health.direct_wake` on. The watcher resumes it when work arrives, so it is
  not blocking and shows as `DORMANT`.
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
- **The relay is read where it runs.** The wake relay's watch process is visible only on its own host, so a
  relay role on another host reports `relay_alive` in that host's rows (a live field, pulled like the rest) and
  the shoal's `relay` is `{"alive": true|false}` from it. When nothing can say (no route, or a failed pull with
  nothing cached) it is `{"alive": null}`: the status text and the sidebar show `relay ?` (the line shows nothing), never `DEAD`, and the
  `relay-dead` condition is raised only by the host that runs the relay.
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
sessions and OpenChamber sessions that are not shoal roles, and open one in its own workspace. The
picker leaves out shoal's own sessions: matcher runs (`shoal-matcher-*`) and every incarnation of a
role (`<slug>-r<N>`, current or retired). Saved ones (`~/.config/shoal/ui-standalone.json`) come back on `ui open`, except one that was closed in the viewer:
once a saved session has had a workspace, a later `ui open` that finds it gone (while Herdr still holds other
labelled panes, so not a fresh server) marks it `dismissed` and leaves it closed across viewer restarts.
`shoal ui open --show <label>` brings it back, `shoal ui pick --save` re-saves it, `shoal ui forget` drops it.

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
- **The chooser.** It asks which host serves the view: this host (`local`) first, then every host with an ssh
  route, each marked when its cached probe fails. Enter takes the last choice (`$XDG_STATE_HOME/shoal/launch.last`),
  and a typed ssh target also works. `--server` never asks, and neither does a launch without a terminal (it takes
  the last choice, else local).
- **No configuration beyond the routes,** by design until Herdr machines are in use.
- **local:**
  - a host provisioned for the local view keeps its file;
  - a lane host is started as it is;
  - a host with neither is provisioned with `herdr-install --local`;
  - then `ui open`, and a local client.
- **remote:** over the route, the host's `shoal-herdr` unit is started and its `ui open` run, then
  `herdr --remote <route> --session shoal --remote-keybindings server`. The route's MagicDNS name is resolved
  through tailscale like every route, so the view works off the home network too.
- **An unreachable host** fails at once, with the `doctor` hint, and returns to the chooser. It never attaches a
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
