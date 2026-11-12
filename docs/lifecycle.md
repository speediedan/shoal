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
`manifest`, `[cold] warm_days`, `[hooks] commit`). The checkpoint reads `[checkpoint]`; retirement commands read
`[hooks] on_retire`. Deep-pass settings are reserved for the deep-pass command.

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
Notes are limited to ten entries of at most 200 characters each, including a final `(N more)` entry when notes were
omitted, so every reader can fold the shared ledger without an unbounded diagnostic payload.
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

In a commit hook, only `{message}` and `{paths}` are replaced, each already shell-quoted, so write them unquoted
(`commit = "my-commit.sh -m {message} {paths}"`); any other brace (`${VAR}`, awk's `{print}`) is left as written.

For any other repository the hooks are ignored, with a note, and the defaults apply. The other settings run nothing and
are read from any repository.

A repository is its git top level, so a worktree is its own repository here: list each worktree path that should run
hooks. A hook trusted for a main checkout is not trusted in that checkout's worktrees, which is the safer default.

## Retirement and recovery

`retire`, `remove-role` and `remove-session` run a configured, trusted `on_retire` hook after the role or saved session
has left the live start-doc roots. A role's `dir` field overrides its document directory; otherwise it is the directory
of its start doc. Relative overrides resolve against the group's working directory (a standalone seat's `cwd`). The
document directory must contain the start doc and stay inside its repository.

The hook command is split into argv using shell-style quoting, with no implicit shell. Its first executable must
resolve before anything is removed. Relative executable paths resolve against the repository root, which is also
the hook's working directory. A hook can explicitly invoke a shell if needed. It inherits the caller's environment,
with these five injected values:

| Variable | Value |
| --- | --- |
| `SHOAL_ID` | group id, empty for a standalone seat |
| `SHOAL_ROLE` | role slug or standalone label |
| `SHOAL_DOC` | absolute start-doc path |
| `SHOAL_ROLE_DIR` | absolute document directory |
| `SHOAL_HOST` | document host |

Preflight verifies host trust, executable availability, a clean document directory (including untracked files), live
roots and symlinks. The owning root is allowed before removal; another owner's root in, or pointing into, the
directory blocks teardown. Dry-run prints exact resolved argv, repository and injected environment, without running
the hook or recording removal. No-hook and untrusted configurations retain the existing removal behavior. A known
repository's empty hook configuration also keeps group retirement's existing offline behavior; a remote-only
directory or a configured routed hook requires a reachable document-host route and compatible worker CLI.

The role's context-restart lock and the seat's handoff lock cover preflight, removal and hook execution. For a routed
hook, a worker holds the locks on the document host while the caller updates metadata, and releases them when the
caller completes or disconnects. Immediately before the hook, cleanliness, live roots and symlinks are checked again.
Root snapshots are refreshed from the ledger and routed caches are bypassed (`refs --roots --all-hosts --refresh`).
There is no dependency on a context advisory timestamp or a restart-ready declaration.

Whole-group `retire` normally leaves sessions intact. With a trusted hook enabled, every target role must instead be
parked or have no live session. A live role blocks retirement before the spec changes and the CLI prints its park
command. `--no-archive` explicitly skips hooks, preserves ordinary teardown behavior and records the opt-out:

```bash
shoal retire --spec <spec> --dry-run
shoal remove-role --spec <spec> --only <role> --no-archive
shoal remove-session --session <label> --dry-run
```

Hooks and explicit opt-out receipts require a configured ledger repository. Hook outcomes are durable `system`
events named `retire-hook`, with `status` (`ok` or `failed`), role, document directory, host, argv, environment, retry
target and an error tail. Group events live in the group's ledger. Standalone events use the reserved ledger
namespace `shoals/_standalone/ledger/`, without a group spec. A prepared receipt precedes teardown; a pending receipt
confirms removal before execution, so an interrupted operation remains discoverable. The terminal outcome follows.
`shoal show` and `shoal status` display unresolved failures, including standalone failures; successful retry clears
the warning. No health page is raised.

Removal and hook outcome are reported separately. A hook failure returns nonzero and never rolls back a completed
removal or attempts to restore a pruned session. Hooks must be idempotent: a partial run can be retried safely. Retry
reads the recorded target, resolves the current trusted hook, obtains the same locks and performs the full preflight
again. The repository identity is retained even if a partial hook already archived the original directory:

```bash
shoal lifecycle retire-hook --shoal <id> --role <role> --dry-run
shoal lifecycle retire-hook --shoal <id> --role <role>
shoal lifecycle retire-hook --session <original-session-id-or-label>
shoal lifecycle retire-hook --status
```

A prepared receipt with live roots still present requires finishing the original removal first. A dirty directory
or a new live root blocks retry. A disabled or untrusted hook is not executed from an old receipt. If several older
directories match a role, their unresolved outcomes are retried separately under their locks. Hook execution has a
600-second timeout; a lost routed connection leaves an unresolved outcome for inspection and idempotent retry.

Generic cross-owner reference rewriting is a separate archive feature. Until it exists, an archive hook must keep
the archive command's reference checks and report references that prevent whole-directory archival.
