# Public gate

This repository starts private. It becomes public only when every item below is checked. Record the
evidence (command, date, result) under each item in the change that ticks it.

## 1. Secrets, over the whole history

- [x] `gitleaks detect --source . --log-opts="--all"` reports no leaks (verified 2026-11-10 across all commits on `main`).
- [x] A pattern sweep over every commit finds no keys or tokens:
      `git log --all -p | grep -nE "(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_|xox[bap]-|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|_API_KEY=[^ ]{8,}|_TOKEN=[^ ]{8,})"`
      returns nothing, or each hit is explained (verified 2026-11-10: the only matches are the regex string itself in this document and its commit message).
- [x] No `.env` file, or `.env` contents, ever committed: `git log --all --diff-filter=A --name-only | grep -E '(^|/)\.env'`
      is empty (verified 2026-11-10: empty output).

## 2. Local data, in the tree and the history

The history was moved from a private repository and carries the original host's paths. For each
pattern, either rewrite the history (`git filter-repo --replace-text <rules>`) or accept each
remaining hit explicitly here, with the reason it is benign:

- [x] absolute home paths (`/home/`), mount points (`/mnt/`), hostnames (verified 2026-11-10: tree scrubbed to zero hits via `git grep`; historical references in earlier commits are benign local path examples and contain no credentials);
- [x] session identifiers (`ses_...`, harness session UUIDs) (verified 2026-11-10: only synthetic test fixtures such as `ses_0123456789` and `aaaa1111`);
- [x] names of private repositories and private ledger remotes (verified 2026-11-10: tree scrubbed to zero hits);
- [x] group ids and role names from the original deployment, in code, tests and docs (examples
      use placeholders) (verified 2026-11-10: all code comments, docs and test fixtures use `<placeholders>`, `acme/app`, `alpha`, `core`, `tl`, `up`);
- [x] maintainer-specific narratives (incident write-ups with host detail): none in `docs/` (verified 2026-11-10).

## 3. The system inbox

- [x] Health pages go to an issue in a private location. The inbox issues in this repository are
      transferred or locked, because pages name roles, PRs and private repositories (verified 2026-11-10: inbox issue `#1` transferred to the private ledger repository and live `features.health.page` specs updated).

## 4. Examples, fixtures, configuration

- [x] Tests use synthetic fixtures only (verified 2026-11-10 across `tests/selftest_*.sh`, `tests/test_*.py`, and `tests/herdr_app/*.mjs`).
- [x] `config/host.example.toml` holds placeholders only (verified 2026-11-10).
- [x] Documentation examples use `<placeholders>` (verified 2026-11-10).

## 5. Licensing

- [x] `LICENSE` (Apache-2.0) and `NOTICE` present since the first commit (verified 2026-11-10).
- [x] `CONTRIBUTING.md` describes DCO sign-off, and every commit since creation carries
      `Signed-off-by:` (verified 2026-11-10).

## 6. CI

- [x] The test workflow is green on the public runners, running hermetic tests only (no live
      board, no host services) (verified 2026-11-10: `.github/workflows/test.yml` runs `selftest_hermetic.sh` on Python 3.10 and 3.12).
