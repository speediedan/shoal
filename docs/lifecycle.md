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
archive = "propose"                    # auto | propose | off, or per role: { "*" = "propose", "<slug>" = "auto" }
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

[refs]
text_ext = []                          # extensions read for references beyond the built-in text set, e.g. [".rst"]
data_max_bytes = 4194304               # a .csv or .tsv larger than this is not read for references (and is reported)
```

A key that is unknown, of the wrong type, or outside its choices is reported by `shoal lifecycle show` and the default
is kept; a bad file never stops a command. The same holds for a cap or cadence below 1 (or a negative `min_age_days`),
and for a `[layout]` template that is absolute, starts with `~`, or has a `..` segment: layout is read from any
repository, and files are moved by it, so where files go never leaves the repository.

Today these settings are read by `shoal refs` (`[tiers]` and `checkpoint.min_age_days`, unless flags name them), by
`doc-check` and `doc-head` (the header cap), and by `shoal archive` ([`archive.md`](archive.md): `[layout] archive` and
`manifest`, `[cold] warm_days`, `[hooks] commit`). `[refs] text_ext` widens the files `refs`, `archive` and the deep pass
read for references: the built-in set is `.md`, `.sh`, `.py`, `.yaml`, `.yml`, `.json`, `.jsonl`, `.toml`, `.txt`, `.cfg`,
`.ini`, `.csv` and `.tsv` (a data table's path column is a reference its generator follows). The checkpoint reads `[checkpoint]`; retirement commands read
`[hooks] on_retire`. `lifecycle deep-pass` and the checkpoint's `deep_pass_due` read `[deep_pass]` and `[cold] warm_days`.

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
`archive` is one mode for every role, or a table of modes per role in the shape `context_restart` uses: keys are role
slugs or standalone labels, and `"*"` is everyone else (`propose` when absent). A pilot is
`archive = { "*" = "propose", "<slug>" = "auto" }`; a bad mode anywhere in the table keeps the default. For an auto role the checkpoint computes candidates
with the age floor on every class (`refs --age-floor-all`) and adds `commands`: the `shoal archive ... --cross-owner
<cross_owner> --strict` run that archives them, with absolute paths, which the role runs itself (from any directory) after moving any
durable knowledge up a tier (the protocol's work loop). The checkpoint still moves nothing and makes no model calls.
`off` skips the lifecycle check.

The probe runs in a child process with a 15-second timeout. A failed scan, a timeout, a header warning or unresolved
candidates never reject `restart_ready`, including beyond the context limit. Existing start-doc freshness, required
sections and durable-job-reference checks still apply. No lifecycle enforcement gate is introduced in this release.
Measure the model work separately on one or two roles before enabling routine distillation or automatic archival:
record turn-token counters immediately before and after the pass, separating cached input and output where available.

## Deep pass

Every `[deep_pass] every_days` days or `every_restarts` restarts, whichever comes first, a role tidies its directory
more deeply than a checkpoint does. It folds concluded auxiliary files into the start doc, the design doc or the log
and archives the originals. It reviews its `keep:` reasons, sends long-warm archive entries cold, and records the trend.
That is model work. The command gathers its inputs and says when it is due, and moves nothing:

```bash
shoal lifecycle deep-pass <start-doc> [--json]
```

- **Due**: the last pass is the last `Deep pass <UTC>:` line in the start doc's companion log; the log is append-only,
  so the last marker is the last pass. A marker inside a fenced block, or one whose time does not parse, is skipped.
  Restarts since are the `After ...` snapshot headings below it. With no marker, the pass reads as due.
- **Keep reasons to review**: each file whose front matter has `keep:`, with the days since it last changed. Remove a
  reason that no longer holds, and the next checkpoint lists the file as a candidate again.
- **Consolidation candidates**: the live auxiliary files (not a root, not history), oldest change first, with size.
- **Cold candidates**: entries of the directory's manifest whose latest event is `archive` or `migrate`, warm for at
  least `[cold] warm_days`, that no live document names now, with the `shoal archive --cold` command. `archive --cold`
  checks again before it moves anything.
- **Trend**: a line to append to the log's session snapshot, which is also the next pass's marker, for example
  `Deep pass 2026-11-17T22:00Z: 14 live auxiliary, 412 KB, 3 kept, 2 warm, 1 cold candidate`, and the previous
  marker's numbers, so growth is visible.

`lifecycle checkpoint` (and so `queue --restart-ready`) carries `deep_pass_due`, read from the companion log alone.
When the pass is due, its first note starts with the command, so the notes' 200-character cap never cuts it. A failure
in that check is one more note; it never costs the checkpoint its counts. Shoal records nothing itself: the marker in
the log is the record.

## Reading history: `log-entry` and `refs --resolve`

The log is read rarely, and then an entry at a time. `shoal log-entry <log> C12` prints that session's entries (its
checkpoints and addenda included) and nothing else, as `doc-head` prints only a start doc's header:

```bash
shoal log-entry <log> C12            # session C12's entries, in log order
shoal log-entry <log> 12             # a bare number matches any prefix: C12, r12, Session 12
shoal log-entry <log> --last 1       # the newest session's entries: the usual restart read
shoal log-entry <log> C12 --snapshot # its end-of-session snapshot (After C12 ...); --any: every heading with the key
shoal log-entry <log> --list         # the heading outline with line numbers, no bodies
```

An entry is a heading at levels 2 to 4 with its body, up to the next heading at the same or a higher level; a heading
inside a fenced block is content. A heading's key is its first session token after at most two words (`Session C4
checkpoint 2` has key `C4`, `After Session 12` has `12`). Session entries are the headings whose words before the key are
`Session`; snapshots are those that start `After`. `--last N` counts keys, in the chosen mode, so `--snapshot --last 2`
prints the two newest snapshots. No match exits 1 and names the nearest keys.

A path an old entry names may have been archived since. `shoal refs --resolve <path> [--from <log>]` says where it is
now, and how to read or restore it ([`refs.md`](refs.md#where-is-it-now---resolve)).

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
document directory must contain the start doc and stay inside its repository. A target with no start doc has nothing
to archive: the hook is skipped and the removal proceeds as before.

The hook command is split into argv using shell-style quoting, with no implicit shell. Its first executable must
resolve before anything is removed: a relative path containing a slash resolves against the repository root, a bare
name is looked up on `PATH`. The repository root is also the hook's working directory. A hook can explicitly invoke
a shell if needed. It inherits the caller's environment,
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
events named `retire-hook`, with `status` (`ok`, `failed`, or `aborted` when the removal itself refused after the
prepared receipt), role, document directory, host, argv, environment, retry
target and an error tail. Group events live in the group's ledger. Standalone events use the reserved ledger
namespace `shoals/_standalone/ledger/`, without a group spec. A prepared receipt precedes teardown; a pending receipt
confirms removal before execution, so an interrupted operation remains discoverable. The terminal outcome follows.
An `aborted` outcome (phase `removal-refused`) closes a prepared receipt when the removal returned without removing
anything, so it stops warning; a bare prepared receipt from a process killed mid-removal still reads as failed.
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
