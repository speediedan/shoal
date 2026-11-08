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

## Hooks run only for a trusted repository

`[hooks]` names commands shoal would run, which makes the file code execution from repository content: any repository a
role writes to could carry one. So shoal honours a repository's hooks only when this host's config opts that repository
in:

```toml
# config/host.toml
[lifecycle]
trusted_repos = ["/path/to/repository"]
```

For any other repository the hooks are ignored, with a note, and the defaults apply. The other settings run nothing and
are read from any repository.

A repository is its git top level, so a worktree is its own repository here: list each worktree path that should run
hooks. A hook trusted for a main checkout is not trusted in that checkout's worktrees, which is the safer default.
