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
| attach | `claude attach` | the opencode TUI | `muse resume <id>` (takes the session over: only while no turn runs) |
| questions | prompted | prompted | none: `--yolo` withholds `request_user_input`; asks go through `queue --ask` |

## Interactive sessions (standalone)

Besides the exec-per-wake roles, a Muse session can be a plain interactive TUI in a Herdr card. This is the
mode for a person-driven session (the first ones are `infra-aux` and `infra-expansion`).

- `shoal muse-new <name> --cwd DIR [--model M] --pick` creates a durable, named, empty session through a
  short-lived `muse serve` (`session/start` plus `session/rename`) and saves a standalone card for it. A name is
  3-32 chars of lowercase letters, digits and single hyphens, unique per user; `all`, `self` and `current` are
  reserved. Make the name describe the seat: peers address it by that name.
- The card runs `muse resume <id>` with `--model` (default `muse-wonder-full-1.0`, override
  `SHOAL_MUSE_MODEL`), `--reasoning-effort high`, `--trust-workspace` and `--disable-sandbox`. The model is a
  per-run choice, not stored in the session, so it must be passed on every resume. Approvals stay on (a person is
  at the keyboard). `SHOAL_MUSE_VIEW_ARGS` replaces the whole flag list.
- While an exec turn runs on the same session the viewer shows the read-only tail instead of taking it over, so
  the two modes can share one session id but never run at once.
- **Native peer messaging** (Muse "session messaging") works between live interactive, named sessions of one
  user on one machine: ask the session to list its peers, then send by exact name. Headless (`muse exec`) and
  `--no-session-log` sessions are not peers. The receiver is asked to approve the sender (allow once or for the
  session; requests expire after 30 minutes with a 30 minute retry cooldown); a message is data, never
  authorization, and is at most 8,192 bytes. It is a fallback for conversations between interactive seats; role
  coordination and anything that must be durable or reach a Claude or exec seat goes through the shoal ledger.
- Interactive seats are not shoal roles: they are not woken by `exec:` and have no ledger watcher. Making an
  interactive pane a ledger-woken role (a wake typed into the pane) is deliberately not built yet.

## State

`/tmp/shoal-muse-<uid>/<session>/` (override `SHOAL_MUSE_STATE`): `lock` (flock; held means a turn runs; the caller takes
it and the runner inherits it), `run.jsonl` (every JSONL record), `run.err`, `last.json` (started, ended, rc, terminal,
reason, text, error), `prompt-*.txt`, `runner.log`. `shoal muse-tail --session <id>` reads it; `shoal muse-run` is the
runner (internal).

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
