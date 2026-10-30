# Muse Code as a harness

Status: Phase 1 (see the design in the private admin repo, `project_admin/shoal/design/muse-code-support`).
Muse Code (`muse`, https://dev.meta.ai/docs/muse-code) runs a role as **one `muse exec` process per wake**. The session
is a durable id (a UUIDv7 shoal mints at launch); each turn is `muse exec --session-id <id> --json --yolo` and the
next wake continues the same session. There is no daemon: an idle role costs no process and no tokens.

## What differs from Claude and opencode

| | Claude | opencode | Muse |
|---|---|---|---|
| a turn | the live background session | the live server session | one `muse exec` process |
| who runs the watcher | the role (`--wake relay`) | the role (`--wake send:<id>`) | **shoal**, at launch (`--wake exec:<id>`) |
| wake | message through the relay | `openchamber session send` | a new turn (`muse exec`) |
| busy | `claude agents --json` tempo | `openchamber session status` | a per-session lock (`lock`) |
| attach | `claude attach` | the opencode TUI | live observer for roles; explicit native interaction only while idle |
| questions | prompted | prompted | none: `--yolo` withholds `request_user_input`; asks go through `queue --ask` |

## Interactive sessions (standalone)

Besides the exec-per-wake roles, a Muse session can be a plain interactive TUI in a Herdr card. This is the
mode for a person-driven session (the first ones are `infra-aux` and `infra-expansion`).

- `shoal muse-new <name> --cwd DIR [--model M] --pick` creates a durable, named, empty session through a
  short-lived `muse serve` (`session/start` plus `session/rename`) and saves a standalone card for it. A name is
  3-32 chars of lowercase letters, digits and single hyphens, unique per user; `all`, `self` and `current` are
  reserved. Make the name describe the seat: peers address it by that name.
- The card runs `muse resume <id>` with `--model` (default `muse-wonder-full-1.0`, override
  `SHOAL_MUSE_MODEL`), `--reasoning-effort high`, `--trust-workspace` and `--disable-sandbox`. The model and
  effort are per-run choices, not stored in the session, so they must be passed on every resume: `muse-new
  --model/--effort` saves the pair on the seat's registry entry (through `ui pick`), and the viewer re-reads
  it on every attach, so a seat created at `xhigh` resumes at `xhigh`. Approvals stay on (a person is
  at the keyboard). `SHOAL_MUSE_VIEW_ARGS` replaces the whole flag list.
- Standalone cards show the read-only live view while an exec turn runs, then can be reopened
  in native mode after that turn. A role card always opens the live observer, including between
  turns: leaving it open never holds automated wakes. It shows public output, tool names and
  turn completion without replaying prompts, tool results or reasoning records. `q`, Escape or
  Ctrl+C returns to the card. Enter in the card opens it again.
- In a role's observer, `i` explicitly requests native interaction. This acquires the same lock
  automated turns use before launching `muse resume`, so a concurrent wake cannot race the
  attach. A busy session stays in the observer. Native mode pauses automatic wakes until you
  quit Muse with `/exit`; the observer then returns. Model/effort remain the role's own pair.
- Native `resume --no-session-log` is a replay with unsaved new turns, not a live spectator.
  It neither follows durable appends nor prevents divergent unrecorded work, so SHOAL does not
  use it as a viewer. Newer Muse versions advertise `serve --listen` multi-client sockets;
  this exec-per-wake adapter does not use that transport.
- **Native peer messaging** (Muse "session messaging") works between live interactive, named sessions of one
  user on one machine: ask the session to list its peers, then send by exact name. Headless (`muse exec`) and
  `--no-session-log` sessions are not peers. The receiver is asked to approve the sender (allow once or for the
  session; requests expire after 30 minutes with a 30 minute retry cooldown); a message is data, never
  authorization, and is at most 8,192 bytes. It is a fallback for conversations between interactive seats; role
  coordination and anything that must be durable or reach a Claude or exec seat goes through the shoal ledger: an interactive seat reaches a role with
  `shoal msg --shoal <id> --by ext:<seat> --to <role> --text ...` (plain mail, no wake, no authority).
- **An attached client holds a role's session.** Muse allows one owner per session: an exec turn started while
  `muse resume <id>` is open fails with "session ... is already in use" (measured 2026-10-15, fts-core, and the
  wake was lost). So an attached client counts as busy: the watcher holds the wake (it goes out at the first idle
  tick after you detach), `start_run` refuses and the runner exits 75, and the status line reads
  `status:busy attached`. Ordinary role observation no longer opens this exclusive client.
  Quit native interaction with `/exit` when finished; never kill the running turn to view it.
- Interactive seats are not shoal roles: they are not woken by `exec:` and have no ledger watcher. Making an
  interactive pane a ledger-woken role (a wake typed into the pane) is deliberately not built yet.

## State

`/tmp/shoal-muse-<uid>/<session>/` (override `SHOAL_MUSE_STATE`): `lock` (flock; held means a turn runs; the caller takes
it and the runner inherits it), `run.jsonl` (every JSONL record), `run.err`, `last.json` (started, ended, rc, terminal,
reason, text, error), `prompt-*.txt`, `runner.log`. `shoal muse-tail --session <id>` reads it; `shoal muse-run` is the
runner (internal).

Modern native journal activity is read from `sessions/YYYY/MM/DD/<id>/session.jsonl` under
`SHOAL_MUSE_DATA`, or the XDG Muse data directory. The older journal layout remains a fallback.
A native `.session.lock` supplies a pid only while locked, with matching process identity and
file ownership; stale text is not ownership. Native `exec` processes do not count as interactive
viewers. Journal age remains a work estimate; an unreadable journal is unknown, not proof of work.

## Environment

`SHOAL_MUSE_BIN` (default `muse` on PATH, then `~/.local/bin/muse`), `SHOAL_MUSE_ARGS` (extra `muse exec` flags; tests use
`--provider echo`), `SHOAL_MUSE_ALLOW_API_KEY=1` (otherwise `META_API_KEY` is removed from every turn: it outranks the
subscription login and bills per token). Every turn also gets `MUSE_NO_AUTO_UPDATE=1` and `MUSE_LOGIN=0`.

## Operating

- Sign in once, by hand: `muse login` (device code in a browser). Never set `META_API_KEY` on the host.
- Move a role: set its `ecosystem` to `muse`, then `shoal restart --spec $SPEC --only <slug>` (the launch flow starts the
  first turn with the brief and arms the watcher). Its start doc and ledger identity are unchanged.
- A standalone session: `shoal ui pick --harness muse --id <name-or-uuid> --label <label> --save`. A name that does not
  exist yet starts a fresh session the first time; name it with `/name`.
- Instructions and settings: `sync-agent-instructions.sh push --harness muse` (a compact core in
  `~/.config/muse/AGENTS.md` plus doctrine skills in `~/.agents/skills`) and `sync-muse-settings.sh`.

## Tests

`tests/selftest_muse.sh` (stub `muse`; `SHOAL_TEST_REAL_MUSE=1` also runs the installed binary's `echo` provider) and
`selftest_h4.sh` section 33.
