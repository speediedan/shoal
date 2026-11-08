#!/usr/bin/env python3
"""The reference graph of a role directory, and the start-doc roots this host knows (docs/refs.md). Read-only; no model
calls.

    shoal refs --roots [--all-hosts] [--json]
    shoal refs [<dir>] [--doc PATH | --shoal ID --slug S [--spec P]] [--root PATH ...] [--scope repo|dir]
               [--min-age-days N] [--history GLOB ...] [--exclude GLOB ...] [--json] [--check]

`--roots` lists every start-doc path a live spec (not retired; any role, parked included, since an offer relaunches a
parked role from its doc) or a saved standalone session names. Archiving one of them breaks the next launch or
handover, and no file in the repository holding it says so.

`refs <dir>` classifies each tracked file in a role directory by what names it: `root` (a start doc, a brief, any
`--roots` path inside the directory), `live` (reachable from a root through live documents, or named by a live document
outside the directory), `history-only` (named only by history documents, such as the companion log), `superseded`
(front matter `status: superseded`, nothing live names it), `unreferenced`. It also reports untracked files, tracked
files deleted from the working tree, and dangling references from roots and live files. `--check` exits 1 on any of
those three. The classes are the input of archival: a root or live file is never a candidate.

A reference is any path-shaped token in a text file (a markdown link, a backticked path, plain prose), resolved relative
to the naming file (so a bare filename names its sibling), relative to the repository root, or as an absolute path into
the repository; a partial path of two or more components counts when no other tracked path ends with it. Tracked
symlinks reference their targets. Paths resolve against `git ls-files`, never the disk alone.
"""
from __future__ import annotations

import datetime
import fnmatch
import glob
import json
import os
import posixpath
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import layout  # noqa: E402
import standalone  # noqa: E402

ROOTS_V = 1
REFS_V = 1
TEXT_EXT = {".md", ".sh", ".py", ".yaml", ".yml", ".json", ".jsonl", ".toml", ".txt", ".cfg", ".ini"}
HISTORY = ["*_log.md", "*_log_*.md"]
EXCLUDE = ["archive/*"]
MIN_AGE_DAYS = 7
# the characters a path uses, so link syntax, backticks, quotes and brackets all delimit one
TOKEN = re.compile(r"[A-Za-z0-9_.~/+@%-]*[A-Za-z0-9_/]")
MD_LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
FRONT = re.compile(r"^(status|keep)\s*:\s*(.+?)\s*$", re.I | re.M)


# ---- roots -----------------------------------------------------------------------------

def _spec_files() -> tuple[list[str], str]:
    root = config.ledger_dir()
    files = sorted(glob.glob(os.path.join(root, layout.GROUPS_DIR, "*", layout.SPEC_FILE)))
    pin = config.env("SPEC")
    if pin and os.path.isfile(pin) and os.path.abspath(pin) not in map(os.path.abspath, files):
        files.append(pin)
    return files, root


def _entry_docs(e: dict) -> list[str]:
    """The docs a standalone entry names: `doc`, and any other `*_doc` field (a handover doc)."""
    return [v for k, v in e.items() if (k == "doc" or k.endswith("_doc")) and isinstance(v, str) and v.strip()]


def roots(all_hosts: bool = False) -> dict:
    """{v, hosts, unreachable, specs_from, roots: {abs path: [who, ...]}}. `who` is a dict: kind `role` (shoal, slug,
    parked) or kind `standalone` (label, host). The registry is per host, so without `all_hosts` other hosts' standalone
    seats are not seen; `hosts` says which were."""
    here = config.server()
    out: dict = {"v": ROOTS_V, "hosts": [here], "unreachable": [], "specs_from": "", "specs_live": 0, "errors": [],
                 "roots": {}}

    def add(path: str, base: str, who: dict) -> None:
        p = os.path.expanduser(path)
        p = os.path.normpath(p if os.path.isabs(p) else os.path.join(base or os.getcwd(), p))
        out["roots"].setdefault(p, []).append(who)

    files, out["specs_from"] = _spec_files()
    for f in files:
        try:
            d = json.load(open(f))
        except (OSError, ValueError) as exc:
            out["errors"].append(f"{f}: {exc}")
            continue
        if d.get("retired_utc"):
            continue
        out["specs_live"] += 1
        sid = d.get("shoal") or os.path.basename(os.path.dirname(f))
        for r in d.get("roles", []):
            if r.get("doc"):
                add(r["doc"], d.get("dir", ""), {"kind": "role", "shoal": sid, "slug": r.get("slug", ""),
                                                 "parked": bool(r.get("parked"))})
    seats = [(e, e.get("host") or here) for e in standalone.load_standalone(raw=True)]
    if all_hosts:
        import routes
        for server in sorted(routes.routes()):
            if server == here:
                continue
            got, ok = routes.remote_standalone(server)
            (out["hosts"] if ok else out["unreachable"]).append(server)
            seats += [(e, server) for e in got]
    for e, host in seats:
        for doc in _entry_docs(e):
            add(doc, "", {"kind": "standalone", "label": e.get("label") or str(e.get("session", ""))[:12], "host": host})
    return out


