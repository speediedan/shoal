#!/usr/bin/env python3
"""Reference-aware archival of role documents (docs/archive.md): warm archive, restore, and warm to cold.

    shoal archive <path> [<path> ...] [--reason R] [--by WHO] [--dry-run] [--json]
    shoal archive --restore <original path> [--by WHO] [--dry-run] [--json]
    shoal archive --cold <original path> [<path> ...] [--force] [--by WHO] [--dry-run] [--json]

`archive` moves tracked files to the repository's archive layout (`.shoal/lifecycle.toml` `[layout] archive`, by default
`archive/<original path>`) and appends one event per file to that directory's manifest (`[layout] manifest`, by default
`archive/<original dir>/MANIFEST.jsonl`). It refuses a path that is not tracked, has uncommitted changes, is a start-doc
root, or is still named by a live document: that is the break this command exists to prevent, so the caller first
points live references elsewhere (or keeps the file). History documents' references stay as written; the manifest is
what resolves them.

`--restore` moves a file back to its original path, from the warm archive or, once cold, from the commit its `cold`
event names. `--cold` deletes a warm file from the tree with a `cold` event whose `restore_from` is the commit that still
holds it; it refuses a file a live document names, and one archived fewer than `[cold] warm_days` ago unless `--force`.

Each run is one commit of exactly its paths (the moved files, their new paths and the manifests), through the
repository's `[hooks] commit` when this host trusts it, else `git commit -- <paths>`: never `git add -A`, because other
sessions commit in the same checkout. If the commit does not land, every move is undone and each manifest is put back as
it was, so a refused hook leaves nothing behind. Agent-neutral: shell, git and files only; no model calls.
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


def _live_referrers(plan: Plan, targets: list[str]) -> dict[str, list[dict]]:
    """{target: [live referrers]} across the repository, by the repository's tiers, plus shoal's own roots."""
    tiers = plan.lc["values"]["tiers"]

    def tier(p: str) -> str:
        if refs._matches(p, tiers["exclude"]):
            return "excluded"
        return "history" if refs._matches(p, tiers["history"]) else "live"

    referrers = sorted(p for p in plan.repo.mode if plan.repo.mode[p] != "120000"
                       and posixpath.splitext(p)[1] in refs.TEXT_EXT and tier(p) != "excluded" and p not in targets)
    edges = refs.edges_to(plan.repo, targets, referrers, tier)
    return {t: [e for e in es if e["tier"] in ("live", "symlink") and e["file"] not in targets] for t, es in edges.items()}


def plan_archive(paths: list[str], reason: str, by: str, force: bool = False) -> Plan:
    plan = Plan(paths[0])
    rels = [plan.rel(p) for p in paths]
    for r in rels:
        _clean(plan, r)
        a = plan.archive_path(r)
        if a == r or a.startswith("..") or os.path.isabs(a):
            raise Refused(f"{r}: the layout maps it to {a!r}, outside the archive")
        if a in plan.repo.mode or os.path.lexists(os.path.join(plan.repo.root, a)):
            raise Refused(f"{r}: {a} already exists in the archive")
    # every host's roots: a document here may be another host's standalone seat's start doc, which no file here names
    known = refs.roots(all_hosts=True)
    if known["unreachable"]:
        silent = "; ".join(f"{u['host']} ({u['reason']})" for u in known["unreachable"])
        if not force:
            raise Refused(f"cannot read the start-doc roots of {silent}, so a file here could be a start doc there: "
                          "--force to archive anyway")
        plan.notes.append(f"archived without the roots of {silent} (--force)")
    roots = {plan.repo.rel(p): whos for p, whos in known["roots"].items()}
    for r in rels:
        if r in roots:
            raise Refused(f"{r} is a start doc ({'; '.join(refs.who_text(w) for w in roots[r])}): retire or move the role first")
    named = _live_referrers(plan, rels)
    blocked = {t: es for t, es in named.items() if es}
    if blocked:
        lines = [f"{t} is still named by live documents:" + "".join(f"\n    {e['file']}:{e['line']}" for e in es[:8])
                 for t, es in sorted(blocked.items())]
        raise Refused("\n  ".join(lines) + "\n  point those references elsewhere (or keep the file), then archive it")
    when = utcnow()
    for r in rels:
        a = plan.archive_path(r)
        plan.moves.append((r, a))
        plan.add_event(r, {"ts": when, "by": by, "event": "archive", "path": r, "archive_path": a, "reason": reason})
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
            r = subprocess.run(["bash", "-c", cmd], cwd=root, capture_output=True, text=True, timeout=600)
            why = f"the commit hook exited {r.returncode}: {(r.stderr or r.stdout).strip()[-600:]}" if r.returncode else ""
        else:
            r = _git(root, "commit", "-q", "-m", message, "--", *paths, check=False)
            why = f"git commit refused: {(r.stderr or r.stdout).strip()[-600:]}" if r.returncode else ""
    except subprocess.TimeoutExpired:
        why = "the commit hook did not finish in 600 s"
    if not _landed(plan, written):
        raise Refused(why or "the commit did not land")
    if why:
        plan.notes.append(f"committed, but {why}")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


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
        for m, events in plan.events.items():
            os.makedirs(os.path.join(root, posixpath.dirname(m)), exist_ok=True)
            base = saved[m] or ""
            written[m] = (base + ("" if not base or base.endswith("\n") else "\n")
                          + "".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
            with open(os.path.join(root, m), "w") as fh:
                fh.write(written[m])
            _git(root, "add", "--", m)
            done.append(("manifest", m, saved[m] is not None))
        head = _commit(plan, message, written)
        return {"ok": True, "commit": head, "notes": plan.notes}
    except Exception as exc:
        _undo(root, done)
        if isinstance(exc, Refused):
            raise
        raise Refused(f"{exc.__class__.__name__}: {exc}") from None


def _undo(root: str, done: list[tuple]) -> None:
    """Put every step back, newest first: moves reversed, restored files removed, deletions restored, and each manifest
    back to HEAD's copy (it was clean when the run began) or removed when the run created it."""
    for step in reversed(done):
        if step[0] == "mv":
            _git(root, "mv", "-f", "--", step[2], step[1], check=False)
            _repoint_symlink(root, step[2], step[1], {v: k for k, v in step[3].items()})
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
    out += [f"append {len(es)} event(s) to {m}" for m, es in plan.events.items()]
    return out


def cmd_archive(a) -> int:
    by = a.by or os.environ.get("SHOAL_SLUG") or "user"
    try:
        if a.restore:
            plan, verb = plan_restore(a.restore, by), "restore"
        elif not a.paths:
            print("archive: name the files to archive, --restore <path> or --cold <path>")
            return 2
        elif a.cold:
            plan, verb = plan_cold(a.paths, a.force, by), "cold"
        else:
            plan, verb = plan_archive(a.paths, a.reason, by, a.force), "archive"
        steps = describe(plan)
        if a.dry_run:
            out = {"ok": True, "dry_run": True, "steps": steps, "events": plan.events, "notes": plan.notes}
        else:
            what = ", ".join(sorted({e["path"] for es in plan.events.values() for e in es}))
            out = dict(apply(plan, f"{verb}: {what}" + (f" ({a.reason})" if a.reason and verb == "archive" else "")),
                       steps=steps, events=plan.events)
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
    p.add_argument("--force", action="store_true", help="with --cold: before [cold] warm_days have passed; archiving: "
                   "although a routed host's start-doc roots cannot be read")
    p.add_argument("--reason", default="", help="why, recorded in the manifest")
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
