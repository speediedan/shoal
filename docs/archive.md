# Archiving role documents (`shoal archive`)

A role's directory accumulates auxiliary files that stop mattering. Archiving one moves it out of the directory, to a
warm archive in the same repository, without breaking anything that still reads it. Later, a warm file goes cold:
it is deleted from the tree, and git history keeps it. Every step is one command, and every step is undone by one.

```bash
shoal archive <path> [<path> ...] [--reason R] [--by WHO] [--cross-owner notify|rewrite] [--dry-run] [--json]
                                                                                 # move to the warm archive
shoal archive --restore <original path> [--dry-run] [--json]                    # back to where it was
shoal archive --cold <original path> [...] [--force] [--dry-run] [--json]        # warm -> cold
shoal archive --migrate <legacy path or folder> [...] [--cross-owner notify|rewrite] [--reason R] [--dry-run] [--json]
                                                                                 # legacy archive folder -> the layout
```

Where files go comes from the repository's `.shoal/lifecycle.toml` ([`lifecycle.md`](lifecycle.md)): by default an
archived file goes to `archive/<original path>`, and each archived directory keeps one append-only manifest,
`archive/<original dir>/MANIFEST.jsonl`, so two roles archiving at the same time never write the same file.

## What it refuses

Nothing is moved when any path in the run is:

