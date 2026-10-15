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
