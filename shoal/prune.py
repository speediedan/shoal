#!/usr/bin/env python3
"""Prune old role incarnations from each harness's own session listing (docs/handover.md, "Pruning old incarnations").

Every handover and restart starts a new session (`<slug>-r<N>`) and only stops the old one, so each harness's listing
fills with retired incarnations (and Claude keeps each job's `tmp/`, often gigabytes). A prune removes them, purge
preferred over archive where an archive would only duplicate:

  claude    purge: a tombstone of the job record under ~/.claude/archived_sessions/jobs/<job>/, then `claude rm`
            (the transcript under ~/.claude/projects/ stays; Claude Code expires it after 30 days)
  codex     archive: `thread/archive` (a flag on the one stored rollout); `--delete`: `thread/delete`
  opencode  archive: OpenChamber's archive flag; `--delete`: opencode's `DELETE /api/session/<id>` via OpenChamber
  muse      archive: the session directory moves to `archived-sessions/`; `--delete`: it is removed

Only sessions shoal created are considered: a name `<stem>-r<N>` whose stem is a role slug or title of a spec, or an
id in a spec's generation history; standalone predecessors only with `--standalone`, matcher sessions only with
`--matcher`. Every guard below keeps a session, and the reason is shown:

  (a) the current session of any role in any spec (retired specs too), matched by job id, current and original
      conversation id
  (b) any role's parked session
  (c) any entry in the viewer registry (ui-standalone.json), session or job, dismissed ones too
  (d) a live worker (pid), or state working, blocked or idle
  (e) a conversation shared with a job that has a worker or is protected
  (f) its recorded worktree exists, a git worktree is registered under its job tmp/, or a process runs inside either
  (g) the job this runs inside
  (h) a held Muse `.session.lock` or shoal exec lock, or a ledger-watch still bound to it
  and anything younger than --min-age (default 24h).

    shoal prune-incarnations (--spec S | --all-specs) [--harness claude,codex,opencode,muse] [--standalone]
                             [--matcher] [--delete] [--min-age 24h] [--batch 10] [--dry-run]

Automatic (features.prune, default off): once a successor proves itself (its `first-turn` handover event, or
`verify_consumer` in a restart), the role's incarnations up to N-2 are pruned; N-1 stays one generation as a
fallback. Never on park. Each removal is one `pruned` telemetry event.
"""
from __future__ import annotations

import dataclasses
import datetime
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

HARNESSES = ("claude", "codex", "opencode", "muse")
LIVE_STATES = ("working", "blocked", "idle")
NAME_RE = re.compile(r"^(?P<stem>.+)-r(?P<n>\d+)$")
MATCHER_RE = re.compile(r"^shoal-matcher-\d+$")
HOST_SID = "_host"  # telemetry home for removals that belong to no shoal (standalone, matcher)


@dataclasses.dataclass
class Cand:
    harness: str
    session: str                 # the id the harness acts on
    job: str = ""                # Claude job id
    name: str = ""
    kind: str = "role"           # role | standalone | matcher
    sid: str = ""                # owning shoal ('' for standalone and matcher sessions)
    slug: str = ""
    seq: int = 0
    nbytes: int | None = None
    age_s: float | None = None
    ids: tuple = ()              # every id it is known by (job, current and original conversation)
    path: str = ""               # its on-disk record (bytes; cwd checks)
    directory: str = ""          # opencode: the session's directory
    verdict: str = "keep"
    reason: str = ""
    did: str = ""


def same_id(a: str, b: str) -> bool:
    """Two stored ids name one session: equal, or one a prefix of the other at 8 characters or more (a Claude job id
    against its conversation id)."""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return len(short) >= 8 and long_.startswith(short)