def who_text(w: dict) -> str:
    if w.get("kind") == "role":
        return f"role {w.get('slug')} of {w.get('shoal')}" + (" (parked)" if w.get("parked") else "")
    return f"standalone session {w.get('label')} on {w.get('host')}"


# ---- the repository ----------------------------------------------------------------------

def _git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _matches(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(posixpath.basename(path), g) for g in globs)


class Repo:
    def __init__(self, any_path: str):
        d = any_path if os.path.isdir(any_path) else os.path.dirname(any_path)
        self.root = os.path.realpath(_git(d, "rev-parse", "--show-toplevel").strip())
        self.mode: dict[str, str] = {}
        for rec in _git(self.root, "ls-files", "-s", "-z").split("\0"):
            if rec:
                meta, path = rec.split("\t", 1)
                self.mode[path] = meta.split()[0]
        self.dirs = set()
        for p in self.mode:
            d = posixpath.dirname(p)
            while d and d not in self.dirs:
                self.dirs.add(d)
                d = posixpath.dirname(d)

    def rel(self, path: str) -> str | None:
        p = os.path.expanduser(path)
        if os.path.isabs(p):
            # resolve the directory, never the file: a start doc that is a symlink is named by its own path
            p = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
        if os.path.isabs(p):
            return os.path.relpath(p, self.root) if (p + "/").startswith(self.root + "/") else None
        return posixpath.normpath(p)

    def exists(self, rel: str) -> bool:
        return rel in self.mode or rel in self.dirs

    def read(self, rel: str) -> str:
        try:
            with open(os.path.join(self.root, rel), errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""


def candidates(token: str, here: str, repo: Repo) -> set[str]:
    """Repository-relative paths a token could name from a file in directory `here`."""
    tok = token.split("#", 1)[0].rstrip(".,:;")
    if not tok or "://" in tok or tok.startswith(("http:", "https:", "mailto:")):
        return set()
    if tok.startswith(("/", "~")):
        r = repo.rel(tok)
        return {r.rstrip("/")} if r and not r.startswith("..") else set()
    out = {posixpath.normpath(posixpath.join(here, tok)), posixpath.normpath(tok)}
    return {c.rstrip("/") for c in out if not c.startswith("..") and c != "."}


def scan(directory: str, docs: list[str] | None = None, extra_roots: list[str] | None = None, scope: str = "repo",
         min_age_days: float = MIN_AGE_DAYS, history: list[str] | None = None, exclude: list[str] | None = None,
         known_roots: dict | None = None, now: float | None = None) -> dict:
    repo = Repo(directory)
    d = repo.rel(os.path.abspath(directory))
    if d is None:
        raise RuntimeError(f"{directory} is not inside {repo.root}")
    d = "" if d == "." else d
    history = list(history or HISTORY)
    exclude = list(exclude or EXCLUDE)
    inside = (lambda p: p.startswith(d + "/")) if d else (lambda p: True)

    def tier(p: str) -> str:
        return "excluded" if _matches(p, exclude) else "history" if _matches(p, history) else "live"

    files = sorted(p for p in repo.mode if inside(p) and tier(p) != "excluded")
    root_paths: dict[str, list[str]] = {}
    for doc in docs or []:
        r = repo.rel(os.path.abspath(os.path.expanduser(doc)))
        if r:
            root_paths.setdefault(r, []).append("start doc")
    for x in extra_roots or []:
        r = repo.rel(os.path.abspath(os.path.expanduser(x)))
        if r:
            root_paths.setdefault(r, []).append("--root")
    owners: dict[str, str] = {}
    for p, whos in (known_roots or {}).items():
        r = repo.rel(p)
        if r is None:
            continue
        owners.setdefault(posixpath.dirname(r), who_text(whos[0]))
        if inside(r):
            root_paths.setdefault(r, []).extend(who_text(w) for w in whos)
    # a symlinked root's target is a root too: it is what the session reads
    for r in list(root_paths):
        if repo.mode.get(r) == "120000":
            t = posixpath.normpath(posixpath.join(posixpath.dirname(r), os.readlink(os.path.join(repo.root, r))))
            root_paths.setdefault(t, []).append(f"target of {r}")

    # every way of naming a file in the directory contains its basename, so one search skips the files, lines and
    # tokens that cannot hold a reference before any path is resolved
    # (substring tests, not one regex alternation: Python's re tries every alternative at every position, which cost
    # 10 s on a 55-file directory against a 28 MB repository; `in` takes 1)
    names = sorted({posixpath.basename(p) for p in files})
    tails: dict[str, int] = {}
    for q in repo.mode:
        parts = q.split("/")
        for k in range(2, len(parts) + 1):  # the whole path too, so a root-level `a/b.md` makes `x/a/b.md` ambiguous
            t = "/".join(parts[-k:])
            tails[t] = tails.get(t, 0) + 1
    suffixes = {"/".join(p.split("/")[-k:]): p for p in files for k in range(2, len(p.split("/")))
                if tails.get("/".join(p.split("/")[-k:])) == 1}
    referrers = sorted(p for p in repo.mode if (scope == "repo" or inside(p)) and repo.mode[p] != "120000"
                       and posixpath.splitext(p)[1] in TEXT_EXT and tier(p) != "excluded")
    edges: dict[str, list[dict]] = {p: [] for p in files}
    dangling: list[dict] = []
    in_dir = set(files)
    for src in referrers:
        text = repo.read(src)
        present = [x for x in names if x in text]
        if not present:
            continue
        here = posixpath.dirname(src)
        for n, line in enumerate(text.splitlines(), 1):
            if not any(x in line for x in present):
                continue
            for m in TOKEN.finditer(line):
                if line[m.end():m.end() + 1] in ("<", "{", "*", "$"):
                    continue  # a placeholder or glob continues the token
                tok = m.group(0)
                if not any(x in tok for x in present):
                    continue
                named = candidates(tok, here, repo) & in_dir
                if not named and tok.rstrip("/.,:;") in suffixes:
                    named = {suffixes[tok.rstrip("/.,:;")]}
                for t in named - {src}:
                    edges[t].append({"file": src, "line": n, "tier": tier(src)})
    for p, mode in repo.mode.items():
        if mode == "120000" and tier(p) != "excluded":
            t = posixpath.normpath(posixpath.join(posixpath.dirname(p), os.readlink(os.path.join(repo.root, p))))
            if t in edges and t != p:
                edges[t].append({"file": p, "line": 0, "tier": "symlink"})

    # live: reachable from a root through live documents inside the directory, or named by a live document (or a
    # symlink) outside it
    live = {r for r in root_paths if r in in_dir}
    for t, es in edges.items():
        if any(not inside(e["file"]) and e["tier"] in ("live", "symlink") for e in es):
            live.add(t)
    grew = True
    while grew:
        grew = False
        for t, es in edges.items():
            if t not in live and any(e["file"] in live and e["tier"] in ("live", "symlink") for e in es):
                live.add(t)
                grew = True

    # dangling, from root and live markdown only: an unresolved local markdown link, or a path token that names this
    # directory explicitly (repository-relative or absolute) and nothing tracked. A bare relative path is not judged: it is
    # as often a path in another repository (`config/host.toml`) as one here.
    for src in sorted(live | set(root_paths)):
        if src not in repo.mode or not src.endswith(".md"):
            continue
        here = posixpath.dirname(src)
        for n, line in enumerate(repo.read(src).splitlines(), 1):
            for m in MD_LINK.finditer(line):
                dest = m.group(1)
                if dest.startswith("#") or ":" in dest.split("/", 1)[0] or not re.search(r"\.\w+$|/$", dest.split("#")[0]):
                    continue
                c = candidates(dest, here, repo)
                if c and not any(repo.exists(x) for x in c):
                    dangling.append({"file": src, "line": n, "target": dest})
            for m in TOKEN.finditer(line):
                tok = m.group(0)
                if "/" not in tok or line[m.end():m.end() + 1] in ("<", "{", "*", "$"):
                    continue
                bare = tok.split("#")[0].rstrip(".,:;")
                explicit = candidates(bare, here, repo) if bare.startswith(("/", "~")) else {posixpath.normpath(bare)}
                if d and any(inside(x) for x in explicit) and not any(repo.exists(x) for x in explicit) \
                        and re.search(r"\.\w+$", bare):
                    dangling.append({"file": src, "line": n, "target": tok})

    born = _first_commit_times(repo, d)
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    out_files, candidates_out = [], []
    for p in files:
        text = repo.read(p) if posixpath.splitext(p)[1] in TEXT_EXT else ""
        front = {k.lower(): v for k, v in FRONT.findall("\n".join(text.splitlines()[:12]))}
        es = edges[p]
        if p in root_paths:
            cls = "root"
        elif p in live:
            cls = "live"
        elif front.get("status", "").lower() == "superseded":
            cls = "superseded"
        elif any(e["tier"] == "history" for e in es):
            cls = "history-only"
        else:
            cls = "unreferenced"
        age = (now - born[p]) / 86400 if p in born else None
        rec = {"path": p, "class": cls, "age_days": None if age is None else round(age, 1),
               "referrers": [dict(e, owner=_owner(owners, e["file"], d)) for e in es]}
        if p in root_paths:
            rec["roots"] = root_paths[p]
        if front.get("keep"):
            rec["keep"] = front["keep"]
        candidate = cls in ("history-only", "superseded") or (cls == "unreferenced" and (age is None or age >= min_age_days))
        if candidate and not rec.get("keep"):
            candidates_out.append(p)
        out_files.append(rec)
    untracked = [x for x in _git(repo.root, "ls-files", "-o", "--exclude-standard", "-z", "--", d or ".").split("\0") if x]
    deleted = [x for x in _git(repo.root, "ls-files", "--deleted", "-z", "--", d or ".").split("\0") if x]
    return {"v": REFS_V, "repo": repo.root, "dir": d, "scope": scope, "roots": sorted(root_paths), "files": out_files,
            "candidates": candidates_out, "dangling": dangling, "untracked": untracked, "deleted": deleted,
            "ok": not (dangling or untracked or deleted)}


def _owner(owners: dict[str, str], path: str, d: str) -> str:
    """The role whose start-doc directory holds `path` (the longest such directory), unless it is this one."""
    best = max((od for od in owners if od and od != d and path.startswith(od + "/")), key=len, default=None)
    return owners[best] if best is not None else ""


def _first_commit_times(repo: Repo, d: str) -> dict[str, float]:
    """When each path under `d` was first added (commit time), in one `git log`."""
    out: dict[str, float] = {}
    ts = None
    log = _git(repo.root, "log", "--no-renames", "--diff-filter=A", "--format=@%ct", "--name-only", "--", d or ".")
    for line in log.splitlines():
        if line.startswith("@"):
            ts = float(line[1:])
        elif line.strip() and ts is not None:
            out[line.strip()] = ts  # git log runs newest first, so the last write is the first add
    return out


# ---- the command -------------------------------------------------------------------------

def cmd_refs(a) -> int:
    if a.roots:
        r = roots(a.all_hosts)
        if a.json:
            print(json.dumps(r, indent=1))
            return 0
        print(f"start-doc roots seen from {', '.join(r['hosts'])} (specs from {r['specs_from']})"
              + (f"; unreachable: {', '.join(r['unreachable'])}" if r["unreachable"] else "")
              + ("" if a.all_hosts else "; other hosts' standalone sessions not read (--all-hosts)"))
        for p, whos in sorted(r["roots"].items()):
            print(f"  {p}  ({'; '.join(who_text(w) for w in whos)})")
        for e in r["errors"]:
            print(f"  note: unreadable spec {e}")
        return 0
    docs, directory = list(a.doc or []), a.dir
    if a.slug:
        from roles import ShoalSpec, find_spec, home_dir
        spec = ShoalSpec.load(find_spec(a.sid, a.spec))
        role = spec.role(a.slug)
        if role is None or not role.doc:
            print(f"refs: no start doc for role {a.slug!r} in {spec.sid}")
            return 2
        docs.append(role.doc)
        brief = os.path.join(home_dir(spec.sid), "briefs", f"{role.tag}.md")
        if os.path.isfile(brief):
            docs.append(brief)
        directory = directory or os.path.dirname(os.path.expanduser(role.doc))
    if not directory:
        print("refs: name a directory, or --shoal/--slug for a role, or --roots")
        return 2
    known = roots(False) if not a.no_known_roots else None
    try:
        r = scan(directory, docs, a.root, a.scope, a.min_age_days, a.history or None, a.exclude or None,
                 known_roots=known["roots"] if known else {})
    except RuntimeError as exc:
        print(f"refs: {exc}")
        return 2
    # an empty roots list would make other roles' start docs in this directory look unreferenced: say so, always
    r["known_roots"] = ({"specs_from": known["specs_from"], "specs_live": known["specs_live"], "hosts": known["hosts"]}
                        if known else None)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        print(f"{r['repo']}/{r['dir']}: {len(r['files'])} tracked files, {len(r['candidates'])} archive candidates"
              f"{'' if r['ok'] else ', NOT CLEAN'}")
        k = r["known_roots"]
        if k is None:
            print("  WARNING: --no-known-roots: start docs that specs name are not protected as roots here")
        elif not k["specs_live"]:
            print(f"  WARNING: no live spec found under {k['specs_from']}: roles' start docs in this directory would read as "
                  "unreferenced (is this host's config the one the shoals use?)")
        else:
            print(f"  roots from {k['specs_live']} live specs ({k['specs_from']}) and {', '.join(k['hosts'])}'s standalone sessions")
        order = ("root", "live", "history-only", "superseded", "unreferenced")
        for cls in order:
            group = [f for f in r["files"] if f["class"] == cls]
            if group:
                print(f"  {cls} ({len(group)})")
                for f in group:
                    mark = " [candidate]" if f["path"] in r["candidates"] else f" [keep: {f['keep']}]" if f.get("keep") else ""
                    out = [e for e in f["referrers"] if e.get("owner")]
                    by = f"  named by {out[0]['owner']} ({out[0]['file']})" if out else ""
                    print(f"    {posixpath.relpath(f['path'], r['dir'] or '.')}{mark}{by}")
        for x in r["dangling"]:
            print(f"  dangling: {x['file']}:{x['line']} -> {x['target']}")
        for x in r["untracked"]:
            print(f"  untracked: {x} (a checkout or an archive loses it: commit or delete it)")
        for x in r["deleted"]:
            print(f"  deleted, not committed: {x}")
    return 1 if a.check and not r["ok"] else 0


def add_parser(sub) -> None:
    p = sub.add_parser("refs", help="what names each file of a role directory, and the start-doc roots (read-only)")
    p.add_argument("dir", nargs="?", default="")
    p.add_argument("--roots", action="store_true", help="list the start-doc paths live specs and standalone sessions name")
    p.add_argument("--all-hosts", action="store_true", help="with --roots: also read each routed host's standalone sessions")
    p.add_argument("--doc", action="append", default=[], help="a start doc of the directory (repeatable)")
    p.add_argument("--shoal", dest="sid", default="", help="with --slug: take the role's start doc and brief")
    p.add_argument("--spec", default="")
    p.add_argument("--slug", default="")
    p.add_argument("--root", action="append", default=[], help="another root document (repeatable)")
    p.add_argument("--scope", choices=("repo", "dir"), default="repo",
                   help="look for references in the whole repository (default) or the directory only")
    p.add_argument("--min-age-days", type=float, default=MIN_AGE_DAYS,
                   help="an unreferenced file younger than this is not a candidate yet")
    p.add_argument("--history", action="append", default=[], help=f"a history-document glob (default {HISTORY})")
    p.add_argument("--exclude", action="append", default=[], help=f"a glob not scanned (default {EXCLUDE})")
    p.add_argument("--no-known-roots", action="store_true", help="do not treat --roots paths inside the directory as roots")
    p.add_argument("--json", action="store_true")
    p.add_argument("--check", action="store_true", help="exit 1 on dangling references, untracked or uncommitted deletions")
    p.set_defaults(func=cmd_refs)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    add_parser(ap.add_subparsers(dest="cmd", required=True))
    a = ap.parse_args()
    sys.exit(a.func(a))
