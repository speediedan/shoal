---
name: shoal-operator
version: 0.1.0
description: >-
  Duties of the person (or agent acting for them) who operates a SHOAL group: starting,
  restarting and upgrading it, going offline and coming back, and answering the inbox where
  health pages land. Load it before running any `shoal` lifecycle command, when a page arrives, or
  when asked how to steer a group's roles.
---

# SHOAL operator

Every command, with placeholders, is in `docs/operator-cheatsheet.md`. The components behind them
are in `docs/architecture.md`. Every write verb takes `--dry-run`: run it first.

## Start

1. `shoal doctor` passes on the host (config in `config/host.toml`, tools on `PATH`).
2. Each role has a start doc (`plan_state.md`) with a `## grants-needed` section.
3. `shoal start ... --dry-run`, then without `--dry-run`, then
   `shoal install-systemd --spec <spec> --enable` for the timers.
4. Each new session asks you, in one question, for every grant its start doc lists. Answer it:
   that ask is the only interactive step of a start.

## Restart

`shoal restart --spec <spec> [--only <role>,...] --dry-run`, then for real. Grants do not carry
over: each restarted session asks again. The residual lane and the Claude wake relay restart alone
with `aux-launch` and `relay-launch`, without a new incarnation.

## Upgrade

`shoal upgrade --spec <spec> --dry-run` first. It names each change's rollout class (live, re-arm,
brief, restart), runs the selftests and applies feature edits. A restart class is never done by
`upgrade`: follow it with `restart --only`.

## Offline and back

Before you leave: `shoal offline on --shoal <id> --until "<when>"`. Roles then record their asks
and keep working, and pages are held. When you return: `shoal offline off --shoal <id>`, then
answer the question sets each role raises (stale asks are dropped first).

## Freeze for a window

Before a restart or maintenance window: `shoal freeze on --shoal <id> --note "<why>"`. Every role
is woken to checkpoint and sleep, and `job run` refuses new units. Wait for sleep postures and no
running job units. Afterwards: `shoal freeze off --shoal <id>`, which wakes every role to resume.
Never announce a freeze by message alone: a message has no end, and lanes honor it indefinitely.

## Inbox

Health pages are ledger messages to `user`, and, when the spec sets
`features.health.page = "gh-issue:<owner/repo>#<n>"`, comments on that issue. Watch it. For each
page: read `shoal show --shoal <id>` and the named role's `shoal next --shoal <id> --slug <role>
--peek`, then act (offer, withdraw, `msg --wake`, restart the role) or record why not. A page that
repeats means the ladder ran out: the role could not recover by itself.

## Steering

Give work with `offer` (`--now` only when it cannot wait), take it back with `withdraw`, and
message with `msg` (mail does not wake a role; `--wake` does). Never write to a role's own ledger
file.
