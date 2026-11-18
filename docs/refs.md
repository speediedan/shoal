# References: what names a role's documents (`shoal refs`)

A role keeps its working memory in one directory: its start doc ([`start-doc.md`](start-doc.md)), the companion log, and the
auxiliary files the start doc links. Files there get written, linked, superseded and forgotten. Before any of them is moved
or archived, something has to say what still depends on each one, because a missing file breaks the next reader silently:
a handover whose start doc is gone, or a relaunch of a parked role whose doc moved. `shoal refs` is that instrument. It is
read-only and makes no model calls.

```bash
shoal refs --roots [--all-hosts] [--json]             # every start doc that live specs and standalone sessions name
shoal refs <dir> [--doc <path>] [--json] [--check]     # what names each tracked file in a role directory
shoal refs --shoal <id> --slug <role> [--check]       # the same for a role: its doc's directory, its doc and brief as roots
shoal refs --resolve <path> [--from <doc>] [--json]   # where a path an old document names is now
```

## Roots

`--roots` lists the start-doc paths shoal itself depends on:

- every role `doc` in every spec under the ledger clone that is not retired, **parked roles included**: an offer relaunches
  a parked role from its doc, so archiving that doc breaks the relaunch;
- every saved standalone session's `doc` (and any other `*_doc` field it carries).

No file in the repository holding these docs says that shoal needs them, so a check inside that repository cannot see them.
The JSON form is a versioned contract for such checks:

```json
{"v": 1, "hosts": ["<host>"], "unreachable": [{"host": "<host>", "reason": "<why>"}], "specs_from": "<ledger clone>",
 "specs_live": 2, "notes": [],
 "roots": {"<absolute path>": [{"kind": "role", "shoal": "<id>", "slug": "<role>", "parked": false},
                               {"kind": "standalone", "label": "<label>", "host": "<host>"}]}}
```

Specs are the same on every host once the ledger syncs, but the standalone registry is per host. Without `--all-hosts` the
answer covers this host's standalone sessions only, and `hosts` says so. With it, each routed host is asked for its own
`refs --roots --json` over its route (cached for two minutes), and the answers are merged: each host reports its own
registry, and its specs from its own clone. A host that cannot answer, or answers in another contract version (a shoal
too old to have `refs`, for one), is listed in `unreachable` with the reason, never dropped silently.

A relative doc resolves against the spec's `dir` (a role) or the entry's `cwd` (a standalone session). One with neither is
left out and named in `notes`, rather than resolved against wherever the command happens to run.

## A role directory

`shoal refs <dir>` classifies each tracked file in the directory (recursively, `archive/` excluded):

| Class | Meaning | Archive candidate |
|---|---|---|
| `root` | a start doc or brief (`--doc`, `--shoal/--slug`, `--root`), any `--roots` path inside the directory, and the target of a root that is a symlink | never |
| `live` | reachable from a root through live documents inside the directory, or named by a live document (or a symlink) anywhere else in the repository | no |
| `history-only` | named only by history documents: the companion log (`*_log.md`), or what `--history` adds | yes |
| `superseded` | its front matter says `status: superseded` and nothing live names it | yes |
| `unreferenced` | nothing names it | yes, once older than `--min-age-days` (default 7, by the commit that added it): a new file is unreferenced until it is linked |

A file whose front matter (its first lines) carries `keep: <reason>` is never a candidate; the reason is reported. For a
file named from another role's directory, the report names that role, so an archiver can tell its owner rather than edit
another role's document.

It also reports three things a checkout or an archive would lose or break, and `--check` exits 1 on any of them:

- **untracked** files in the directory;
- tracked files **deleted** from the working tree and not committed;
- **dangling** references from root and live markdown: a local link that resolves to nothing tracked, or a path that names
  this directory explicitly (repository-relative or absolute) and nothing tracked. A bare relative path is not judged: it is
  as often a path in another repository as one here.

The report says how many live specs the roots came from. When none was found it warns, because then the start docs of
other roles in the directory read as unreferenced. `--no-known-roots` skips the roots on purpose (and says so).

The history and excluded globs and the age floor come from the repository's `.shoal/lifecycle.toml`
([`lifecycle.md`](lifecycle.md)) unless `--history`, `--exclude` or `--min-age-days` name them; the JSON says which were used.
A root's companion log (`<stem>_log.md` beside `<stem>_start*.md` or `<stem>_plan_state.md`, or the only `*_log.md` beside a
bare `plan_state.md`) is live by contract, linked or not. A dangling reference is reported only from a live document: a history document that a live one links still records old
paths as they were.

## What counts as a reference

Any path-shaped token in a text file: a markdown link, a backticked path, a path in prose. It resolves relative to the
naming file (so a bare filename names its sibling), relative to the repository root, or as an absolute path into the
repository. A partial path of two or more components counts when no other tracked path ends with it. A token continued by
`<`, `{`, `*` or `$` is a placeholder and names nothing. Tracked symlinks reference their targets. Paths resolve against the
tracked set (`git ls-files`), never against the disk alone, so a reference that works only because of an untracked file
does not count.

"Text file" means the built-in extensions (`.md`, `.sh`, `.py`, `.yaml`, `.yml`, `.json`, `.jsonl`, `.toml`, `.txt`,
`.cfg`, `.ini`, `.csv`, `.tsv`) plus any the repository lists in `.shoal/lifecycle.toml` `[refs] text_ext`. A token
stops at commas, tabs and quotes, so each field of a data table is a token. A data table larger than `[refs]
data_max_bytes` (4 MiB by default) is not read, so a large tracked dataset does not slow every scan; `refs` lists it
under `skipped`, and `archive` and the deep pass name it in their notes. A directory is named only by a token
written with a `/` (`archive/`, `x/archive`): a bare word would resolve beside the naming file, and nothing tells a
directory name from a word.

`--scope dir` looks for references inside the directory only, which is faster and misses what other directories name.

## Where is it now (`--resolve`)

A history document keeps the paths it was written with, and archival moves files away from them. `--resolve` answers
for one such path, from the archive manifests ([`archive.md`](archive.md)) checked against the tree:

| State | Meaning | Also printed |
|---|---|---|
| `live` | tracked at that path | nothing more |
| `warm` | archived, and its archive path is tracked | the archive path and `shoal archive --restore <path>` |
| `cold` | archived, then deleted from the tree | `git show <blob>`, or `git show <restore_from>:<archive path>` when the blob is gone, and the restore command |
| `restored` | restored and tracked again | the restore event |
| `gone` | deleted outside the archive protocol | `git show <commit>^:<path>`, from the commit that deleted it; the JSON carries `"protocol": "bypassed"` |

With `--from <doc>`, the path is read as written in that document: relative to it, or repository-relative. Without it,
relative to the current directory, then repository-relative. A path no manifest names is looked for as a legacy path (a
`migrate` event's `from`) and as an archive path that has since gone cold, in every tracked manifest. Then as a path
deleted outside the protocol. Last, a partial path of two or more components resolves when exactly one known path ends
with it, as a reference does (an archive path counts as its original).

It exits 0 when the path resolves, `gone` included: a deleting commit is an answer, and whether that deletion bypassed the
protocol is for the repository's own checks to judge. It exits 1 when nothing resolves, or when a manifest event and the
tree disagree (a `warm` event whose archive path is not tracked, say): it reports the disagreement rather than pointing at a
file that is not there. Outside a repository, 2.