def parse_age(text: str) -> float:
    """'24h', '90m', '2d', '30s' or seconds; 0 for '' or '0'."""
    t = str(text or "0").strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", t)
    if not m:
        raise ValueError(f"bad age {text!r} (want e.g. 24h, 90m, 2d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def human(n: int | None) -> str:
    if n is None:
        return "-"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return str(n)


def du(path: str) -> int:
    """Bytes under `path` (no symlinks followed), 0 when unreadable."""
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except NotADirectoryError:
            try:
                total += os.lstat(p).st_size
            except OSError:
                pass
        except OSError:
            continue
    return total


def _ts(v) -> float | None:
    """Epoch seconds from an ISO string, epoch seconds or epoch milliseconds."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e11 else float(v)
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---- the world: specs, registry, processes ------------------------------------------------------------------

class World:
    """Everything the guards read, gathered once per run: every spec (protection), the selected specs (candidates),
    the viewer registry, and one pass over /proc (cwds, ancestors, ledger-watch bindings)."""

    def __init__(self, ledger_dir: str = "", selected: list[str] | None = None, all_specs: bool = False,
                 extra_protect: list[tuple[str, str]] | None = None, only_slug: str = "", max_seq: int = 0):
        self.ledger_dir = ledger_dir or config.ledger_dir()
        paths = sorted(glob.glob(os.path.join(self.ledger_dir, "shoals", "*", "shoal.json")))
        pin = config.env("SPEC")
        for p in [pin] + list(selected or []):
            if p and os.path.isfile(p) and os.path.abspath(p) not in map(os.path.abspath, paths):
                paths.append(p)
        self.specs: list[tuple[str, dict]] = []
        for p in paths:
            try:
                self.specs.append((p, json.load(open(p))))
            except (OSError, ValueError):
                continue
        sel = {os.path.abspath(p) for p in (selected or [])}
        self.selected = [(p, d) for p, d in self.specs if all_specs or os.path.abspath(p) in sel]
        self.only_slug, self.max_seq = only_slug, max_seq
        self.protect: list[tuple[str, str]] = list(extra_protect or [])
        for _p, d in self.specs:
            sid = str(d.get("shoal") or "")
            for r in d.get("roles") or []:
                if r.get("session"):
                    self.protect.append((str(r["session"]), f"current session of {r.get('slug')}@{sid}"))
                ps = (r.get("parked") or {}).get("session") if isinstance(r.get("parked"), dict) else ""
                if ps:
                    self.protect.append((str(ps), f"parked session of {r.get('slug')}@{sid}"))
        # (c) the viewer registry, every entry (dismissed and mirrored ones too)
        self.registry: list[dict] = []
        try:
            p = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "shoal",
                             "ui-standalone.json")
            self.registry = [e for e in json.load(open(p)).get("sessions", []) if isinstance(e, dict)]
        except (OSError, ValueError, AttributeError):
            self.registry = []
        for e in self.registry:
            lab = e.get("label") or str(e.get("session", ""))[:12]
            for k in ("session", "job"):
                if e.get(k):
                    self.protect.append((str(e[k]), f"standalone seat {lab}" + (" (dismissed)" if e.get("dismissed") else "")))
        # (g) the session this runs inside
        me = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if me:
            self.protect.append((me, "the session this runs inside"))
        # candidates: name stems and generation history of the selected specs
        self.stems: dict[str, tuple[str, str]] = {}
        self.history: dict[str, tuple[str, str, int]] = {}
        for _p, d in self.selected:
            sid = str(d.get("shoal") or "")
            for r in d.get("roles") or []:
                for stem in {r.get("slug"), r.get("title")}:
                    if stem:
                        self.stems.setdefault(str(stem), (sid, str(r.get("slug"))))
            for g in d.get("generations") or []:
                seqs = g.get("seqs") or {}
                for slug, sess in (g.get("sessions") or {}).items():
                    if sess:
                        self.history.setdefault(str(sess), (sid, slug, int(seqs.get(slug) or 0)))
            for r in d.get("roles") or []:  # current and parked sessions are shoal's too (shown, and kept by a/b)
                ps = (r.get("parked") or {}).get("session") if isinstance(r.get("parked"), dict) else ""
                for sess in (r.get("session"), ps):
                    if sess:
                        self.history.setdefault(str(sess), (sid, str(r.get("slug")), int(r.get("seq") or 0)))
        self.std_labels = {str(e.get("label")): e for e in self.registry if e.get("label") and not e.get("host")}
        self.std_from = {str((e.get("handover_state") or {}).get("from")) for e in self.registry
                         if (e.get("handover_state") or {}).get("from")}
        self._scan_proc()

    def _scan_proc(self) -> None:
        self.cwds: list[tuple[int, str]] = []
        self.bound: list[tuple[int, str]] = []  # (pid, session id a ledger-watch delivers to)
        me = os.getpid()
        for d in os.listdir("/proc"):
            if not d.isdigit() or int(d) == me:
                continue
            try:
                self.cwds.append((int(d), os.readlink(f"/proc/{d}/cwd")))
            except OSError:
                pass
            try:
                with open(f"/proc/{d}/cmdline", "rb") as fh:
                    argv = [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
            except OSError:
                continue
            if "ledger-watch" in argv and any(a.endswith(("shoal", "cli.py")) for a in argv[:3]):
                opt = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
                wake = opt.get("--wake", "")
                for v in (opt.get("--session", ""), wake.split(":", 1)[1] if wake.startswith(("send:", "exec:")) else ""):
                    if v:
                        self.bound.append((int(d), v))
        self.ancestors: set[int] = set()
        pid = os.getpid()
        for _ in range(64):
            try:
                with open(f"/proc/{pid}/stat") as fh:
                    pid = int(fh.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                break
            if pid <= 1:
                break
            self.ancestors.add(pid)
        try:
            self.ancestors.add(int(os.environ.get("CLAUDE_PID") or 0))
        except ValueError:
            pass
        self.ancestors.discard(0)

    def protected(self, ids) -> str:
        for pid_, why in self.protect:
            if any(same_id(i, pid_) for i in ids if i):
                return why
        return ""

    def bound_to(self, ids) -> int:
        for pid, v in self.bound:
            if any(same_id(i, v) for i in ids if i):
                return pid
        return 0

    def proc_inside(self, *dirs: str) -> int:
        for pid, cwd in self.cwds:
            for d in dirs:
                if d and (cwd == d or cwd.startswith(d.rstrip("/") + "/")):
                    return pid
        return 0

    def origin(self, name: str, ids) -> tuple[str, str, str, int]:
        """(kind, sid, slug, seq) when shoal created this session (by its `<stem>-r<N>` name or generation history),
        else ('', '', '', 0)."""
        for i in ids:
            for hid, (sid, slug, seq) in self.history.items():
                if same_id(i, hid):
                    m = NAME_RE.match(name or "")
                    return "role", sid, slug, seq or (int(m.group("n")) if m else 0)
        m = NAME_RE.match(name or "")
        if m and m.group("stem") in self.stems:
            sid, slug = self.stems[m.group("stem")]
            return "role", sid, slug, int(m.group("n"))
        return "", "", "", 0

    def wanted(self, c: Cand) -> bool:
        """The auto-prune filter: only this role's incarnations at or below N-2."""
        if not self.only_slug:
            return True
        return c.kind == "role" and c.slug == self.only_slug and 0 < c.seq <= self.max_seq


# ---- per harness: listing and guards ------------------------------------------------------------------------

def git_worktree_under(root: str, depth: int = 3) -> str:
    """A git worktree (a directory with a `.git` FILE) within `depth` levels under root, '' when none."""
    if not os.path.isdir(root):
        return ""
    base = root.rstrip("/").count("/")
    for d, dirs, files in os.walk(root):
        if ".git" in files:
            return d
        if d.count("/") - base >= depth:
            dirs[:] = []
        else:
            dirs[:] = [x for x in dirs if x not in (".git", "node_modules")]
    return ""


def claude_candidates(w: World, standalone: bool, min_age: float, sizes: bool = True) -> tuple[int, list[Cand]]:
    from backends import _state_of, claude_base, get as get_backend
    rows = get_backend("claude")._agents(include_stopped=True)
    now = time.time()
    info = []
    for r in rows:
        job = str(r.get("id") or "")
        st = _state_of(job)
        orig = str(r.get("originalSessionId") or st.get("sessionId") or "")
        info.append((r, job, st, (job, str(r.get("sessionId") or ""), orig)))
    own = {job for r, job, _st, _ids in info if r.get("pid") and int(r.get("pid") or 0) in w.ancestors}
    hard = {}  # job -> why a job protects its conversation: a worker, a protected id, or this process
    for r, job, _st, ids in info:
        why = ("the job this runs inside" if job in own else w.protected(ids)
               or (f"live worker pid {r.get('pid')}" if r.get("pid") else ""))
        if why:
            hard[job] = why
    out = []
    for r, job, st, ids in info:
        name = str(r.get("name") or st.get("name") or "")
        kind, sid, slug, seq = w.origin(name, ids)
        if not kind and standalone and (name in w.std_labels or job in w.std_from):
            kind = "standalone"
        if not kind:
            continue
        c = Cand("claude", str(r.get("sessionId") or job), job=job, name=name, kind=kind, sid=sid, slug=slug,
                 seq=seq, ids=ids, path=os.path.join(claude_base(), "jobs", job) if job else "")
        if not w.wanted(c):
            continue
        t = _ts(st.get("updatedAt")) or _ts(st.get("lastTerminalAt")) or _ts(r.get("startedAt"))
        c.age_s = now - t if t else None
        c.reason = claude_guard(w, r, st, ids, info, hard, own, min_age, c)
        c.verdict = "keep" if c.reason else "prune"
        if not c.reason:
            c.reason = f"{r.get('state') or 'stopped'}, idle {_age(c.age_s)}"
        if sizes:
            c.nbytes = du(c.path) if c.path and os.path.isdir(c.path) else None
        out.append(c)
    return len(rows), out


def claude_guard(w, r, st, ids, info, hard, own, min_age, c) -> str:
    job = ids[0]
    why = w.protected(ids)
    if job in own or why == "the session this runs inside":
        return "(g) the job this runs inside"
    if why:
        return f"(a-c) {why}"
    if not job or r.get("kind") == "interactive":
        return "(d) interactive session (no background job to remove)"
    if r.get("pid"):
        return f"(d) live worker pid {r.get('pid')}"
    if str(r.get("state") or "") in LIVE_STATES:
        return f"(d) state {r.get('state')}"
    for r2, job2, _st2, ids2 in info:
        if job2 == job or job2 not in hard:
            continue
        if any(same_id(a, b) for a in ids[1:] for b in ids2[1:] if a and b):
            return f"(e) conversation shared with {job2} ({hard[job2]})"
    wt = str(st.get("worktreePath") or "")
    if wt and os.path.exists(wt):
        return f"(f) its worktree exists ({wt})"
    g = git_worktree_under(os.path.join(c.path, "tmp"))
    if g:
        return f"(f) git worktree under its tmp ({g})"
    pid = w.proc_inside(c.path, wt)
    if pid:
        return f"(f) process {pid} runs inside it"
    pid = w.bound_to(ids)
    if pid:
        return f"(h) ledger-watch pid {pid} bound to it"
    if min_age and (c.age_s is None or c.age_s < min_age):
        return f"younger than --min-age ({_age(c.age_s)} old)" if c.age_s is not None else "age unknown"
    return ""


def _age(s: float | None) -> str:
    if s is None:
        return "?"
    if s >= 86400:
        return f"{s / 86400:.0f}d"
    if s >= 3600:
        return f"{s / 3600:.0f}h"
    return f"{s / 60:.0f}m"


def _generic_guard(w: World, c: Cand, busy: bool, live_state: str, min_age: float) -> str:
    why = w.protected(c.ids)
    if why:
        return f"(a-c) {why}"
    if live_state:
        return f"(d) {live_state}"
    if busy:
        return "(h) lock held (a turn runs, or a client holds it)"
    pid = w.bound_to(c.ids)
    if pid:
        return f"(h) ledger-watch pid {pid} bound to it"
    if min_age and (c.age_s is None or c.age_s < min_age):
        return f"younger than --min-age ({_age(c.age_s)} old)" if c.age_s is not None else "age unknown"
    return ""


def codex_candidates(w: World, min_age: float) -> tuple[int, list[Cand]]:
    import codex_harness as codex
    from muse import lock_held
    rows = codex.list_threads()
    now = time.time()
    out = []
    for t in rows:
        tid = str(t.get("id") or "")
        if not tid:
            continue
        name = str(t.get("name") or "")
        kind, sid, slug, seq = w.origin(name, (tid,))
        if not kind:
            continue
        c = Cand("codex", tid, name=name or tid[:12], kind=kind, sid=sid, slug=slug, seq=seq, ids=(tid,),
                 path=str(t.get("path") or ""))
        if not w.wanted(c):
            continue
        ts = _ts(t.get("updatedAt"))
        c.age_s = now - ts if ts else None
        status = (t.get("status") or {}).get("type") if isinstance(t.get("status"), dict) else ""
        busy = lock_held(os.path.join(codex.sdir(tid), "lock")) or bool(codex.attached_client(tid))
        c.reason = _generic_guard(w, c, busy, "thread active" if status == "active" else "", min_age)
        c.verdict = "keep" if c.reason else "prune"
        c.reason = c.reason or f"idle {_age(c.age_s)}"
        try:
            c.nbytes = os.path.getsize(c.path) if c.path else None
        except OSError:
            c.nbytes = None
        out.append(c)
    return len(rows), out


def opencode_sessions() -> tuple[list[dict], set[str]]:
    """(every unarchived root session, the ids of active ones), read through OpenChamber's server: opencode's own
    `GET /api/session`, paged by its cursor (the `openchamber session list` control call stops at opencode's first page
    of 50), with OpenChamber's archive flag folded in, and `GET /api/session/active`."""
    import urllib.parse
    from backends import openchamber_api
    rows: list[dict] = []
    cursor = ""
    for _ in range(200):
        q = "/api/session?limit=200&parentID=null" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        code, body = openchamber_api("GET", q)
        if code >= 300 or not isinstance(body, dict):
            raise RuntimeError(f"GET /api/session: HTTP {code} {str(body)[:120]}")
        rows += [s for s in body.get("data") or [] if isinstance(s, dict) and s.get("id")]
        cursor = str(((body.get("cursor") or {}).get("next")) or "")
        if not cursor:
            break
    code, act = openchamber_api("GET", "/api/session/active")
    active = set(((act or {}).get("data") or {}) if code < 300 and isinstance(act, dict) else {})
    return [s for s in rows if not (s.get("time") or {}).get("archived")], active


def opencode_candidates(w: World, matcher: bool, min_age: float) -> tuple[int, list[Cand]]:
    rows, active = opencode_sessions()
    now = time.time()
    out = []
    for s in rows:
        sid_ = str(s["id"])
        title = str(s.get("title") or "")
        kind, shoal, slug, seq = w.origin(title, (sid_,))
        if not kind and matcher and MATCHER_RE.match(title):
            kind = "matcher"
        if not kind:
            continue
        c = Cand("opencode", sid_, name=title or sid_[:12], kind=kind, sid=shoal, slug=slug, seq=seq, ids=(sid_,),
                 directory=str((s.get("location") or {}).get("directory") or s.get("directory") or ""))
        if not w.wanted(c):
            continue
        tm = s.get("time") or {}
        ts = _ts(tm.get("updated") or tm.get("created"))
        c.age_s = now - ts if ts else None
        c.reason = _generic_guard(w, c, False, "session active" if sid_ in active else "", min_age)
        c.verdict = "keep" if c.reason else "prune"
        c.reason = c.reason or f"idle {_age(c.age_s)}"
        out.append(c)
    return len(rows), out


def muse_candidates(w: World, min_age: float, sizes: bool = True) -> tuple[int, list[Cand]]:
    import muse
    root = muse.sessions_root()
    dirs = [d for d in glob.glob(os.path.join(root, "*", "*", "*", "*")) if os.path.isdir(d)]
    now = time.time()
    out = []
    for d in dirs:
        sess = os.path.basename(d)
        if not muse.UUID_RE.fullmatch(sess):
            continue
        kind, sid, slug, seq = w.origin("", (sess,))
        if not kind:
            continue
        c = Cand("muse", sess, name=f"{slug}-r{seq}" if seq else sess[:12], kind=kind, sid=sid, slug=slug, seq=seq,
                 ids=(sess,), path=d)
        if not w.wanted(c):
            continue
        try:
            c.age_s = now - os.path.getmtime(os.path.join(d, "session.jsonl"))
        except OSError:
            c.age_s = now - os.path.getmtime(d)
        busy = muse.lock_held(os.path.join(d, ".session.lock")) or muse.lock_held(os.path.join(muse.sdir(sess), "lock"))
        c.reason = _generic_guard(w, c, busy, "", min_age)
        c.verdict = "keep" if c.reason else "prune"
        c.reason = c.reason or f"idle {_age(c.age_s)}"
        if sizes:
            c.nbytes = du(d)
        out.append(c)
    return len(dirs), out


# ---- apply ----------------------------------------------------------------------------------------------------

def mode_for(harness: str, delete: bool) -> str:
    """The harness policy: Claude purges (it has no archive); the others archive unless --delete."""
    return "purge" if harness == "claude" or delete else "archive"


def emit(c: Cand, mode: str, trigger: str, outcome: str) -> None:
    """One `pruned` telemetry event per removal, in the owning shoal's telemetry (HOST_SID for no shoal). Never
    raises: telemetry must not stop a prune."""
    try:
        tdir = os.path.join(HERE, "telemetry")
        if tdir not in sys.path:
            sys.path.insert(0, tdir)
        from shoal_telemetry import append_event, build_event, resolve_dir
        sid = c.sid or HOST_SID
        kv = [f"harness={c.harness}", f"mode={mode}", f"name={c.name}", f"kind={c.kind}", f"trigger={trigger}",
              f"outcome={outcome}"]
        for k, v in (("job", c.job), ("slug", c.slug), ("seq", c.seq or ""), ("bytes", c.nbytes),
                     ("reason", c.reason)):
            if v not in (None, ""):
                kv.append(f"{k}={v}")
        append_event(resolve_dir(None, sid), build_event(sid, c.session, c.harness, "pruned", kv))
    except Exception:  # noqa: BLE001
        pass


def apply(cands: list[Cand], delete: bool, batch: int, trigger: str, log=print) -> int:
    """Prune every `prune` verdict, `batch` at a time; returns the failure count."""
    from backends import get as get_backend
    todo = [c for c in cands if c.verdict == "prune"]
    fails = 0
    for i in range(0, len(todo), max(batch, 1)):
        chunk = todo[i:i + max(batch, 1)]
        for c in chunk:
            mode = mode_for(c.harness, delete)
            be = get_backend(c.harness)
            try:
                if c.harness == "opencode":
                    c.did = be.prune(c.session, mode=mode, directory=c.directory)
                else:
                    c.did = be.prune(c.session, job=c.job, mode=mode)
                emit(c, mode, trigger, "ok")
            except Exception as exc:  # noqa: BLE001 - one session never stops the batch
                c.did = f"FAILED ({str(exc)[:140]})"
                fails += 1
            log(f"  {c.harness} {c.job or c.session[:12]} {c.name}: {c.did}")
        log(f"  batch {i // max(batch, 1) + 1}/{(len(todo) + max(batch, 1) - 1) // max(batch, 1)} done")
    return fails


def table(harness: str, listed: int, cands: list[Cand], log=print) -> None:
    pr = [c for c in cands if c.verdict == "prune"]
    free = sum(c.nbytes or 0 for c in pr)
    log(f"== {harness}: {listed} listed, {len(cands)} shoal-created; prune {len(pr)}, keep {len(cands) - len(pr)}"
        + (f" (frees {human(free)})" if free else ""))
    if not cands:
        return
    log(f"  {'ID':<14} {'NAME':<34} {'ACTION':<6} {'BYTES':>9}  REASON")
    for c in sorted(cands, key=lambda c: (c.verdict != "prune", c.name)):
        cid = c.job or c.session[:12]
        log(f"  {cid:<14} {c.name[:34]:<34} {c.verdict:<6} {human(c.nbytes):>9}  {c.reason}")


def run(*, specs: list[str] | None = None, all_specs: bool = False, harnesses: tuple = HARNESSES,
        standalone: bool = False, matcher: bool = False, delete: bool = False, min_age: float = 86400,
        batch: int = 10, dry_run: bool = False, only_slug: str = "", max_seq: int = 0, trigger: str = "manual",
        extra_protect: list[tuple[str, str]] | None = None, sizes: bool = True, log=print) -> dict:
    """Gather, judge and (unless dry_run) prune. Returns {harness: {"listed", "cands", "failed"}}."""
    w = World(selected=specs, all_specs=all_specs, extra_protect=extra_protect, only_slug=only_slug, max_seq=max_seq)
    out = {}
    for h in harnesses:
        try:
            if h == "claude":
                listed, cands = claude_candidates(w, standalone, min_age, sizes=sizes)
            elif h == "codex":
                listed, cands = codex_candidates(w, min_age)
            elif h == "opencode":
                listed, cands = opencode_candidates(w, matcher, min_age)
            elif h == "muse":
                listed, cands = muse_candidates(w, min_age, sizes=sizes)
            else:
                log(f"== {h}: unknown harness")
                continue
        except Exception as exc:  # noqa: BLE001 - one harness never stops the others
            log(f"== {h}: listing failed ({str(exc)[:160]}); nothing pruned there")
            out[h] = {"listed": 0, "cands": [], "failed": 0, "error": str(exc)}
            continue
        table(h, listed, cands, log)
        failed = 0
        if not dry_run and any(c.verdict == "prune" for c in cands):
            failed = apply(cands, delete, batch, trigger, log)
        out[h] = {"listed": listed, "cands": cands, "failed": failed}
    n_pr = sum(1 for v in out.values() for c in v["cands"] if c.verdict == "prune")
    n_keep = sum(1 for v in out.values() for c in v["cands"] if c.verdict != "prune")
    log(f"{'[dry-run] ' if dry_run else ''}total: prune {n_pr}, keep {n_keep}"
        + (f", failed {sum(v['failed'] for v in out.values())}" if not dry_run else ""))
    return out


# ---- automatic: N-2 once N proves itself --------------------------------------------------------------------

def feature(spec) -> dict:
    """features.prune: false/absent (off), true (on), or {"enabled": true, "delete": false}."""
    v = (getattr(spec, "features", {}) or {}).get("prune")
    if isinstance(v, dict):
        return {"enabled": bool(v.get("enabled", True)), "delete": bool(v.get("delete", False))}
    return {"enabled": bool(v), "delete": False}


def auto(spec, role, trigger: str, spec_path: str = "", dry_run: bool = False, detach: bool = True,
         log=print) -> str:
    """Prune `role`'s incarnations up to N-2 once incarnation N has proved itself (`trigger`: first-turn or restart);
    N-1 stays one generation as the fallback. Off unless features.prune; never for a parked role. `detach` runs it
    as its own process (a role's `queue` write must not wait on removals); otherwise in this process. Returns what
    was done, '' when nothing applies."""
    f = feature(spec)
    if not f["enabled"] or getattr(role, "parked", None) or not getattr(role, "session", ""):
        return ""
    max_seq = int(getattr(role, "seq", 0) or 0) - 2
    if max_seq < 1:
        return ""
    path = spec_path or getattr(spec, "path", "") or ""
    if not path:
        from roles import find_spec
        path = find_spec(spec.sid)
    if detach:
        argv = shlex.split(config.cli()) + ["prune-incarnations", "--spec", path, "--role", role.slug,
                                            "--max-seq", str(max_seq), "--harness", role.ecosystem, "--min-age", "0",
                                            "--trigger", trigger] + (["--delete"] if f["delete"] else [])
        if dry_run:
            return "would run: " + " ".join(argv)
        from backends import spawn_detached
        from roles import state_dir
        os.makedirs(state_dir(spec.sid), exist_ok=True)
        logf = os.path.join(state_dir(spec.sid), f"prune-{role.slug}.log")
        spawn_detached(argv, stdin=subprocess.DEVNULL, stdout=open(logf, "a"), stderr=subprocess.STDOUT)
        return f"pruning {role.slug} incarnations <= r{max_seq} (log {logf})"
    keep = [(r.session, f"current session of {r.slug}@{spec.sid}") for r in spec.roles if r.session]
    res = run(specs=[path], harnesses=(role.ecosystem,), delete=f["delete"], min_age=0, dry_run=dry_run,
              only_slug=role.slug, max_seq=max_seq, trigger=trigger, extra_protect=keep, sizes=False, log=log)
    n = sum(1 for v in res.values() for c in v["cands"] if c.verdict == "prune")
    return f"{'would prune' if dry_run else 'pruned'} {n} {role.slug} incarnation(s) <= r{max_seq}"


# ---- CLI ------------------------------------------------------------------------------------------------------

def cmd_prune(a) -> int:
    hs = tuple(h.strip() for h in (a.harness or ",".join(HARNESSES)).split(",") if h.strip())
    bad = [h for h in hs if h not in HARNESSES]
    if bad:
        print(f"prune-incarnations: unknown harness {bad} (want {','.join(HARNESSES)})")
        return 2
    if not (a.spec or a.all_specs or a.standalone or a.matcher):
        print("prune-incarnations: name what to prune: --spec S or --all-specs (and/or --standalone, --matcher)")
        return 2
    try:
        min_age = parse_age(a.min_age)
    except ValueError as exc:
        print(f"prune-incarnations: {exc}")
        return 2
    if a.role and not a.spec:
        print("prune-incarnations: --role needs --spec")
        return 2
    res = run(specs=[a.spec] if a.spec else [], all_specs=a.all_specs, harnesses=hs, standalone=a.standalone,
              matcher=a.matcher, delete=a.delete, min_age=min_age, batch=a.batch, dry_run=a.dry_run,
              only_slug=a.role, max_seq=a.max_seq if a.role else 0, trigger=a.trigger, sizes=not a.no_sizes)
    return 1 if any(v.get("failed") for v in res.values()) else 0


def add_parser(sub) -> None:
    p = sub.add_parser("prune-incarnations",
                       help="remove old role incarnations from each harness's session listing (guarded; --dry-run)")
    p.add_argument("--spec", default="", help="candidates from this spec only (guards always read every spec)")
    p.add_argument("--all-specs", action="store_true", help="candidates from every spec in the ledger clone")
    p.add_argument("--harness", default=",".join(HARNESSES), help="comma list (default: all four)")
    p.add_argument("--standalone", action="store_true",
                   help="also Claude predecessors of saved standalone seats (same name, an older job)")
    p.add_argument("--matcher", action="store_true", help="also leftover `shoal-matcher-*` opencode sessions")
    p.add_argument("--delete", action="store_true",
                   help="codex, opencode, muse: delete instead of archive (Claude always purges)")
    p.add_argument("--min-age", default="24h", help="keep anything changed more recently (default 24h)")
    p.add_argument("--batch", type=int, default=10, help="removals per batch (default 10)")
    p.add_argument("--no-sizes", action="store_true", help="skip the per-session byte count")
    p.add_argument("--dry-run", action="store_true", help="print the table, change nothing")
    p.add_argument("--role", default="", help=argparse_hide())
    p.add_argument("--max-seq", type=int, default=0, help=argparse_hide())
    p.add_argument("--trigger", default="manual", help=argparse_hide())
    p.set_defaults(func=cmd_prune)


def argparse_hide():
    import argparse
    return argparse.SUPPRESS
