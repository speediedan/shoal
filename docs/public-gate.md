# Public gate

This repository starts private. It becomes public only when every item below is checked. Record the
evidence (command, date, result) under each item in the change that ticks it.

## 1. Secrets, over the whole history

- [ ] `gitleaks detect --source . --log-opts="--all"` reports no leaks.
- [ ] A pattern sweep over every commit finds no keys or tokens:
      `git log --all -p | grep -nE "(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[bap]-|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|API_KEY=|_TOKEN=)"`
      returns nothing, or each hit is explained.
- [ ] No `.env` file, or `.env` contents, ever committed: `git log --all --diff-filter=A --name-only | grep -E '(^|/)\.env'`
      is empty.

## 2. Local data, in the tree and the history

The history was moved from a private repository and carries the original host's paths. For each
pattern, either rewrite the history (`git filter-repo --replace-text <rules>`) or accept each
remaining hit explicitly here, with the reason it is benign:

- [ ] absolute home paths (`/home/`), mount points (`/mnt/`), hostnames;
- [ ] session identifiers (`ses_...`, harness session UUIDs);
- [ ] names of private repositories and private ledger remotes;
- [ ] group ids and role names from the original deployment, in code, tests and docs (examples
      use placeholders);
- [ ] maintainer-specific narratives (incident write-ups with host detail): none in `docs/`.

## 3. The system inbox

- [ ] Health pages go to an issue in a private location. The inbox issues in this repository are
      transferred or locked, because pages name roles, PRs and private repositories.

## 4. Examples, fixtures, configuration

- [ ] Tests use synthetic fixtures only.
- [ ] `config/host.example.toml` holds placeholders only.
- [ ] Documentation examples use `<placeholders>`.

## 5. Licensing

- [ ] `LICENSE` (Apache-2.0) and `NOTICE` present since the first commit.
- [ ] `CONTRIBUTING.md` describes DCO sign-off, and every commit since creation carries
      `Signed-off-by:`.

## 6. CI

- [ ] The test workflow is green on the public runners, running hermetic tests only (no live
      board, no host services).
