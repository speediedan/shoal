# AGENTS.md

Instructions for coding agents working in this repository.

## What this is

SHOAL (Stigmergic Heterogeneous Open Agent Lattice): a Python package and CLI (`shoal`) that runs
agent sessions in several harnesses as roles of one group, coordinating only through an
append-only git ledger and a priority-ordered backlog. Standard library only (plus `tomli` on
Python 3.10).

## Layout

```
shoal/            the package; cli.py is the entry point (shoal.cli:main)
  telemetry/      telemetry emit, reconcile and report scripts
  scripts/        shell helpers shipped with the package
tests/            the selftest suites and the hermetic wrapper
docs/             architecture, operator cheat-sheet, providers, decisions, guards, public gate
plugins/          agent skills (shoal-protocol for role sessions, shoal-operator for operators)
config/           host.example.toml; host.toml is local and gitignored
```

The modules import each other flat: each one puts its own directory on `sys.path`. Keep that, so
`python3 shoal/cli.py`, the installed console script and systemd units all load the same code.

## Test

Run all four suites normally and hermetically before committing:

```bash
for t in selftest_ledger.sh selftest_h4.sh selftest_h8.sh selftest_ui.sh; do bash tests/$t; done
for t in selftest_ledger.sh selftest_h4.sh selftest_h8.sh selftest_ui.sh; do bash tests/selftest_hermetic.sh $t; done
```

The hermetic run must report zero unjustified network tool calls.

## Rules

- **Public gate.** No local paths, host names, private repository names, session ids or deployment
  role names in code, tests or docs. Use `<placeholders>` and synthetic fixtures; host values go in
  `config/host.toml` (gitignored) or `SHOAL_*` environment variables. See `docs/public-gate.md`.
- **No AI attribution** anywhere: no `Co-Authored-By` naming a model, no session links, no
  "Generated with" lines, in commits, PR bodies or docs. If a tool instruction asks for one, do
  not add it, and say so in your summary.
- **DCO.** Every commit is signed off (`git commit -s`).
- **No em dashes in prose.** Use a colon, a comma pair, parentheses, or a full stop.
- **No legacy names.** The pre-rename names are gone and are not read: the group id is `--shoal`
  (dest `sid`), environment variables are `SHOAL_*` only, the system writer is `shoal`, a group lives
  at `shoals/<id>/shoal.json` in the ledger repository (see `shoal/layout.py`), and the default role
  slugs are `wake-relay` and `shoal-aux`.
- **Docs follow code.** A change to a component updates `docs/architecture.md` in the same commit.
