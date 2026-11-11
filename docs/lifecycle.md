# The role-document lifecycle: a repository's settings

A role keeps its working memory in one directory: its start doc ([`start-doc.md`](start-doc.md)), the companion log, and
auxiliary files. Over a role's life those files are distilled, archived and finally retired with it. How a repository
wants that done differs from one infrastructure to the next, so shoal reads it from one file at the root of the
repository that holds the role directories:

```
<repository>/.shoal/lifecycle.toml
```

The file travels with the repository: every shoal and every standalone session writing there reads the same values,
whatever harness it runs in. Nothing in it depends on a harness. Without the file the defaults below apply.

```bash
shoal lifecycle show [<path>] [--json]   # the effective settings for the repository holding <path>, and their source
```

## Settings and defaults

```toml
[layout]
role_dir = ""                          # "" : a role's directory is its start doc's
archive = "archive/{path}"             # where an archived file goes: the original path under archive/
manifest = "archive/{dir}/MANIFEST.jsonl"  # one append-only manifest per archived directory

[tiers]
history = ["*_log.md", "*_log_*.md"]   # documents that record what was true when written
exclude = ["archive/*"]                # trees never scanned
header_max_lines = 40                  # the start-doc header cap (advisory)
header_max_bytes = 3072

[checkpoint]
archive = "propose"                    # auto | propose | off
min_age_days = 7                       # an unreferenced file younger than this is not an archive candidate
cross_owner = "notify"                 # notify | rewrite | keep: a candidate another role's live document names

[deep_pass]
every_days = 3
every_restarts = 3

[cold]
warm_days = 90                         # warm in the tree, then deleted with a manifest event naming its last commit
after_checkpoints = 2

[instructions]
every_days = 14                        # instruction-tier distillation, as proposed diffs only

[hooks]
commit = ""                            # how to commit by exact path; "" : git commit -- <paths>
                                       # {message} and {paths} expand shell-quoted: write them unquoted
on_retire = ""                         # run when a role or standalone session retires
```

A key that is unknown, of the wrong type, or outside its choices is reported by `shoal lifecycle show` and the default
is kept; a bad file never stops a command. The same holds for a cap or cadence below 1 (or a negative `min_age_days`),
and for a `[layout]` template that is absolute, starts with `~`, or has a `..` segment: layout is read from any
repository, and files are moved by it, so where files go never leaves the repository.

Today these settings are read by `shoal refs` (`[tiers]` and `checkpoint.min_age_days`, unless flags name them), by
`doc-check` and `doc-head` (the header cap), and by `shoal archive` ([`archive.md`](archive.md): `[layout] archive` and
`manifest`, `[cold] warm_days`, `[hooks] commit`). The checkpoint, deep-pass and `on_retire` settings are read by the
commands that follow them.

## Checkpoint pass at restart-ready

Before each restart-ready checkpoint, the role distills its own start doc: append completed details and decisions
that no longer apply to its append-only log, keep only current guidance, refresh the bounded header, and append the
session entry and snapshot. Read live PR, CI and queue state when needed instead of preserving copies of it. Commit
the exact changed paths through the repository's normal hook.

`queue --restart-ready` then runs an advisory lifecycle check. Standalone sessions run the same check explicitly:

```bash
shoal lifecycle checkpoint <start-doc> [--json]
```

The check reads `doc-check` and the directory's reference classification, including roots from the host's live specs
and standalone registry. It reports dangling references, deleted and untracked files, header warnings and archive
candidates without changing files or running hooks. Each category prints its count and at most five examples. The
queue event records the bounded report as `lifecycle_checkpoint`; the next session can act on unresolved advice.
Other hosts' standalone roots are not read here, so this is advice rather than authority to move anything. The
archive operation still checks roots across hosts before a move.

For `[checkpoint] archive = "propose"`, the role lists candidates for review or records a `keep: <reason>` in their
front matter. Roots, live files and untracked files cannot be archive candidates. Resolve untracked work first.
Keep files another owner's live document references and tell that owner through ledger mail to update its link.
Automatic archival remains deferred pending model-cost measurements: `auto` currently also reports proposals and
never moves files. `off` skips the lifecycle check. The check itself makes no model calls.

The probe runs in a child process with a 15-second timeout. A failed scan, a timeout, a header warning or unresolved
candidates never reject `restart_ready`, including beyond the context limit. Existing start-doc freshness, required
sections and durable-job-reference checks still apply. No lifecycle enforcement gate is introduced in this release.
Measure the model work separately on one or two roles before enabling routine distillation or automatic archival:
record turn-token counters immediately before and after the pass, separating cached input and output where available.

## Hooks run only for a trusted repository

`[hooks]` names commands shoal would run, which makes the file code execution from repository content: any repository a
role writes to could carry one. So shoal honours a repository's hooks only when this host's config opts that repository
in:

```toml
# config/host.toml
[lifecycle]
trusted_repos = ["/path/to/repository"]
```

In a hook, only `{message}` and `{paths}` are replaced, each already shell-quoted, so write them unquoted
(`commit = "my-commit.sh -m {message} {paths}"`); any other brace (`${VAR}`, awk's `{print}`) is left as written.

For any other repository the hooks are ignored, with a note, and the defaults apply. The other settings run nothing and
are read from any repository.

A repository is its git top level, so a worktree is its own repository here: list each worktree path that should run
hooks. A hook trusted for a main checkout is not trusted in that checkout's worktrees, which is the safer default.
