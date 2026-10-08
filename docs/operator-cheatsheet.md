# SHOAL operator cheat-sheet

Commands for the person operating a SHOAL group. Placeholders: `<id>` is the group id, `<role>` a role
slug, `<repo>#<n>:<verb>` an item (verb `fix` or `review`), `<...>` text is yours to write. Every
write verb takes `--dry-run`. `--by` defaults to `user` (you).

## Setup (once per shell)

```bash
export S=<id> SPEC=<path to the group's spec>
```

## Look (read-only)

- The whole group, one row per role (posture, active and next items, flags):

    ```bash
    shoal show --shoal $S
    ```

- One role in depth, or its boundary view exactly as the role sees it (`--peek` leaves its mail
  unread):

    ```bash
    shoal show --shoal $S --slug <role>
    shoal next --shoal $S --slug <role> --peek
    ```

- Health: host and group checks, what the health quorum would do now, and ledger permissions:

    ```bash
    shoal doctor --spec $SPEC
    shoal health --spec $SPEC --instance a --dry-run
    shoal ledger-lint --shoal $S
    ```

- The board as a role sees it (ranked candidates, the semantic ranking, the switch bar):

    ```bash
    shoal digest --shoal $S --slug <role>
    shoal digest --shoal $S --slug <role> --semantic
    shoal digest --shoal $S --compare
    ```

## Operator UI (Herdr; details in [`ui.md`](ui.md))

- Everything above as one read-only document for every shoal on the host, or one line:

    ```bash
    shoal ui status [--shoal $S] [--conditions] [--json]
    shoal ui line
    ```

- Watch or answer one role in its own harness client (never a resume, never a wake; detach to
  leave it running), or any standalone session:

    ```bash
    shoal view $S <role>
    shoal view --harness claude|opencode --session <id>
    ```

- The Herdr workspace (once on the lane host; a viewing machine then attaches with
  `herdr --remote <host> --session shoal --remote-keybindings server`):

    ```bash
    shoal ui herdr-install            # config block, plugin link, herdr + feeder units, layouts
    shoal ui doctor                   # restore off, no integrations, schema, units
    shoal ui open                     # re-apply layouts after adding a role (fills gaps only)
    ```

## Give work and steer roles

- Offer an item. The simplest form, then with a note, then every optional flag (`--base` records
  the commit you judged against, and `--prio` orders open offers, lower first, default 50):

    ```bash
    shoal offer --shoal $S --to <role> --item <repo>#<n>:<verb>
    shoal offer --shoal $S --to <role> --item <repo>#<n>:<verb> --note "<context the role needs>"
    shoal offer --shoal $S --to <role> --item <repo>#<n>:<verb> --base <repo>@<sha> --prio 10 --note "<context>"
    ```

- Interrupt a busy role with work that cannot wait (use sparingly), or take an offer back:

    ```bash
    shoal offer --shoal $S --to <role> --item <repo>#<n>:<verb> --now --note "<why it cannot wait>"
    shoal withdraw --shoal $S --to <role> --item <repo>#<n> --reason "<why>"
    ```

- Message a role. Mail never wakes it; `--wake` does:

    ```bash
    shoal msg --shoal $S --to <role> --text "FYI: <note>"
    shoal msg --shoal $S --to <role> --wake --text "<question>? Answer first-person."
    ```

- A role's recorded asks as the question sets it will raise (live PR labels):

    ```bash
    shoal asks --shoal $S --slug <role>
    ```

- Going away and coming back. Roles record their asks while you are offline; on return each one
  drops the stale asks and raises the rest as question sets:

    ```bash
    shoal offline on --shoal $S --until "<when>" --note "<why>"
    shoal offline off --shoal $S
    ```

- Hand a claimed item from one role to another. The holding role runs this itself:

    ```bash
    shoal release --shoal $S --slug <holding-role> --item <repo>#<n> --kind handoff --to <new-role> --reason "<why>"
    ```

## Group lifecycle

- Start a group (dry-run first). `--role` is `SLUG,ECOSYSTEM,TITLE,DOC[,fill=self|assigned|none][,aux][,model=M][,effort=E]`:

    ```bash
    shoal start --name <name> --dir <working repo> \
      --role "<role>,opencode,<title>,<plan_state.md>,fill=self,model=<model>" \
      --role "<role>,claude,<title>,<plan_state.md>,fill=assigned,model=<model>" \
      --repo <owner/repo> --source github-project:<owner>/<number> --dry-run
    ```

- Restart into the next incarnation (all roles, or `--only` some); add a role; retire:

    ```bash
    shoal restart --spec $SPEC [--only <role>,<role>] --dry-run
    shoal add-role --spec $SPEC --role "<role>,<ecosystem>,<title>,<plan_state.md>,fill=self,model=<model>" --dry-run
    shoal retire --spec $SPEC --uninstall-units --note "<why>" --dry-run
    ```

- The residual lane and the Claude wake relay, each started alone (no incarnation bump):

    ```bash
    shoal aux-init --shoal $S && shoal aux-launch --shoal $S
    shoal relay-init --shoal $S && shoal relay-launch --shoal $S
    ```

- Timers (health quorum, digest, reconcile, reports), from the spec:

    ```bash
    shoal install-systemd --spec $SPEC --dry-run
    shoal install-systemd --spec $SPEC --enable
    ```

- Upgrade a running group. It prints each change's rollout class (live, re-arm, brief, restart),
  runs the tests, and applies feature edits:

    ```bash
    shoal upgrade --spec $SPEC --dry-run
    shoal upgrade --spec $SPEC --set 'matcher={"live": false}'
    shoal upgrade --spec $SPEC --enable matcher.live
    shoal upgrade --spec $SPEC --enable blockers --rearm now   # verified blockers; idle checked against the board
    shoal upgrade --spec $SPEC --enable waittargets --rearm now   # CI waits need a target; watchers wake on job/lease ends
    shoal upgrade --spec $SPEC --enable health.revive              # respawn dead Claude workers, relaunch a dead relay (FM-14)
    ```

## Reports

```bash
shoal report --spec $SPEC --gen <generation>
shoal report --spec $SPEC --hours 168
shoal kill-audit --no-record
```

## Jobs

```bash
shoal job status --shoal $S
shoal job stop --shoal $S --slug <role> --name <job> --dry-run
```
