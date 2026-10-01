# SHOAL

**SHOAL (Stigmergic Heterogeneous Open Agent Lattice)** runs a group of long-lived agent sessions,
each holding one role, against one priority-ordered backlog. The sessions never message each other
to get work done: each reads a shared, append-only ledger (a git repository with one file per
writer), acts, and writes one event per decision, so coordination happens through the traces the
agents leave (stigmergy). The group is flattened: there is no coordinator session. Roles pull work
from the backlog (a GitHub Projects board, whose order is the priority) or take work the maintainer
offers, and a pair of cheap timers (the health quorum) wakes a stalled role or pages the maintainer
without spending model tokens. It is harness-neutral: Claude Code and opencode sessions work in the
same group today, and Codex support is planned.

## Install

SHOAL is a Python package with no runtime dependencies beyond the standard library (plus `tomli` on
Python 3.10). From a checkout:

```bash
uv tool install -e .      # or: python -m pip install -e .
shoal --help
```

`python3 shoal/cli.py` runs the same CLI without installing.

## Quickstart

1. Copy [`config/host.example.toml`](config/host.example.toml) to `config/host.toml` (gitignored)
   and set at least `ledger_repo` and `data_root`. Every key can also come from a `SHOAL_*`
   environment variable.
2. Check the host: `shoal doctor`.
3. Start a group and operate it with the commands in
   [`docs/operator-cheatsheet.md`](docs/operator-cheatsheet.md).

Further reading:

- [`docs/architecture.md`](docs/architecture.md): the components and how they fit.
- [`docs/providers.md`](docs/providers.md): the host contracts (resource leases, wait targets,
  board source, harness adapters) and how to supply your own.
- [`docs/limitations.md`](docs/limitations.md): what the current design does not handle yet (ledger
  growth, multi-host joining, one repository for every group, git's scaling ceiling), with the
  measurement that would trigger each refactor.
- [`plugins/shoal-skills/skills/shoal-protocol/SKILL.md`](plugins/shoal-skills/skills/shoal-protocol/SKILL.md):
  the protocol every role session follows.
- [`plugins/shoal-skills/skills/shoal-operator/SKILL.md`](plugins/shoal-skills/skills/shoal-operator/SKILL.md):
  the operator's duties.
- [`plugins/shoal-skills/skills/shoal-health-review/SKILL.md`](plugins/shoal-skills/skills/shoal-health-review/SKILL.md):
  the operator's periodic health review, with its per-posture checks and failure-mode catalog.

## Tests

```bash
for t in selftest_ledger.sh selftest_h4.sh selftest_h8.sh; do bash tests/selftest_hermetic.sh $t; done
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

Apache License 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
