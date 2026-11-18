#!/usr/bin/env python3
"""Reference-aware archival of role documents (docs/archive.md): warm archive, restore, and warm to cold.

    shoal archive <path> [<path> ...] [--reason R] [--by WHO] [--cross-owner notify|rewrite] [--dry-run] [--json]
    shoal archive --restore <original path> [--by WHO] [--dry-run] [--json]
    shoal archive --cold <original path> [<path> ...] [--force] [--by WHO] [--dry-run] [--json]
    shoal archive --migrate <legacy path or folder> [...] [--cross-owner notify|rewrite] [--reason R] [--dry-run]
                  [--json]

`archive` moves tracked files to the repository's archive layout (`.shoal/lifecycle.toml` `[layout] archive`, by default
`archive/<original path>`) and appends one event per file to that directory's manifest (`[layout] manifest`, by default
`archive/<original dir>/MANIFEST.jsonl`). It refuses a path that is not tracked, has uncommitted changes, is a start-doc
root, or is still named by a live document: that is the break this command exists to prevent, so the caller first
points live references elsewhere (or keeps the file). History documents' references stay as written; the manifest is
what resolves them.

`--cross-owner rewrite` points those live references at the archive paths in the same run instead of refusing: a path
substitution only, each reference kept in the form it was written in (relative, repository-relative, absolute, `~/`;
a unique partial path becomes the full archive path), and a tracked symlink repointed. A directory the run empties (every
tracked file under it moves, all to one mirror directory) is a target too, so a reference to the directory itself is
listed under `notify` and rewritten under `rewrite`, keeping a trailing `/`; a directory whose files land in different
places is refused under `rewrite` rather than guessed. Every rewritten file must equal
its `HEAD` copy with exactly those substitutions, and a rescan must find no live reference left to an old path, or the
run is undone. A referrer must be tracked, clean and UTF-8 text; a symlink that is a start doc refuses (its role moves
first). The default, `notify`, is the refusal above; the manual command never reads `[checkpoint] cross_owner`, so a
caller with a policy passes the flag.

`--migrate` moves files out of a legacy nested archive folder (`handoffs/archive/x.md`) to the layout's mirror of the
path they had before (`handoffs/x.md`), with `migrate` events whose `from` is the legacy path; otherwise it is an
archive run. After a run lands, the directories its moves emptied are removed.

`--restore` moves a file back to its original path, from the warm archive or, once cold, from the commit its `cold`
event names. `--cold` deletes a warm file from the tree with a `cold` event whose `restore_from` is the commit that still
holds it; it refuses a file a live document names, and one archived fewer than `[cold] warm_days` ago unless `--force`.

Each run is one commit of exactly its paths (the moved files, their new paths and the manifests), through the
repository's `[hooks] commit` when this host trusts it, else `git commit -- <paths>`: never `git add -A`, because other
sessions commit in the same checkout. If the commit does not land, every move is undone and each manifest is put back as
it was, so a refused hook leaves nothing behind. A refusal shows the hook's output from its first failure line (both
streams, interleaved), and names a file under the git directory that holds all of it. Agent-neutral: shell, git and
files only; no model calls.
"""
from __future__ import annotations

import datetime
import json
import os
import posixpath
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import lifecycle  # noqa: E402
import refs  # noqa: E402

GIT_TIMEOUT_S = 120


