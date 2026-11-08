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
```

## Roots

`--roots` lists the start-doc paths shoal itself depends on:

- every role `doc` in every spec under the ledger clone that is not retired, **parked roles included**: an offer relaunches
  a parked role from its doc, so archiving that doc breaks the relaunch;
- every saved standalone session's `doc` (and any other `*_doc` field it carries).

No file in the repository holding these docs says that shoal needs them, so a check inside that repository cannot see them.
The JSON form is a versioned contract for such checks:

```json
{"v": 1, "hosts": ["<host>"], "unreachable": [], "specs_from": "<ledger clone>", "specs_live": 2, "errors": [],
 "roots": {"<absolute path>": [{"kind": "role", "shoal": "<id>", "slug": "<role>", "parked": false},
                               {"kind": "standalone", "label": "<label>", "host": "<host>"}]}}
```

Specs are the same on every host once the ledger syncs, but the standalone registry is per host. Without `--all-hosts` the
answer covers this host's standalone sessions only, and `hosts` says so. With it, each routed host's registry is read
through its route (cached), and a host that does not answer is listed in `unreachable`.

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

## What counts as a reference

Any path-shaped token in a text file: a markdown link, a backticked path, a path in prose. It resolves relative to the
naming file (so a bare filename names its sibling), relative to the repository root, or as an absolute path into the
repository. A partial path of two or more components counts when no other tracked path ends with it. A token continued by
`<`, `{`, `*` or `$` is a placeholder and names nothing. Tracked symlinks reference their targets. Paths resolve against the
tracked set (`git ls-files`), never against the disk alone, so a reference that works only because of an untracked file
does not count.

`--scope dir` looks for references inside the directory only, which is faster and misses what other directories name.
