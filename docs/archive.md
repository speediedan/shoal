# Archiving role documents (`shoal archive`)

A role's directory accumulates auxiliary files that stop mattering. Archiving one moves it out of the directory, to a
warm archive in the same repository, without breaking anything that still reads it. Later, a warm file goes cold:
it is deleted from the tree, and git history keeps it. Every step is one command, and every step is undone by one.

```bash
shoal archive <path> [<path> ...] [--reason R] [--by WHO] [--dry-run] [--json]   # move to the warm archive
shoal archive --restore <original path> [--dry-run] [--json]                    # back to where it was
shoal archive --cold <original path> [...] [--force] [--dry-run] [--json]        # warm -> cold
```

Where files go comes from the repository's `.shoal/lifecycle.toml` ([`lifecycle.md`](lifecycle.md)): by default an
archived file goes to `archive/<original path>`, and each archived directory keeps one append-only manifest,
`archive/<original dir>/MANIFEST.jsonl`, so two roles archiving at the same time never write the same file.

## What it refuses

Nothing is moved when any path in the run is:

- not tracked (an untracked file is its owner's to commit or delete), or carries uncommitted changes;
- a start doc that a live spec or a standalone session names, on any routed host ([`refs.md`](refs.md),
  `--roots --all-hosts`). When a routed host cannot answer, the run is refused: a file here could be a start doc there.
  `--force` archives anyway and records that it did;
- named by a live document anywhere in the repository, or by a tracked symlink: that reader would break. Point those
  references elsewhere first, or keep the file. History documents (the repository's history tiers) keep their
  references as written: the manifest is what resolves them.

Files archived in the same run may name each other: a symlink archived together with its target stays paired, and a
moved relative symlink is re-aimed from its new place.

`--cold` refuses a file archived fewer than `[cold] warm_days` ago unless `--force` (the deep pass is what decides when
files go cold), and a file whose archive path a live document still names.

## The manifest

One JSON object per line, appended, never rewritten:

| field | meaning |
| --- | --- |
| `ts`, `by` | UTC time; who (`--by`, else `$SHOAL_SLUG`, else `user`) |
| `event` | `archive`, `restore` or `cold` (`migrate`, for files arriving from a legacy archive folder, is reserved) |
| `path`, `archive_path` | the original path, and where the file went |
| `reason` | `--reason`, for `archive` |
| `blob`, `mode` | `cold` only: the file's blob id and git mode; `--restore` reads the blob first, so a binary comes back byte for byte and an executable keeps its bit |
| `restore_from` | `cold` only: a commit that holds `archive_path`: the merge base with the upstream when that holds the file (already published), else HEAD. A shared checkout's HEAD may be a local commit that a commit hook rebases before pushing, which orphans it; the blob survives that rebase, so it is read first |

## One commit, or nothing

A run commits exactly its own paths: the moved files at both ends, deletions, and the manifests. It uses the repository's
`[hooks] commit` when this host trusts the repository (`[lifecycle] trusted_repos`), else `git commit -- <paths>`. It
never stages anything else, so a change another session has staged in the same checkout stays staged and stays out.

Whether the run landed is decided by content: HEAD must hold every manifest exactly as the run wrote it. That holds
when another session commits meanwhile, and it tells a hook that refused (nothing landed) from one that committed and
then failed later, a rejected push for one (landed, reported as a note). When nothing landed, every move is undone and
each manifest is returned to HEAD's copy, or removed if the run created it.

A `[hooks] commit` must commit every path it is given in one commit. Landing is judged by the manifests, so a hook that
committed the manifests but not the moves would count as landed with the moves still staged.