- not tracked (an untracked file is its owner's to commit or delete), or carries uncommitted changes;
- a start doc that a live spec or a standalone session names, on any routed host ([`refs.md`](refs.md),
  `--roots --all-hosts`), or the target of one named through a symlink, wherever the target lives. When a routed host cannot answer, the run is refused: a file here could be a start doc there.
  `--force` archives anyway and records that it did;
- named by a live document anywhere in the repository, or by a tracked symlink: that reader would break. Point those
  references elsewhere first, keep the file, or archive with `--cross-owner rewrite` (below). History documents (the
  repository's history tiers) keep their references as written: the manifest is what resolves them.

Files archived in the same run may name each other: a symlink archived together with its target stays paired, and a
moved relative symlink is re-aimed from its new place.

`--cold` refuses a file archived fewer than `[cold] warm_days` ago unless `--force` (the deep pass is what decides when
files go cold), and a file whose archive path a live document still names.

## Rewriting live references (`--cross-owner rewrite`)

With `--cross-owner rewrite`, a live reference no longer refuses the run: the same run points it at the archive path.
This is the one write a role may make outside its own directory, so it is a path substitution and nothing else.

- **Each reference keeps its form.** A path relative to the naming file stays relative (`../design/x/a.md` becomes
  `../../archive/proj/design/x/a.md`), a repository-relative one stays repository-relative, and an absolute or `~/` path
  keeps its prefix. A `#anchor` and trailing punctuation are kept. A unique partial path (`x/a.md`) becomes the full
  archive path: after the move it would still resolve, to the archived copy, and hide that the file was archived.
- **A directory the run empties is a target too.** When every tracked file under a directory moves, and all of them
  land under one mirror directory, a reference to the directory itself (`archive/runs/r1/`,
  `proj/design/x/archive`) is listed under `notify` and rewritten under `rewrite` like a file reference, keeping a
  trailing `/` when it had one. When the directory's files land in different places (a `--migrate` run whose legacy
  folder holds a nested legacy folder, say), no one path replaces the directory, so `rewrite` refuses and lists the
  references. The manifest records a directory's rewrites with the first file (sorted) moved out of it, each entry
  carrying `dir`. "Empties" counts tracked files only: a directory that still holds untracked files stays on disk
  after the run, yet its references now name the archive. Commit or remove those files first if the old path should
  keep meaning something.
- **`--strict` refuses a partial path.** A unique partial path (`x/a.md`) can be prose about another repository's file
  that happens to match, so with `--strict` the run refuses and lists such references instead of rewriting them. An
  unattended run passes it; a person can check the list and run without it.
- **A tracked symlink is repointed** in its own form: a relative link stays relative. (Only relative links are read as
  references today; an absolute one would stay absolute.) A symlink that is a start doc refuses the run: that role
  moves first.
- **A referrer must be tracked, clean and UTF-8 text with `\n` line endings.** History and excluded documents are never
  written; history references are counted in the manifest.
- **Verified before it commits.** Every rewritten file must equal its `HEAD` copy with exactly the planned substitutions,
  each new text must name the archive path, and a rescan of the written tree must find no live reference left to an
  old path. Any failure undoes the run.
- **One commit.** The rewritten files and repointed symlinks are committed with the moves and the manifest. When the
  commit does not land, each referrer is put back, but only while it still holds what the run wrote: an edit someone
  made after the rewrite is left in place and named in the refusal.

The default, `notify`, is the refusal. `keep` is reserved. The command never reads `[checkpoint] cross_owner`: a
repository's configuration does not widen what a manual run writes, so a caller with a policy (a retirement hook, say)
passes the flag itself. Run with `--dry-run` first: it prints a count, then every substitution, one
`rewrite <file>:<line> <old> -> <new>` line each.

What it does not rewrite: references *from* the archived files. A moved file's relative references to files that stay
behind no longer resolve from the archive. Those files are history once archived, and their own references are left
as written, as before.

## The manifest

One JSON object per line, appended, never rewritten:

| field | meaning |
| --- | --- |
| `ts`, `by` | UTC time; who (`--by`, else `$SHOAL_SLUG`, else `user`) |
| `event` | `archive`, `restore` or `cold`, or `migrate` for a file that arrived from a legacy archive folder (`--migrate`) |
| `path`, `archive_path` | the original path, and where the file went |
| `from` | `migrate` only: the legacy path the file came from, which `refs --resolve` follows |
| `reason` | `--reason`, for `archive` and `migrate` |
| `refs` | `archive` and `migrate`: `history`, the number of history references left as written, and `rewritten`, one `{file, line, from, to}` per reference `--cross-owner rewrite` changed (`line` 0 for a symlink; `dir` when the reference named a directory the run emptied) |
| `blob`, `mode` | `cold` only: the file's blob id and git mode; `--restore` reads the blob first, so a binary comes back byte for byte and an executable keeps its bit |
| `restore_from` | `cold` only: a commit that holds `archive_path`: the merge base with the upstream when that holds the file (already published), else HEAD. A shared checkout's HEAD may be a local commit that a commit hook rebases before pushing, which orphans it; the blob survives that rebase, so it is read first |

## Legacy archive folders (`--migrate`)

Before this layout, files were archived by hand into nested folders beside their originals: `handoffs/archive/`,
`<workstream>/archive/`, `handoff_docs/archived/`. `--migrate` moves such files into the layout. Each file's original
path is its legacy path without the deepest `archive`, `archived` or `_archive` segment (never the first segment, nor
the file name): `a/handoffs/archive/x.md` was `a/handoffs/x.md`, so it goes to `archive/a/handoffs/x.md` (the default
layout). A `migrate` event goes in the original directory's manifest, with the legacy path in `from`. A folder argument
stands for every tracked file under it, and subfolders keep their place below the original.

Everything else is an archive run: the same refusals (an untracked, dirty or start-doc file; a live reference, unless
`--cross-owner rewrite`, which points it at the new path), one commit, and the same undo. It also refuses a path with
no legacy segment, a path already under the archive root, two legacy files with the same original in one run, an
original path that is live again (the mirror would then stand for two files), and a mirror that already exists. After the commit lands, a run removes the directories its moves emptied, so a migrated legacy folder,
or a retired role's directory, does not linger on disk. A history document that still names the legacy path resolves
it with `shoal refs --resolve <legacy path>`, which finds the event by its `from`.

## One commit, or nothing

A run commits exactly its own paths: the moved files at both ends, deletions, rewritten referrers, and the manifests.
It uses the repository's `[hooks] commit` when this host trusts the repository (`[lifecycle] trusted_repos`), else
`git commit -- <paths>`. It never stages anything else, so a change another session has staged in the same checkout
stays staged and stays out.

Whether the run landed is decided by content: HEAD must hold every manifest exactly as the run wrote it. That holds
when another session commits meanwhile, and it tells a hook that refused (nothing landed) from one that committed and
then failed later, a rejected push for one (landed, reported as a note). When nothing landed, every move is undone and
each manifest is returned to HEAD's copy, or removed if the run created it.

A refusal shows the hook's (or `git commit`'s) output from its first line that reads as a failure (`Failed`, `REFUSED`,
`refuse`, `error:`, `fatal:`, `exit code: `), stdout and stderr interleaved in the order written, capped at 1,500
characters; with no such line, the last 600 characters. The whole output is kept in a file under the git directory
(`<git dir>/shoal/archive-hook-<utc>.log`), never in the work tree, and the refusal names it. The newest 20 such
logs are kept; older ones are removed when a new one is written. A hook can print its cause
first and pages of accepted notes after it, so the tail alone can omit the cause.

A `[hooks] commit` must commit every path it is given in one commit. Landing is judged by the manifests, so a hook that
committed the manifests but not the moves would count as landed with the moves still staged.