class Refused(Exception):
    pass


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise Refused(f"git {' '.join(args)}: no answer in {GIT_TIMEOUT_S} s (a held lock?)") from None
    if check and r.returncode:
        raise Refused(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def _git_bytes(repo: str, *args: str) -> bytes:
    """A git call whose output is file content: bytes, so a binary blob comes back intact."""
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise Refused(f"git {' '.join(args)}: no answer in {GIT_TIMEOUT_S} s (a held lock?)") from None
    if r.returncode:
        raise Refused(f"git {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def _ls_tree(repo: str, rev: str, path: str) -> tuple[str, str]:
    """(mode, blob id) of `path` in `rev`, or ("", "") when it holds no such file."""
    out = _git(repo, "ls-tree", rev, "--", path, check=False).stdout.strip()
    if not out:
        return "", ""
    meta = out.split("\t", 1)[0].split()
    return meta[0], meta[2]


def _durable_holder(repo: str, path: str) -> str:
    """A commit holding `path` that a rebase will not rewrite: the merge base with the upstream when that holds the file
    (it is already published), else HEAD. A shared checkout's HEAD may be a local commit that the commit hook rebases
    before pushing, which would orphan it; the blob id the event also records survives that rebase."""
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).returncode:
        return head
    base = _git(repo, "merge-base", "HEAD", "@{u}", check=False).stdout.strip()
    if base and not _git(repo, "cat-file", "-e", f"{base}:{path}", check=False).returncode:
        return base
    return head


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Plan:
    """One run: its repository and settings, the moves, the manifest lines, and what to commit."""

    def __init__(self, any_path: str):
        self.repo = refs.Repo(any_path)
        self.lc = lifecycle.load(self.repo.root)
        self.layout = self.lc["values"]["layout"]
        self.moves: list[tuple[str, str]] = []           # (from, to), repository-relative
        self.restores: list[tuple[str, str, dict]] = []  # from cold: (archive path, original, {blob, mode})
        self.deletes: list[str] = []
        self.rewrites: dict[str, dict] = {}              # referrer -> {before: its text, subs: [span + "to"]}
        self.relinks: list[tuple[str, str, str]] = []    # (symlink, link text, new link text)
        self.old_new: dict[str, str] = {}                # archived original -> archive path, for the rescan
        self.dirs: frozenset[str] = frozenset()          # the directories among the targets (emptied by the run)
        self.events: dict[str, list[dict]] = {}          # manifest path -> new events
        self.notes: list[str] = []

    def rel(self, path: str) -> str:
        r = self.repo.rel(os.path.abspath(os.path.expanduser(path)))
        if r is None or r.startswith(".."):
            raise Refused(f"{path} is not inside {self.repo.root}")
        return r

    def archive_path(self, rel: str) -> str:
        return posixpath.normpath(self.layout["archive"].format(path=rel, dir=posixpath.dirname(rel)))

    def manifest_path(self, rel: str) -> str:
        return posixpath.normpath(self.layout["manifest"].format(dir=posixpath.dirname(rel) or ".", path=rel))

    def history(self, rel: str) -> list[dict]:
        """Every manifest event about original path `rel`, oldest first (committed and planned)."""
        m = self.manifest_path(rel)
        out = [e for e in read_manifest(os.path.join(self.repo.root, m)) if e.get("path") == rel]
        return out + [e for e in self.events.get(m, []) if e.get("path") == rel]

    def add_event(self, rel: str, event: dict) -> None:
        m = self.manifest_path(rel)
        if m not in self.events and os.path.lexists(os.path.join(self.repo.root, m)):
            # a manifest is appended to and, on failure, put back to HEAD: it must be tracked and clean to begin with
            if m not in self.repo.mode:
                raise Refused(f"{m} exists but is not tracked: commit or remove it first")
            _clean(self, m)
        self.events.setdefault(m, []).append(event)

    def paths(self) -> list[str]:
        out = [p for mv in self.moves for p in mv] + list(self.deletes) + list(self.events)
        out += list(self.rewrites) + [link for link, _, _ in self.relinks]
        # a cold file is no longer in the tree, so only its original path is committed
        out += [o for _, o, _ in self.restores]
        return sorted(set(out))


def read_manifest(path: str) -> list[dict]:
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def _clean(plan: Plan, rel: str) -> None:
    """Refuse a path that is not tracked or carries uncommitted changes: an archive would sweep or strand them."""
    if rel not in plan.repo.mode:
        raise Refused(f"{rel} is not tracked (an untracked file is its owner's to commit or delete first)")
    st = _git(plan.repo.root, "status", "--porcelain", "--", rel).stdout.strip()
    if st:
        raise Refused(f"{rel} has uncommitted changes ({st.split()[0]}): commit or discard them first")


def _spans(plan: Plan, targets: list[str], repo: refs.Repo | None = None, skip: set[str] = frozenset()) -> list[dict]:
    """Every reference to `targets` from outside them (and outside `skip`), by the repository's tiers, excluded tiers
    left out: `refs.reference_spans`, so the refusal and the rewrite read the same references."""
    repo, tiers = repo or plan.repo, plan.lc["values"]["tiers"]

    def tier(p: str) -> str:
        if refs._matches(p, tiers["exclude"]):
            return "excluded"
        return "history" if refs._matches(p, tiers["history"]) else "live"

    out_of, exts = set(targets) | set(skip), refs.text_ext(plan.lc["values"])
    referrers = sorted(p for p in repo.mode if repo.mode[p] != "120000"
                       and posixpath.splitext(p)[1].lower() in exts and tier(p) != "excluded" and p not in out_of)
    referrers, skipped = refs.readable(repo, referrers, plan.lc["values"])
    for p in skipped:
        note = f"{p} not read for references: a data table over [refs] data_max_bytes"
        if note not in plan.notes:
            plan.notes.append(note)
    return [s for s in refs.reference_spans(repo, targets, referrers, tier, plan.dirs) if s["file"] not in out_of]


def _live_referrers(plan: Plan, targets: list[str]) -> dict[str, list[dict]]:
    """{target: [live referrers]} across the repository, by the repository's tiers, plus shoal's own roots."""
    named: dict[str, list[dict]] = {t: [] for t in targets}
    for s in _spans(plan, targets):
        if s["tier"] in ("live", "symlink"):
            named[s["target"]].append({"file": s["file"], "line": s["line"], "tier": s["tier"]})
    return named


def _rewritten(s: dict, new: str) -> str:
    """The text that names `new` in the form `s` named its target in (a `refs.reference_spans` span)."""
    w, old = s["written"], s["target"]
    slash = "/" if w.endswith("/") else ""  # a directory written with its trailing slash keeps it
    if s["form"] == "relative":
        return posixpath.relpath(new, posixpath.dirname(s["file"]) or ".") + slash
    if s["form"] in ("repo", "suffix"):
        return new + slash
    if not w.rstrip("/").endswith("/" + old):
        raise Refused(f"{s['file']}:{s['line']}: cannot rewrite `{w}` in its own form (it does not end with {old})")
    return w.rstrip("/")[:-len(old)] + new + slash


def _relinked(s: dict, new: str, root: str) -> str:
    """A symlink's new link text, in its own form: relative stays relative, absolute stays absolute."""
    if os.path.isabs(s["written"]):
        return os.path.join(root, new)
    return posixpath.relpath(new, posixpath.dirname(s["file"]) or ".")


def _plan_rewrites(plan: Plan, live: list[dict], roots: dict) -> None:
    """Plan the substitution of every live reference (`live`, spans) by its archive path: refuses an ambiguous token,
    a start-doc symlink, and a referrer that is untracked, dirty or not UTF-8 text with `\\n` line endings."""
    by_file: dict[str, list[dict]] = {}
    for s in live:
        if s["ambiguous"]:
            raise Refused(f"{s['file']}:{s['line']}: `{s['written']}` names more than one archived path")
        new = plan.old_new[s["target"]]
        if s["form"] == "symlink":
            if s["file"] in roots:
                raise Refused(f"{s['file']} is a start doc ({'; '.join(refs.who_text(w) for w in roots[s['file']])}) "
                              f"and a symlink to {s['target']}: move that role first")
            _clean(plan, s["file"])
            plan.relinks.append((s["file"], s["written"], _relinked(s, new, plan.repo.root)))
            continue
        by_file.setdefault(s["file"], []).append(dict(s, to=_rewritten(s, new)))
    for f, subs in sorted(by_file.items()):
        _clean(plan, f)
        with open(os.path.join(plan.repo.root, f), "rb") as fh:
            raw = fh.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None or text != plan.repo.read(f):
            raise Refused(f"{f} names an archived path but is not UTF-8 text with \\n line endings: rewrite it by hand")
        subs.sort(key=lambda x: x["start"])
        plan.rewrites[f] = {"before": text, "subs": subs}


def _substitute(text: str, subs: list[dict]) -> str:
    out, pos = [], 0
    for x in subs:
        out += [text[pos:x["start"]], x["to"]]
        pos = x["end"]
    return "".join(out) + text[pos:]


def _verify_rewrites(plan: Plan) -> None:
    """Each rewritten file is its `HEAD` copy with exactly the planned substitutions, each new text names the archive
    path, and a rescan of the written tree finds no live reference left to an old path. Checked independently of the
    writer, so a writer bug is caught here rather than committed."""
    root = plan.repo.root
    for f, rw in plan.rewrites.items():
        head = _git_bytes(root, "cat-file", "blob", f"HEAD:{f}").decode("utf-8", errors="replace")
        with open(os.path.join(root, f), "rb") as fh:
            after = fh.read().decode("utf-8", errors="replace")
        if head != rw["before"]:
            raise Refused(f"{f} changed at HEAD while this run was planning it")
        pos = apos = 0
        for x in rw["subs"]:
            seg = head[pos:x["start"]]
            if after[apos:apos + len(seg)] != seg or head[x["start"]:x["end"]] != x["written"]:
                raise Refused(f"{f}: the rewrite is not a pure path substitution (near line {x['line']})")
            apos += len(seg)
            if after[apos:apos + len(x["to"])] != x["to"]:
                raise Refused(f"{f}:{x['line']}: expected `{x['to']}` where `{x['written']}` was")
            apos, pos = apos + len(x["to"]), x["end"]
        if after[apos:] != head[pos:]:
            raise Refused(f"{f}: the rewrite changed text after its last substitution")
    after_repo = refs.Repo(root)
    for f, rw in plan.rewrites.items():
        for x in rw["subs"]:
            new = plan.old_new[x["target"]]
            if new not in refs.candidates(x["to"], posixpath.dirname(f), after_repo):
                raise Refused(f"{f}:{x['line']}: `{x['to']}` does not name {new}")
    left = [s for s in _spans(plan, list(plan.old_new), after_repo, set(plan.old_new.values()))
            if s["tier"] in ("live", "symlink")]
    if left:
        raise Refused("live references to archived paths remain after the rewrite: "
                      + "; ".join(f"{s['file']}:{s['line']} `{s['written']}`" for s in left[:8]))


LEGACY = ("archive", "archived", "_archive")


def legacy_original(rel: str) -> str | None:
    """The path a file in a legacy nested archive folder had before it was archived there: `rel` without its deepest
    `archive`, `archived` or `_archive` segment (never the first segment, which is a root archive, nor the file name).
    `a/handoffs/archive/x.md` was `a/handoffs/x.md`; None when no such segment exists."""
    parts = rel.split("/")
    for i in range(len(parts) - 2, 0, -1):
        if parts[i] in LEGACY:
            return "/".join(parts[:i] + parts[i + 1:])
    return None


def _expand(plan: Plan, paths: list[str]) -> list[str]:
    """Repository-relative paths, a directory standing for every tracked file under it."""
    out: list[str] = []
    for p in paths:
        r = plan.rel(p)
        under = sorted(q for q in plan.repo.mode if q.startswith(r.rstrip("/") + "/"))
        out += under if r not in plan.repo.mode and under else [r]
    return out


def _emptied(plan: Plan, rels: list[str]) -> tuple[dict[str, str], list[str]]:
    """The directories a run empties: ({directory: its destination}, [directories whose files land in different
    places]). A directory is emptied when every tracked file under it is in `rels`; its destination is the one directory
    every file's destination shares with the file's path below it, as `plan.old_new` maps them."""
    moving = set(rels)
    dirs = {d for r in rels for d in _ancestors(r)}
    whole, split = {}, []
    for d in sorted(dirs):
        under = [q for q in plan.repo.mode if q.startswith(d + "/")]
        if not under or any(q not in moving for q in under):
            continue
        dests = set()
        for q in under:
            tail, new = q[len(d):], plan.old_new[q]
            dests.add(new[:-len(tail)] if new.endswith(tail) else None)
        if len(dests) == 1 and None not in dests:
            whole[d] = dests.pop()
        else:
            split.append(d)
    return whole, split


def _ancestors(rel: str) -> list[str]:
    out, d = [], posixpath.dirname(rel)
    while d:
        out.append(d)
        d = posixpath.dirname(d)
    return out


def plan_archive(paths: list[str], reason: str, by: str, force: bool = False, cross_owner: str = "notify",
                 migrate: bool = False, strict: bool = False) -> Plan:
    """Archive `paths`; with `migrate`, move files out of a legacy nested archive folder into the layout's mirror of
    their original path (`legacy_original`), recording `migrate` events whose `from` is the legacy path."""
    plan = Plan(paths[0])
    rels = _expand(plan, paths) if migrate else [plan.rel(p) for p in paths]
    origin: dict[str, str] = {}  # migrate: legacy path -> original path
    top = plan.layout["archive"].split("{", 1)[0].rstrip("/")
    for r in rels:
        if migrate:
            if top and (r == top or r.startswith(top + "/")):
                raise Refused(f"{r} is already under the archive root {top}/: not a legacy folder to migrate")
            o = legacy_original(r)
            if o is None:
                raise Refused(f"{r} is not in a legacy archive folder (no {', '.join(LEGACY)} segment to drop)")
            if o in plan.repo.mode or os.path.lexists(os.path.join(plan.repo.root, o)):
                raise Refused(f"{r}: its original path {o} is live again, so the mirror would mean two files")
            twin = next((x for x, y in origin.items() if y == o), None)
            if twin:
                raise Refused(f"{twin} and {r} both had the original path {o}: migrate one, and rename or keep the other")
            origin[r] = o
        _clean(plan, r)
        a = plan.archive_path(origin.get(r, r))
        if a == r or a.startswith("..") or os.path.isabs(a):
            raise Refused(f"{r}: the layout maps it to {a!r}, outside the archive")
        if a in plan.repo.mode or os.path.lexists(os.path.join(plan.repo.root, a)):
            raise Refused(f"{r}: {a} already exists in the archive")
    plan.old_new = {r: plan.archive_path(origin.get(r, r)) for r in rels}
    # every host's roots: a document here may be another host's standalone seat's start doc, which no file here names
    known = refs.roots(all_hosts=True)
    if known["unreachable"]:
        silent = "; ".join(f"{u['host']} ({u['reason']})" for u in known["unreachable"])
        if not force:
            raise Refused(f"cannot read the start-doc roots of {silent}, so a file here could be a start doc there: "
                          "--force to archive anyway")
        plan.notes.append(f"archived without the roots of {silent} (--force)")
    roots = {plan.repo.rel(p): whos for p, whos in known["roots"].items()}
    for r, whos in list(roots.items()):
        # a start doc named through a symlink is its target too: the session reads the target
        if r and plan.repo.mode.get(r) == "120000":
            t = posixpath.normpath(posixpath.join(posixpath.dirname(r), os.readlink(os.path.join(plan.repo.root, r))))
            roots.setdefault(t, whos)
    for r in rels:
        if r in roots:
            raise Refused(f"{r} is a start doc ({'; '.join(refs.who_text(w) for w in roots[r])}): retire or move the role first")
    if cross_owner not in ("notify", "rewrite"):
        raise Refused(f"--cross-owner {cross_owner} is not defined yet: notify (refuse and list) or rewrite")
    # a directory the run empties is named like a file: a reference to it would dangle as surely as one to a moved file
    emptied, split = _emptied(plan, rels)
    plan.old_new.update(emptied)
    plan.dirs = frozenset(emptied) | frozenset(split)
    spans = _spans(plan, rels + sorted(emptied) + split)
    live = [s for s in spans if s["tier"] in ("live", "symlink")]
    stranded = [s for s in live if s["target"] in split]
    if stranded and cross_owner == "rewrite":
        raise Refused("live documents name a directory whose files this run moves to different places, so no one path "
                      "replaces it:" + "".join(f"\n    {s['file']}:{s['line']} `{s['written']}`" for s in stranded[:8])
                      + "\n  point those references at the files, or move the directory's files together")
    if live and cross_owner != "rewrite":
        blocked: dict[str, list[dict]] = {}
        for s in live:
            blocked.setdefault(s["target"], []).append(s)
        lines = [f"{t} is still named by live documents:" + "".join(f"\n    {e['file']}:{e['line']}" for e in es[:8])
                 for t, es in sorted(blocked.items())]
        raise Refused("\n  ".join(lines) + "\n  point those references elsewhere (or keep the file), or "
                      "--cross-owner rewrite, then archive it")
    suffix = [s for s in live if s.get("form") == "suffix"]
    if suffix and strict and cross_owner == "rewrite":
        # a unique partial path can be prose about another repository's file; an unattended run lists it for a person
        raise Refused("--strict: these references name an archived path only by a partial path, so they may mean "
                      "something else; check and fix them by hand, then run again:"
                      + "".join(f"\n    {s['file']}:{s['line']} `{s['written']}`" for s in suffix[:8]))
    if live:
        _plan_rewrites(plan, live, roots)
    when = utcnow()
    for r in rels:
        a = plan.old_new[r]
        plan.moves.append((r, a))
        rewritten = [{"file": x["file"], "line": x["line"], "from": x["written"], "to": x["to"]}
                     for f, rw in sorted(plan.rewrites.items()) for x in rw["subs"] if x["target"] == r]
        rewritten += [{"file": link, "line": 0, "from": old, "to": new} for link, old, new in plan.relinks
                      if posixpath.normpath(posixpath.join(posixpath.dirname(link), old)) == r]
        history = sum(1 for s in spans if s["tier"] == "history" and s["target"] == r)
        # an emptied directory's references are recorded once, with the first file moved out of it
        for d in sorted(emptied):
            if r == min(q for q in rels if q.startswith(d + "/")):
                rewritten += [{"file": x["file"], "line": x["line"], "from": x["written"], "to": x["to"], "dir": d}
                              for f, rw in sorted(plan.rewrites.items()) for x in rw["subs"] if x["target"] == d]
                rewritten += [{"file": link, "line": 0, "from": old, "to": new, "dir": d}
                              for link, old, new in plan.relinks
                              if posixpath.normpath(posixpath.join(posixpath.dirname(link), old)) == d]
        history += sum(1 for s in spans if s["tier"] == "history" and s["target"] in emptied
                       and r == min(q for q in rels if q.startswith(s["target"] + "/")))
        o = origin.get(r, r)
        plan.add_event(o, {"ts": when, "by": by, "event": "migrate" if migrate else "archive", "path": o,
                           **({"from": r} if migrate else {}), "archive_path": a, "reason": reason,
                           "refs": {"history": history, "rewritten": rewritten}})
    return plan


def plan_restore(path: str, by: str) -> Plan:
    plan = Plan(path)
    r = plan.rel(path)
    if r in plan.repo.mode or os.path.lexists(os.path.join(plan.repo.root, r)):
        raise Refused(f"{r} exists: nothing to restore")
    hist = [e for e in plan.history(r) if e.get("event") in ("archive", "migrate", "restore", "cold")]
    if not hist or hist[-1]["event"] == "restore":
        raise Refused(f"{r} is not in the archive (no manifest event in {plan.manifest_path(r)})")
    last, a = hist[-1], hist[-1]["archive_path"]
    if last["event"] == "cold":
        # the blob first: it survives the rebase that can orphan restore_from's commit; then restore_from
        blob, mode, sha = last.get("blob", ""), last.get("mode", ""), last.get("restore_from", "")
        if blob and not _git(plan.repo.root, "cat-file", "-e", blob, check=False).returncode:
            src = {"blob": blob, "mode": mode or "100644"}
        else:
            m, b = _ls_tree(plan.repo.root, sha, a) if sha else ("", "")
            if not b:
                raise Refused(f"{r}: neither its cold event's blob {blob[:12]!r} nor restore_from {sha[:12]!r} holds {a}")
            src = {"blob": b, "mode": m}
        plan.restores.append((a, r, src))
    else:
        if a not in plan.repo.mode:
            raise Refused(f"{r}: {a} is not tracked, and no cold event says where it went")
        _clean(plan, a)
        plan.moves.append((a, r))
    plan.add_event(r, {"ts": utcnow(), "by": by, "event": "restore", "path": r, "archive_path": a})
    return plan


def _manifests(plan: Plan) -> list[str]:
    """Every tracked manifest: files named as the layout's manifest template names them, under its archive root."""
    name = posixpath.basename(plan.layout["manifest"])
    top = plan.layout["archive"].split("{", 1)[0].rstrip("/")
    return sorted(p for p in plan.repo.mode if posixpath.basename(p) == name and (not top or p.startswith(top + "/")))


def _events(plan: Plan, rel: str) -> list[dict]:
    return [e for e in plan.history(rel) if e.get("event") in ("archive", "migrate", "restore", "cold")]


def _state(plan: Plan, rel: str, hist: list[dict]) -> dict:
    """Where original path `rel` is now, from its manifest events (oldest first) checked against the tree."""
    root, tracked = plan.repo.root, rel in plan.repo.mode
    last = hist[-1] if hist else None
    restore = f"shoal archive --restore {shlex.quote(rel)}"
    if tracked:
        out = {"state": "restored" if last and last["event"] == "restore" else "live", "ok": True,
               **({"event": last} if last else {})}
        if last and last["event"] != "restore":
            # recreated by hand after it was archived: say why the manifest still lists it
            a = last.get("archive_path", "")
            out["note"] = (f"tracked again without a restore event; its last manifest event is {last['event']} "
                           f"{last.get('ts', '')}" + (f" (archive path {a})" if a else ""))
        return out
    if last and last["event"] in ("archive", "migrate"):
        a = last["archive_path"]
        if a not in plan.repo.mode:
            return {"state": "inconsistent", "ok": False, "event": last,
                    "problem": f"its {last['event']} event says {a}, which is not tracked"}
        return {"state": "warm", "ok": True, "archive_path": a, "event": last, "restore": restore}
    if last and last["event"] == "cold":
        a, blob, sha = last["archive_path"], last.get("blob", ""), last.get("restore_from", "")
        if a in plan.repo.mode:
            return {"state": "inconsistent", "ok": False, "event": last,
                    "problem": f"its cold event says {a} left the tree, but it is tracked"}
        if blob and not _git(root, "cat-file", "-e", blob, check=False).returncode:
            recover = f"git show {blob}"
        elif sha and _ls_tree(root, sha, a)[1]:
            recover = f"git show {sha}:{shlex.quote(a)}"
        else:
            return {"state": "inconsistent", "ok": False, "event": last,
                    "problem": f"neither its blob {blob[:12]!r} nor restore_from {sha[:12]!r} holds {a}"}
        return {"state": "cold", "ok": True, "archive_path": a, "event": last, "recover": recover, "restore": restore}
    # no event, or a restored file deleted since: removed outside the archive protocol, or never here
    sha = _git(root, "log", "-1", "--format=%H", "--diff-filter=D", "--", f":(literal){rel}", check=False).stdout.strip()
    if sha:
        return {"state": "gone", "ok": True, "protocol": "bypassed", "deleted_in": sha,
                "recover": f"git show {sha}^:{shlex.quote(rel)}", **({"event": last} if last else {})}
    return {"state": "unknown", "ok": False, "problem": "not tracked, no manifest event and no commit deleting it"}


def resolve(path: str, from_doc: str = "") -> dict:
    """Where a path a document names is now: live, warm, cold, restored, or gone (deleted outside the archive protocol).

    With `from_doc`, `path` is read as written in that document (relative to it, or repository-relative), as `shoal refs`
    reads references; without, relative to the current directory, then repository-relative. A path no manifest names is
    then looked for as a legacy `from` or an `archive_path` in every tracked manifest, then as a path deleted outside the
    protocol, and last as a unique suffix of a tracked or archived path. The result's `ok` is false when nothing resolves or the manifest and the tree disagree."""
    base = os.path.expanduser(from_doc) if from_doc else os.getcwd()
    anchor = base if os.path.isdir(base) else os.path.dirname(os.path.abspath(base))
    if os.path.isabs(os.path.expanduser(path)) and not from_doc:
        anchor = os.path.expanduser(path)
        while anchor and not os.path.isdir(anchor):
            anchor = os.path.dirname(anchor)
    try:
        plan = Plan(anchor)
    except RuntimeError as exc:
        raise Refused(str(exc)) from None
    r = plan.repo.rel(os.path.abspath(base) if from_doc else os.path.join(anchor, "x"))
    here = posixpath.dirname(r) if r and not r.startswith("..") else ""
    cands = refs.candidates(path, here, plan.repo)
    order = sorted(cands, key=lambda c: c != posixpath.normpath(posixpath.join(here, path.split("#", 1)[0])))
    out = {"query": path, "from": from_doc, "repo": plan.repo.root}
    for c in order:
        hist = _events(plan, c)
        if c in plan.repo.mode or hist:
            return {**out, "path": c, "via": "path", **_state(plan, c, hist)}
    # a legacy path (a migrate event's `from`) or an archive path that went cold: find the event that names it
    events = [e for m in _manifests(plan) for e in read_manifest(os.path.join(plan.repo.root, m))]
    for c in order:
        for field in ("from", "archive_path"):
            hit = [e for e in events if e.get(field) == c and e.get("path")]
            if hit:
                p = hit[-1]["path"]
                hist = _events(plan, p)
                return {**out, "path": p, "via": field, **_state(plan, p, hist)}
    # the path itself, deleted outside the protocol: before a partial-path match, which could name another file
    for c in order:
        st = _state(plan, c, [])
        if st["state"] == "gone":
            return {**out, "path": c, "via": "path", **st}
    # a unique partial path, as `refs` reads one (two or more segments, not relative): of a tracked file, or of an
    # original path an event names
    tok = path.split("#", 1)[0].rstrip("/.,:;")
    known = set(plan.repo.mode) | {e["path"] for e in events if e.get("path")}
    original = {e["archive_path"]: e["path"] for e in events if e.get("archive_path") and e.get("path")}
    hits = (sorted({original.get(p, p) for p in known if p.endswith("/" + tok)})  # an archive path is its original
            if "/" in tok and not tok.startswith((".", "/", "~")) else [])
    if len(hits) == 1:
        p = hits[0]
        hist = _events(plan, p)
        return {**out, "path": p, "via": "suffix", **_state(plan, p, hist)}
    return {**out, "path": order[0] if order else path, "via": "path", "state": "unknown", "ok": False,
            "problem": (f"{len(hits)} paths end in {tok}: {', '.join(hits[:5])}" if len(hits) > 1 else
                        "not tracked, named by no manifest event, and no commit deleting it")}


def resolve_text(r: dict) -> list[str]:
    head = f"{r['path']}: {r['state']}"
    if r.get("via") not in (None, "path"):
        head += f" (found by its {r['via'].replace('_', ' ')} {r['query']})"
    lines = [head]
    e = r.get("event") or {}
    if r["state"] == "warm":
        lines.append(f"  at {r['archive_path']} ({e['event']} {e.get('ts', '')}"
                     + (f", reason: {e['reason']}" if e.get("reason") else "") + ")")
    if r["state"] == "cold":
        lines.append(f"  left the tree {e.get('ts', '')}; was at {r['archive_path']}")
    if r["state"] == "restored":
        lines.append(f"  restored {e.get('ts', '')}")
    if r["state"] == "gone":
        lines.append(f"  deleted outside the archive protocol in {r['deleted_in'][:12]}")
    if r.get("recover"):
        lines.append(f"  read it: {r['recover']}")
    if r.get("restore"):
        lines.append(f"  restore: {r['restore']}")
    if r.get("note"):
        lines.append(f"  note: {r['note']}")
    if r.get("problem"):
        lines.append(f"  problem: {r['problem']}")
    return lines


def plan_cold(paths: list[str], force: bool, by: str) -> Plan:
    plan = Plan(paths[0])
    warm_days = plan.lc["values"]["cold"]["warm_days"]
    now = datetime.datetime.now(datetime.timezone.utc)
    targets = []
    for p in paths:
        r = plan.rel(p)
        hist = [e for e in plan.history(r) if e.get("event") in ("archive", "migrate", "restore", "cold")]
        if not hist or hist[-1]["event"] not in ("archive", "migrate"):
            raise Refused(f"{r} is not warm in the archive")
        a = hist[-1]["archive_path"]
        _clean(plan, a)
        try:
            age = (now - datetime.datetime.strptime(hist[-1]["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)).days
        except (KeyError, ValueError):
            age = None
        if not force and (age is None or age < warm_days):
            raise Refused(f"{r} was archived {age if age is not None else '?'} days ago, fewer than [cold] warm_days "
                          f"({warm_days}): --force to go cold now")
        targets.append((r, a))
    named = _live_referrers(plan, [a for _, a in targets])
    blocked = {t: es for t, es in named.items() if es}
    if blocked:
        raise Refused("still named by live documents: " + "; ".join(
            f"{t} ({', '.join(e['file'] for e in es[:4])})" for t, es in sorted(blocked.items())))
    for r, a in targets:
        mode, blob = _ls_tree(plan.repo.root, "HEAD", a)
        plan.deletes.append(a)
        plan.add_event(r, {"ts": utcnow(), "by": by, "event": "cold", "path": r, "archive_path": a,
                           "restore_from": _durable_holder(plan.repo.root, a), "blob": blob, "mode": mode})
    return plan


def _landed(plan: Plan, written: dict[str, str]) -> bool:
    """True when HEAD holds every manifest exactly as this run wrote it: the run's commit is in, whoever else committed
    meanwhile (a shared checkout moves under a concurrent rebase, so "HEAD moved" proves nothing)."""
    for m, text in written.items():
        r = _git(plan.repo.root, "cat-file", "blob", f"HEAD:{m}", check=False)
        if r.returncode or r.stdout != text:
            return False
    return True


def _commit(plan: Plan, message: str, written: dict[str, str]) -> str:
    """Commit exactly the plan's paths, through the trusted hook or `git commit --`; returns the new HEAD. A hook that
    fails after its commit landed (a push rejected after the commit, say) is a landed run with a note, not a refusal."""
    root, paths = plan.repo.root, plan.paths()
    hook = plan.lc["values"]["hooks"]["commit"] if plan.lc["trusted"] else ""
    why = ""
    try:
        if hook:
            # only the two placeholders are replaced, so any other brace in the hook (${VAR}, awk's {print}) is left as
            # written; each expands already shell-quoted, so the template writes them unquoted
            cmd = hook.replace("{message}", shlex.quote(message)).replace("{paths}", " ".join(shlex.quote(p) for p in paths))
            argv, what = ["bash", "-c", cmd], "the commit hook exited"
        else:
            argv, what = ["git", "-C", root, "commit", "-q", "-m", message, "--", *paths], "git commit exited"
        # both streams in one, in the order written: a pre-commit hook reports a failure on stdout and its notes on stderr
        r = subprocess.run(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
        if r.returncode:
            why = f"{what} {r.returncode}: {_failure_excerpt(r.stdout)}{_keep_output(root, r.stdout)}"
    except subprocess.TimeoutExpired:
        why = "the commit hook did not finish in 600 s"
    if not _landed(plan, written):
        raise Refused(why or "the commit did not land")
    if why:
        plan.notes.append(f"committed, but {why}")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


FAILURE_MARK = ("Failed", "REFUSED", "refuse", "error:", "fatal:", "exit code: ")
EXCERPT_MAX = 1500
KEEP_OUTPUTS = 20  # refused-hook logs kept under the git directory; older ones are removed when a new one is written


def _failure_excerpt(out: str) -> str:
    """The hook's output from its first line that reads as a failure, capped; the tail when no line does. A hook can
    print its cause first and pages of accepted notes after it, so the tail alone can omit the cause."""
    lines = out.strip().splitlines()
    at = next((i for i, ln in enumerate(lines) if any(m in ln for m in FAILURE_MARK)), None)
    if at is None:
        return out.strip()[-600:]
    text = "\n".join(lines[at:])
    return text if len(text) <= EXCERPT_MAX else text[:EXCERPT_MAX] + " [...]"


def _keep_output(root: str, out: str) -> str:
    """Write the whole output under the git directory (never the work tree), keep the newest KEEP_OUTPUTS such logs,
    and say where; empty if it cannot."""
    try:
        path = _git(root, "rev-parse", "--path-format=absolute", "--git-path",
                    f"shoal/archive-hook-{utcnow().replace(':', '')}.log").stdout.strip()
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(out)
        # the names sort by time (a UTC stamp), so the oldest are first
        logs = sorted(f for f in os.listdir(d) if f.startswith("archive-hook-") and f.endswith(".log"))
        for f in logs[:-KEEP_OUTPUTS]:
            os.remove(os.path.join(d, f))
        return f"\n  (full output: {path})"
    except (OSError, Refused):
        return ""


def apply(plan: Plan, message: str) -> dict:
    """Do the plan and commit it; on any failure before the commit lands, undo every step. Returns the new HEAD."""
    root = plan.repo.root
    saved = {m: open(os.path.join(root, m)).read() if os.path.exists(os.path.join(root, m)) else None for m in plan.events}
    written: dict[str, str] = {}
    done: list[tuple] = []
    try:
        moved = dict(plan.moves)
        for src, dst in plan.moves:
            os.makedirs(os.path.join(root, posixpath.dirname(dst)), exist_ok=True)
            _git(root, "mv", "--", src, dst)
            done.append(("mv", src, dst, moved))
            _repoint_symlink(root, src, dst, moved)
        for a, original, src in plan.restores:
            data = _git_bytes(root, "cat-file", "blob", src["blob"])
            dest = os.path.join(root, original)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if src["mode"] == "120000":
                os.symlink(data.decode(), dest)
            else:
                with open(dest, "wb") as fh:
                    fh.write(data)
                if src["mode"] == "100755":
                    os.chmod(dest, os.stat(dest).st_mode | 0o111)
            _git(root, "add", "--", original)
            done.append(("add", original))
        for a in plan.deletes:
            _git(root, "rm", "-q", "--", a)
            done.append(("rm", a))
        for f, rw in plan.rewrites.items():
            p = os.path.join(root, f)
            with open(p, "rb") as fh:
                if fh.read() != rw["before"].encode("utf-8"):
                    raise Refused(f"{f} changed after this run planned its rewrite: run it again")
            after = _substitute(rw["before"], rw["subs"]).encode("utf-8")
            with open(p, "wb") as fh:
                fh.write(after)
            _git(root, "add", "--", f)
            done.append(("rewrite", f, rw["before"].encode("utf-8"), after))
        for link, old, new in plan.relinks:
            p = os.path.join(root, link)
            if os.readlink(p) != old:
                raise Refused(f"{link} changed after this run planned to repoint it: run it again")
            os.remove(p)
            os.symlink(new, p)
            _git(root, "add", "--", link)
            done.append(("relink", link, old, new))
        for m, events in plan.events.items():
            os.makedirs(os.path.join(root, posixpath.dirname(m)), exist_ok=True)
            base = saved[m] or ""
            written[m] = (base + ("" if not base or base.endswith("\n") else "\n")
                          + "".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
            with open(os.path.join(root, m), "w") as fh:
                fh.write(written[m])
            _git(root, "add", "--", m)
            done.append(("manifest", m, saved[m] is not None))
        if plan.rewrites or plan.relinks:
            _verify_rewrites(plan)
        head = _commit(plan, message, written)
        for src, _ in plan.moves:
            # git leaves the directories a move emptied (a migrated legacy folder, a retired role's): remove them
            _prune_empty(root, posixpath.dirname(src))
        return {"ok": True, "commit": head, "notes": plan.notes}
    except Exception as exc:
        left = _undo(root, done)
        why = str(exc) if isinstance(exc, Refused) else f"{exc.__class__.__name__}: {exc}"
        raise Refused(why + "".join(f"\n  {x}" for x in left)) from None


def _undo(root: str, done: list[tuple]) -> list[str]:
    """Put every step back, newest first: moves reversed, restored files removed, deletions restored, each manifest
    back to HEAD's copy (it was clean when the run began) or removed when the run created it, and each rewritten file or
    repointed symlink back as it was, but only while it still holds what this run wrote: someone else's edit since is
    left in place and named in the returned notes."""
    left = []
    for step in reversed(done):
        if step[0] == "rewrite":
            p = os.path.join(root, step[1])
            with open(p, "rb") as fh:
                ours = fh.read() == step[3]
            if ours:
                with open(p, "wb") as fh:
                    fh.write(step[2])
                _git(root, "reset", "-q", "HEAD", "--", step[1], check=False)
            else:
                left.append(f"left {step[1]} as found: it changed after this run rewrote it")
        elif step[0] == "relink":
            p = os.path.join(root, step[1])
            if os.path.islink(p) and os.readlink(p) == step[3]:
                os.remove(p)
                os.symlink(step[2], p)
                _git(root, "reset", "-q", "HEAD", "--", step[1], check=False)
            else:
                left.append(f"left {step[1]} as found: it changed after this run repointed it")
        elif step[0] == "mv":
            _git(root, "mv", "-f", "--", step[2], step[1], check=False)
            _repoint_symlink(root, step[2], step[1], {v: k for k, v in step[3].items()})
            _prune_empty(root, posixpath.dirname(step[2]))
        elif step[0] == "add":
            _git(root, "rm", "-q", "-f", "--", step[1], check=False)
        elif step[0] == "rm":
            _git(root, "reset", "-q", "HEAD", "--", step[1], check=False)
            _git(root, "checkout", "HEAD", "--", step[1], check=False)
        elif step[0] == "manifest":
            if step[2]:
                _git(root, "reset", "-q", "HEAD", "--", step[1], check=False)
                _git(root, "checkout", "HEAD", "--", step[1], check=False)
            else:
                _git(root, "rm", "-q", "--cached", "-f", "--", step[1], check=False)
                p = os.path.join(root, step[1])
                if os.path.exists(p):
                    os.remove(p)
    return left


def _prune_empty(root: str, rel_dir: str) -> None:
    """Remove `rel_dir` and its parents while they are empty: the directories a run's first move into them created."""
    while rel_dir and rel_dir != ".":
        try:
            os.rmdir(os.path.join(root, rel_dir))
        except OSError:
            return
        rel_dir = posixpath.dirname(rel_dir)


def _repoint_symlink(root: str, src: str, dst: str, moved: dict[str, str] | None = None) -> None:
    """A moved relative symlink still names its target relative to where it was: aim it from where it is now, at the
    target's new place when the same run moves the target too (`moved`: from -> to)."""
    p = os.path.join(root, dst)
    if not os.path.islink(p):
        return
    target = os.readlink(p)
    if os.path.isabs(target):
        return
    real = posixpath.normpath(posixpath.join(posixpath.dirname(src), target))
    real = (moved or {}).get(real, real)
    new = posixpath.relpath(real, posixpath.dirname(dst) or ".")
    if new != target:
        os.remove(p)
        os.symlink(new, p)
        _git(root, "add", "--", dst, check=False)


def describe(plan: Plan) -> list[str]:
    out = [f"move {a} -> {b}" for a, b in plan.moves]
    out += [f"restore {o} from blob {src['blob'][:12]} (mode {src['mode']})" for _, o, src in plan.restores]
    out += [f"delete {a} (cold)" for a in plan.deletes]
    out += [f"rewrite {f}:{x['line']} `{x['written']}` -> `{x['to']}`" for f, rw in plan.rewrites.items() for x in rw["subs"]]
    out += [f"repoint symlink {link}: {old} -> {new}" for link, old, new in plan.relinks]
    out += [f"append {len(es)} event(s) to {m}" for m, es in plan.events.items()]
    return out


def cmd_archive(a) -> int:
    by = a.by or os.environ.get("SHOAL_SLUG") or "user"
    try:
        if a.cross_owner != "notify" and (a.restore or a.cold):
            raise Refused(f"--cross-owner {a.cross_owner} applies to archiving only")
        if a.migrate and (a.restore or a.cold):
            raise Refused("--migrate moves legacy archive folders into the layout: not with --restore or --cold")
        if a.restore:
            plan, verb = plan_restore(a.restore, by), "restore"
        elif not a.paths:
            print("archive: name the files to archive, --restore <path> or --cold <path>")
            return 2
        elif a.cold:
            plan, verb = plan_cold(a.paths, a.force, by), "cold"
        elif a.migrate:
            plan, verb = plan_archive(a.paths, a.reason, by, a.force, a.cross_owner, migrate=True, strict=a.strict), "migrate"
        else:
            plan, verb = plan_archive(a.paths, a.reason, by, a.force, a.cross_owner, strict=a.strict), "archive"
        steps = describe(plan)
        n = sum(len(rw["subs"]) for rw in plan.rewrites.values()) + len(plan.relinks)
        files = len(plan.rewrites) + len(plan.relinks)
        if n:
            steps.insert(0, f"rewrite {n} reference(s) in {files} file(s), listed below")
        if a.dry_run:
            out = {"ok": True, "dry_run": True, "steps": steps, "events": plan.events, "notes": plan.notes}
        else:
            what = ", ".join(sorted({e["path"] for es in plan.events.values() for e in es}))
            message = (f"{verb}: {what}" + (f" ({a.reason})" if a.reason and verb == "archive" else "")
                       + (f"; rewrote {n} reference(s) in {files} file(s)" if n else ""))
            out = dict(apply(plan, message), steps=steps, events=plan.events)
    except Refused as exc:
        if a.json:
            print(json.dumps({"ok": False, "refused": str(exc)}))
        else:
            print(f"REFUSED: {exc}")
        return 1
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        for s in steps:
            print(("[dry-run] would " if a.dry_run else "") + s)
        if not a.dry_run:
            print(f"committed {out['commit'][:12]}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("archive", help="move role documents to the warm archive (reference-aware), restore them, or "
                                       "send warm ones cold")
    p.add_argument("paths", nargs="*")
    p.add_argument("--restore", default="", metavar="PATH", help="move an archived file back to its original path")
    p.add_argument("--cold", action="store_true", help="delete warm archived files from the tree (git history keeps them)")
    p.add_argument("--migrate", action="store_true", help="move files (or every tracked file under a directory) out of "
                   "a legacy nested archive folder into the layout's mirror of their original path, with migrate events")
    p.add_argument("--force", action="store_true", help="with --cold: before [cold] warm_days have passed; archiving: "
                   "although a routed host's start-doc roots cannot be read")
    p.add_argument("--reason", default="", help="why, recorded in the manifest")
    p.add_argument("--cross-owner", default="notify", choices=("notify", "rewrite", "keep"),
                   help="live references to an archived file: notify refuses and lists them (default); rewrite points "
                        "them at the archive path in the same commit (path substitution only, verified); keep is not "
                        "defined yet")
    p.add_argument("--strict", action="store_true",
                   help="with --cross-owner rewrite: refuse, and list, any reference written only as a partial path "
                        "(an unattended run's setting)")
    p.add_argument("--by", default="", help="who, recorded in the manifest (default $SHOAL_SLUG, else user)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_archive)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    add_parser(ap.add_subparsers(dest="cmd", required=True))
    a = ap.parse_args()
    sys.exit(a.func(a))
