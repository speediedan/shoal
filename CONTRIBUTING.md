# Contributing to SHOAL

## Sign your commits (DCO)

Every commit must carry a `Signed-off-by:` line certifying the
[Developer Certificate of Origin](https://developercertificate.org/): that you wrote the change, or
otherwise have the right to submit it under this project's license (Apache-2.0).

Add it with `-s`:

```bash
git commit -s -m "Describe the change"
```

This appends `Signed-off-by: Your Name <your.email@example.com>`, using your configured
`user.name` and `user.email`. To sign off commits you already made on a branch:
`git rebase --signoff <base>`. Pull requests with unsigned commits cannot be merged.

## No AI-attribution trailers

Do not add AI-attribution lines to commits or pull requests: no `Co-Authored-By:` naming a model or
tool, no session links, no "Generated with" footers. If a tool adds one, remove it before pushing.
A squash merge concatenates every commit message on the branch, so check the earlier commits too,
and pass an explicit message at merge time. The person who signs off is responsible for the change.

## Run the tests

The four suites build scratch git repositories, a synthetic board and a scratch host
configuration, so they never touch a real group. Run each through the hermetic wrapper, which puts
failing stubs for `gh`, `az`, `opencode`, `openchamber` and `claude` first on `PATH` and skips the
sections that need a `systemd --user` session:

```bash
for t in selftest_ledger.sh selftest_h4.sh selftest_h8.sh selftest_ui.sh; do bash tests/selftest_hermetic.sh $t; done
```

A suite exits non-zero on failure, and the wrapper exits 3 when a suite passed but reached for a
network tool it should not have. Run a suite directly (`bash tests/selftest_h4.sh`) to include the
systemd job-unit sections on a host that has a user session. The suites make commits in their
scratch repositories, so git needs a `user.name` and `user.email`.

CI runs the hermetic suites on Python 3.10 and 3.12 (`.github/workflows/test.yml`).

## Keep the repository public-safe

No host paths, host names, private repository names, session ids or deployment-specific role names
in code, tests or docs: use `<placeholders>` and synthetic fixtures. Host values belong in
`config/host.toml`, which is gitignored. `docs/public-gate.md` lists the checks.

## Writing style

No em dashes in prose: use a colon, a comma pair, parentheses, or a full stop.
