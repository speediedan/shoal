#!/usr/bin/env python3
"""The unified HAG communication primitive: a git-versioned, append-only event
ledger with one file per writer (readme 5.j, established 2026-09-20).

Every piece of agent traffic — tasking (offer/withdraw), work ownership
(claim/release/done), row state (state), and messages (msg) — is one JSON
line appended to `hags/<hag>/ledger/<writer>.jsonl` in the queue repo
(`speediedan/hag-queues`, cloned at HAG_LEDGER_DIR). One writer per file
means `git pull --rebase` never conflicts; append-only means history is the
audit trail; JSONL means every reader (agent, watcher, report, human via
`hag.py show`) parses the same grammar. Direct session sends between peers
are retired: a peer that wants another's attention appends `msg`, and the
target's own `ledger-watch` wakes it.

Grammar (every line: ts, by, type, id; the rest per type):
  offer    to item [verb base prio note src]   put work on a queue (to=slug|*)
  withdraw item [to reason re]                 take an offer back
  claim    item [verb base src]                own the work (first landed wins)
  release  item kind [reason retry]            give it up: decline|lost-race|blocked|handoff
  done     item [verb kind url sha note]       finished (verified against GitHub by reconcile)
  msg      to text [kind re ack]               to=slug|*|user; kind=info|ack|ask|instruct|
                                               stall-suspect|server-respawned|starved|hold
  state    state [sub holder rescan_utc monitor note session host]   row state
  system   event [note]                        ledger-init|generation-*

Permissions (checked at read time, so a bad line is flagged and ignored,
never silently honored): only `user` (and the `hag.py` system writer) may
offer or withdraw; an offer or withdraw written by a role is a violation
(flattened only, 2026-09-24). A role self-fills by
claiming board items, and hands its own claim to a peer with `release
--kind handoff --to <role>` (an implicit offer the recipient may claim). A
role with fill=assigned may claim only offers addressed to it or `*`;
anyone may msg anyone. Claims: the claim that LANDED on the remote first
wins — `claim()` checks before and after its push and a loser records
`release kind=lost-race` itself.

Concurrency: one local clone per host, shared by every session on it; all
mutating git operations run under an flock so two sessions on one host
cannot interleave a rebase. Offline: an append that cannot push stays
committed locally and goes out on the next sync; readers keep working on
the last pulled state (a watcher counts fetch failures and dies loudly at 5).
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER_REPO = "git@github.com:speediedan/hag-queues.git"
LEDGER_DIR = os.environ.get("HAG_LEDGER_DIR", os.path.expanduser("~/repos/hag-queues"))
GIT_ENV = dict(os.environ, PATH="/usr/bin:/bin:/usr/local/bin:" + os.environ.get("PATH", ""),
               GIT_TERMINAL_PROMPT="0")

TYPES = ("offer", "withdraw", "claim", "release", "done", "msg", "state", "system", "queue")
QUEUE_NEXT_MAX = 3  # a role's declared queue: one active item + at most this many next
RELEASE_KINDS = ("decline", "lost-race", "blocked", "handoff")
MSG_KINDS = ("info", "ack", "ask", "instruct", "stall-suspect", "server-respawned",
             "starved", "hold", "wake-test", "health")
OFFER_SRC = ("user", "board", "peer")
VERB_RE = re.compile(r":(fix|review|scope|design|verify|triage|merge|doc|test|release|investigate)$")
DEFAULT_PRIO = 50


class LedgerError(RuntimeError):
    pass


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt: datetime.datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime.datetime:
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def split_item(item: str) -> tuple[str, str]:
    """'interpretune#600:fix' -> ('interpretune#600', 'fix'); no verb -> ''."""
    item = (item or "").strip()
    m = VERB_RE.search(item)
    if m:
        return item[:m.start()], m.group(1)
    if ":" in item and "#" in item:
        key, verb = item.rsplit(":", 1)
        return key, verb
    return item, ""


def item_key(item: str) -> str:
    """Canonical matching key: lower-case, owner stripped when it is the
    default owner, verb dropped. 'speediedan/interpretune#600:fix' ->
    'interpretune#600'."""
    key, _ = split_item(item)
    key = key.strip().lower()
    if "/" in key and "#" in key:
        owner, rest = key.split("/", 1)
        if owner == "speediedan":
            key = rest
    return key


def new_id(*parts: str) -> str:
    h = hashlib.sha1(("|".join(parts) + "|" + os.urandom(6).hex()).encode()).hexdigest()
    return h[:10]


# ---- git plumbing ------------------------------------------------------------

def git(root: str, *argv: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", root, *argv], capture_output=True, text=True,
                       timeout=timeout, env=GIT_ENV)
    if check and r.returncode != 0:
        raise LedgerError(f"git {' '.join(argv[:2])} failed: {(r.stdout + r.stderr).strip()[-300:]}")
    return r


class Lock:
    """flock on <root>/.hag-lock: every mutating git op on the shared clone."""

    def __init__(self, root: str):
        self.path = os.path.join(root, ".hag-lock")
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "a+")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def ensure_clone(root: str = LEDGER_DIR, remote: str = DEFAULT_LEDGER_REPO,
                 quiet: bool = False) -> str:
    """Clone the queue repo if absent. Returns the root."""
    if os.path.isdir(os.path.join(root, ".git")):
        return root
    os.makedirs(os.path.dirname(root), exist_ok=True)
    r = subprocess.run(["git", "clone", "--quiet", remote, root], capture_output=True,
                       text=True, timeout=120, env=GIT_ENV)
    if r.returncode != 0:
        raise LedgerError(f"clone {remote} -> {root} failed: {(r.stdout + r.stderr).strip()[-300:]}")
    if not quiet:
        print(f"cloned {remote} -> {root}")
    return root


def has_remote(root: str) -> bool:
    return git(root, "remote", check=False).stdout.strip() != ""


def sync_pull(root: str, timeout: int = 60) -> bool:
    """fetch + rebase onto the remote branch. False when the fetch fails
    (offline): the caller keeps working on local state."""
    if not has_remote(root):
        return True
    with Lock(root):
        r = git(root, "fetch", "--quiet", "origin", timeout=timeout, check=False)
        if r.returncode != 0:
            return False
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        r = git(root, "rebase", "--quiet", f"origin/{branch}", check=False, timeout=timeout)
        if r.returncode != 0:
            git(root, "rebase", "--abort", check=False)
            raise LedgerError(f"rebase onto origin/{branch} failed (should be impossible with "
                              f"one file per writer): {(r.stdout + r.stderr).strip()[-300:]}")
    return True


def sync_push(root: str, attempts: int = 3) -> bool:
    if not has_remote(root):
        return True
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for _ in range(attempts):
        with Lock(root):
            r = git(root, "push", "--quiet", "origin", branch, check=False, timeout=60)
        if r.returncode == 0:
            return True
        if not sync_pull(root):
            return False
    return False


def unpushed(root: str) -> int:
    if not has_remote(root):
        return 0
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    r = git(root, "rev-list", "--count", f"origin/{branch}..HEAD", check=False)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


# ---- the ledger --------------------------------------------------------------

class Ledger:
    def __init__(self, hag: str, root: str = "", sync: bool = True):
        self.hag = hag
        self.root = root or LEDGER_DIR
        self.sync = sync and has_remote(self.root) if os.path.isdir(self.root) else sync
        self.dir = os.path.join(self.root, "hags", hag)
        self.ldir = os.path.join(self.dir, "ledger")
        self._events: list[dict] | None = None

    # -- files
    def writer_path(self, writer: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", writer)
        return os.path.join(self.ldir, f"{safe}.jsonl")

    def exists(self) -> bool:
        return os.path.isdir(self.ldir)

    def init(self, spec_snapshot: dict | None = None, note: str = "") -> str:
        os.makedirs(self.ldir, exist_ok=True)
        keep = os.path.join(self.ldir, ".gitkeep")
        if not os.path.exists(keep):
            open(keep, "a").close()
        paths = [keep]
        if spec_snapshot is not None:
            sp = os.path.join(self.dir, "hag.json")
            with open(sp, "w") as fh:
                json.dump(spec_snapshot, fh, indent=2)
                fh.write("\n")
            paths.append(sp)
        with Lock(self.root):
            rels = [os.path.relpath(p, self.root) for p in paths]
            git(self.root, "add", "--", *rels)
            git(self.root, "commit", "--quiet", "-m", f"{self.hag}: ledger init (spec snapshot)", "--", *rels,
                check=False)
        return self.append("hag.py", "system", event="ledger-init", note=note, _commit=True)["id"]

    def snapshot_spec(self, spec_dict: dict, push: bool = True) -> None:
        """Refresh hags/<hag>/hag.json (the copy remote agents read)."""
        os.makedirs(self.dir, exist_ok=True)
        p = os.path.join(self.dir, "hag.json")
        with open(p, "w") as fh:
            json.dump(spec_dict, fh, indent=2)
            fh.write("\n")
        with Lock(self.root):
            git(self.root, "add", "--", os.path.relpath(p, self.root))
            r = git(self.root, "commit", "--quiet", "-m", f"{self.hag}: spec snapshot", check=False)
        if push and self.sync and r.returncode == 0:
            sync_push(self.root)

    # -- write
    def append(self, by: str, typ: str, _commit: bool = True, _push: bool | None = None,
               **fields) -> dict:
        if typ not in TYPES:
            raise LedgerError(f"unknown event type {typ!r} (want one of {TYPES})")
        ev = {"ts": iso(), "by": by, "type": typ}
        ev["id"] = new_id(ev["ts"], by, typ, str(fields.get("item") or fields.get("to") or ""))
        for k, v in fields.items():
            if v is None or v == "":
                continue
            ev[k] = v
        if "item" in ev:
            key, verb = split_item(str(ev["item"]))
            ev["item"] = key
            if verb and not ev.get("verb"):
                ev["verb"] = verb
        os.makedirs(self.ldir, exist_ok=True)
        path = self.writer_path(by)
        line = json.dumps(ev, separators=(",", ":"), ensure_ascii=False)
        with Lock(self.root):
            with open(path, "a") as fh:
                fh.write(line + "\n")
            if _commit:
                rel = os.path.relpath(path, self.root)
                git(self.root, "add", "--", rel)
                subject = f"{self.hag} {by} {typ} {ev.get('item') or ev.get('to') or ev.get('event') or ''}".strip()
                git(self.root, "commit", "--quiet", "-m", subject[:72], "--", rel)
        self._events = None
        push = self.sync if _push is None else _push
        if _commit and push:
            sync_push(self.root)
        return ev

    def commit_own(self, by: str, subject: str) -> None:
        """Commit lines appended with `_commit=False` to `by`'s file as ONE commit
        (batch verbs such as `triage`); a no-op when nothing is staged."""
        path = self.writer_path(by)
        rel = os.path.relpath(path, self.root)
        with Lock(self.root):
            git(self.root, "add", "--", rel)
            if subprocess.run(["git", "-C", self.root, "diff", "--cached", "--quiet", "--", rel],
                              env=GIT_ENV).returncode == 0:
                return
            git(self.root, "commit", "--quiet", "-m", subject[:72], "--", rel)
        self._events = None

    # -- read
    def pull(self) -> bool:
        ok = sync_pull(self.root) if self.sync else True
        self._events = None
        return ok

    def events(self, reload: bool = False) -> list[dict]:
        if self._events is not None and not reload:
            return self._events
        evs = []
        if os.path.isdir(self.ldir):
            for fn in sorted(os.listdir(self.ldir)):
                if not fn.endswith(".jsonl"):
                    continue
                writer = fn[:-6]
                with open(os.path.join(self.ldir, fn)) as fh:
                    for n, line in enumerate(fh):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(e, dict) or "ts" not in e or "type" not in e:
                            continue
                        e.setdefault("by", writer)
                        e["_writer_file"] = writer
                        e["_n"] = n
                        evs.append(e)
        evs.sort(key=lambda e: (e["ts"], e.get("by", ""), e["_n"]))
        self._events = evs
        return evs

    def landed_order(self, ids: list[str]) -> dict[str, int]:
        """Commit position of each event id on the current branch (0 = oldest).
        Used only to settle contested claims; ids not found map to a large number."""
        out = {}
        try:
            log = git(self.root, "log", "--reverse", "--format=%H", "--", os.path.relpath(self.ldir, self.root)).stdout.split()
            pos = {h: i for i, h in enumerate(log)}
            for i in ids:
                r = git(self.root, "log", "--format=%H", "-S", f'"id":"{i}"', "--",
                        os.path.relpath(self.ldir, self.root), check=False)
                hs = r.stdout.split()
                out[i] = min((pos.get(h, 10**9) for h in hs), default=10**9)
        except LedgerError:
            for i in ids:
                out[i] = 10**9
        return out


# ---- permissions -------------------------------------------------------------

OFFER_WRITERS = ("user", "hag.py")  # the only writers that may offer or withdraw


class Perms:
    """Resolved from the HAG spec; `user` and `hag.py` are implicit writers."""

    def __init__(self, spec=None):
        self.roles = {}
        if spec is not None:
            for r in spec.roles:
                self.roles[r.slug] = {"fill": getattr(r, "fill", "assigned")}

    def is_role(self, by: str) -> bool:
        return by in self.roles

    def may_offer(self, by: str, to: str) -> bool:
        """Only `user` (and the `hag.py` system writer) puts work on a queue; a role-written
        offer is a violation. Peer transfer is `release --kind handoff --to`, never an offer."""
        return by in OFFER_WRITERS

    def may_withdraw(self, by: str, offer: dict | None) -> bool:
        return by in OFFER_WRITERS

    def may_claim(self, by: str, addressed_to: str | None, src: str) -> bool:
        """addressed_to: the offer's `to` (slug, `*`) or None when no offer exists
        (board/self claim)."""
        if by in ("user", "hag.py"):
            return True
        role = self.roles.get(by)
        if role is None:
            return False
        if addressed_to in (by, "*"):
            return True
        if addressed_to is None:
            return role.get("fill") == "self"
        return False


# ---- projection --------------------------------------------------------------

class View:
    """Fold the ledger into queue posture. Invalid events (permission
    violations) are collected in `violations` and otherwise ignored."""

    def __init__(self, ledger: Ledger, spec=None):
        self.ledger = ledger
        self.spec = spec
        self.perms = Perms(spec)
        self.offers: dict[str, dict] = {}       # offer id -> offer (with status)
        self.by_item: dict[str, list[dict]] = {}  # item key -> offers
        self.claims: dict[str, list[dict]] = {}   # item key -> live claims (ordered)
        self.done: dict[str, dict] = {}           # item key -> done event
        self.releases: list[dict] = []
        self.msgs: list[dict] = []
        self.states: dict[str, dict] = {}         # slug -> last state event
        self.queues: dict[str, dict] = {}         # slug -> latest role-owned `queue` version
        self.modes: dict[str, dict] = {}          # mode name -> latest system mode event
        self.last_by: dict[str, str] = {}         # writer -> last ts
        self.violations: list[tuple[dict, str]] = []
        self.contested: dict[str, list[dict]] = {}
        self._fold()

    def _viol(self, e: dict, why: str) -> None:
        self.violations.append((e, why))

    def _fold(self) -> None:
        for e in self.ledger.events():
            by, typ = e.get("by", "?"), e.get("type")
            self.last_by[by] = max(self.last_by.get(by, ""), e["ts"])
            if typ == "offer":
                to = e.get("to", "")
                if not to or not e.get("item"):
                    self._viol(e, "offer without to/item")
                    continue
                if not self.perms.may_offer(by, to):
                    self._viol(e, f"{by} may not offer to {to}")
                    continue
                e = dict(e, status="open")
                self.offers[e["id"]] = e
                self.by_item.setdefault(item_key(e["item"]), []).append(e)
            elif typ == "withdraw":
                key = item_key(e.get("item", ""))
                targets = [o for o in self.by_item.get(key, []) if o["status"] == "open"
                           and (not e.get("to") or o["to"] == e.get("to"))
                           and (not e.get("re") or o["id"] == e.get("re"))]
                if not targets:
                    self._viol(e, "withdraw with no open offer")
                    continue
                for o in targets:
                    if not self.perms.may_withdraw(by, o):
                        self._viol(e, f"{by} may not withdraw {o['id']}")
                        continue
                    o["status"] = "withdrawn"
                    o["withdrawn_by"] = by
            elif typ == "claim":
                key = item_key(e.get("item", ""))
                if not key:
                    self._viol(e, "claim without item")
                    continue
                if key in self.done:
                    self._viol(e, "claim on a done item")
                    continue
                addressed = None
                for o in self.by_item.get(key, []):
                    if o["status"] == "open" and o["to"] in (by, "*"):
                        addressed = o["to"]
                        break
                if addressed is None and any(o["status"] == "open" for o in self.by_item.get(key, [])):
                    addressed = next(o["to"] for o in self.by_item.get(key, []) if o["status"] == "open")
                if not self.perms.may_claim(by, addressed, e.get("src", "")):
                    self._viol(e, f"{by} may not claim {key} (offer to {addressed})")
                    continue
                live = [c for c in self.claims.get(key, []) if c.get("_live")]
                if any(c["by"] == by for c in live):
                    continue  # idempotent re-claim
                e = dict(e, _live=True)
                self.claims.setdefault(key, []).append(e)
                for o in self.by_item.get(key, []):
                    if o["status"] == "open" and o["to"] in (by, "*"):
                        o["status"] = "claimed"
                        o["claimed_by"] = by
            elif typ == "release":
                key = item_key(e.get("item", ""))
                mine = [c for c in self.claims.get(key, []) if c.get("_live") and c["by"] == by]
                for c in mine:
                    c["_live"] = False
                    c["released"] = e
                self.releases.append(e)
                if e.get("kind") == "handoff" and e.get("to") and mine:
                    # the holder hands ITS OWN claim to a named role: an implicit offer, so the
                    # recipient can claim it (and is woken if idle); no peer write to a queue
                    o = {"id": e["id"] + "-h", "ts": e["ts"], "by": by, "type": "offer", "to": e["to"],
                         "item": key, "verb": e.get("verb") or (mine[0].get("verb") or ""),
                         "prio": int(e.get("prio") or 20), "src": "handoff",
                         "note": e.get("reason", ""), "status": "open"}
                    self.offers[o["id"]] = o
                    self.by_item.setdefault(key, []).append(o)
                if e.get("kind") == "decline":
                    for o in self.by_item.get(key, []):
                        if o["status"] == "claimed" and o.get("claimed_by") == by:
                            o["status"] = "open"
                            o.pop("claimed_by", None)
                            o["declined_by"] = sorted(set(o.get("declined_by", []) + [by]))
            elif typ == "done":
                key = item_key(e.get("item", ""))
                self.done[key] = e
                for c in self.claims.get(key, []):
                    if c.get("_live"):
                        c["_live"] = False
                        c["done"] = e
                for o in self.by_item.get(key, []):
                    if o["status"] in ("open", "claimed"):
                        o["status"] = "done"
            elif typ == "msg":
                self.msgs.append(e)
            elif typ == "state":
                self.states[by] = e
            elif typ == "queue":
                if e.get("_writer_file") != by or (self.spec is not None and not self.perms.is_role(by)):
                    self._viol(e, f"queue event by {by} not in its own role file")
                    continue
                self.queues[by] = e
            elif typ == "system" and e.get("mode"):
                if by not in ("user", "hag.py"):
                    self._viol(e, f"{by} may not set mode {e.get('mode')}")
                    continue
                self.modes[e["mode"]] = e
        for key, cs in self.claims.items():
            live = [c for c in cs if c.get("_live")]
            if len(live) > 1:
                self.contested[key] = live

    # -- queries
    def winner(self, key: str) -> dict | None:
        """The live claim that landed first (contested: commit order, then ts)."""
        key = item_key(key)
        live = [c for c in self.claims.get(key, []) if c.get("_live")]
        if not live:
            return None
        if len(live) == 1:
            return live[0]
        order = self.ledger.landed_order([c["id"] for c in live])
        return sorted(live, key=lambda c: (order.get(c["id"], 10**9), c["ts"], c["by"]))[0]

    def active(self, slug: str) -> list[dict]:
        out = []
        for key, cs in self.claims.items():
            for c in cs:
                if c.get("_live") and c["by"] == slug:
                    w = self.winner(key)
                    out.append(dict(c, contested=(w is not None and w["by"] != slug)))
        return sorted(out, key=lambda c: c["ts"])

    def queue(self, slug: str) -> list[dict]:
        """Open offers addressed to slug or `*` (not declined by slug), by prio then ts."""
        out = [o for o in self.offers.values() if o["status"] == "open"
               and o["to"] in (slug, "*") and slug not in o.get("declined_by", [])]
        return sorted(out, key=lambda o: (int(o.get("prio", DEFAULT_PRIO)), o["ts"]))

    def declined(self, slug: str) -> dict[str, dict]:
        out = {}
        for r in self.releases:
            if r["by"] == slug and r.get("kind") == "decline":
                out[item_key(r.get("item", ""))] = r
        return out

    def taken(self) -> set[str]:
        """Item keys that are claimed (live) or done: not candidates for anyone."""
        s = set(self.done)
        for key, cs in self.claims.items():
            if any(c.get("_live") for c in cs):
                s.add(key)
        return s

    def offered_open(self) -> set[str]:
        """Item keys with an open offer to anyone: already in the system, so
        never re-surfaced as a board candidate."""
        return {item_key(o["item"]) for o in self.offers.values() if o["status"] == "open"}

    def last_done(self, slug: str, n: int = 3) -> list[dict]:
        ds = [d for d in self.done.values() if d["by"] == slug]
        return sorted(ds, key=lambda d: d["ts"])[-n:]

    def msgs_to(self, slug: str, since_ts: str = "") -> list[dict]:
        return [m for m in self.msgs if m.get("to") in (slug, "*") and m["ts"] > since_ts
                and m.get("by") != slug]

    def unacked(self, slug: str) -> list[dict]:
        """msgs by slug with ack=true that have no msg re=<id> from the target."""
        acked = {m.get("re") for m in self.msgs if m.get("re")}
        return [m for m in self.msgs if m["by"] == slug and m.get("ack") and m["id"] not in acked]

    def role_state(self, slug: str) -> dict | None:
        return self.states.get(slug)

    def current_queue(self, slug: str) -> dict | None:
        """The role's latest declared queue version (proposal §D), or None."""
        return self.queues.get(slug)

    def is_idle(self, slug: str) -> bool:
        """Idle = the role's latest queue version has no active item. A role that has
        never written a queue version is idle unless it holds a live claim."""
        q = self.queues.get(slug)
        if q is not None:
            return not q.get("active")
        return not self.active(slug)

    def blocked_by(self, slug: str) -> list[dict]:
        """Roles whose latest state waits on `slug` (state `blocked_on` = `<slug>[:item]`):
        [{role, item, since, holder}]. The blocker's `next` and the health quorum read this."""
        out = []
        for r, st in self.states.items():
            b = str(st.get("blocked_on") or "")
            if st.get("state") == "waiting" and b and b.split(":", 1)[0] == slug and r != slug:
                out.append({"role": r, "item": b.split(":", 1)[1] if ":" in b else "",
                            "since": st["ts"], "holder": st.get("holder", "")})
        return out

    def held_unscheduled(self, slug: str) -> list[str]:
        """Live claims the role holds that its declared queue (active + next) omits."""
        q = self.queues.get(slug)
        if q is None:
            return []
        planned = {item_key(x) for x in [q.get("active") or ""] + list(q.get("next") or []) if x}
        return sorted(item_key(c["item"]) for c in self.active(slug) if item_key(c["item"]) not in planned)

    def user_offline(self) -> dict | None:
        """The live user-offline mode event, or None when the maintainer is online."""
        m = self.modes.get("user-offline")
        return m if m is not None and m.get("state") == "on" else None


# ---- rendering ---------------------------------------------------------------

def fmt_item(e: dict) -> str:
    verb = e.get("verb")
    return f"{e.get('item', '?')}{(':' + verb) if verb else ''}"


def short(s: str, n: int = 60) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def render_role(view: View, slug: str, candidates: list[dict] | None = None,
                hb_age_min: float | None = None, n_msgs: int = 5) -> str:
    L = []
    role = view.spec.role(slug) if view.spec is not None else None
    if role is not None:
        L.append(f"role: {role.label}")
    st = view.role_state(slug)
    if st:
        sub = f"/{st['sub']}" if st.get("sub") else ""
        holder = f" holder={short(st.get('holder', ''), 50)}" if st.get("holder") else ""
        rescan = f" rescan={st['rescan_utc']}" if st.get("rescan_utc") else ""
        if st.get("state") == "waiting" and st.get("rescan_utc") and st["rescan_utc"] < iso():
            rescan += " RESCAN EXPIRED"
        blk = f" blocked-on={st['blocked_on']}" if st.get("blocked_on") else ""
        L.append(f"state: {st.get('state')}{sub}{holder}{blk}{rescan} (at {st['ts']})")
    else:
        L.append("state: (no state event yet)")
    if hb_age_min is not None:
        L.append(f"heartbeat: {hb_age_min:.0f}m ago (this host)")
    qv = view.current_queue(slug)
    if qv is not None:
        L.append(f"queue v{qv.get('version')}: active={qv.get('active') or '-'} next={','.join(qv.get('next') or []) or '-'}"
                 + (f" asks={len(qv['asks'])}" if qv.get("asks") else ""))
    for h in view.held_unscheduled(slug):
        L.append(f"HELD-UNSCHEDULED {h}: claimed but in neither active nor next (schedule or release it)")
    for b in view.blocked_by(slug):
        L.append(f"BLOCKING {b['role']} on {b['item'] or '(unnamed item)'} since {b['since']}")
    act = view.active(slug)
    L.append("claims: " + (", ".join(fmt_item(c) + (" [CONTESTED: lost]" if c.get("contested") else "")
                                     + (f" base={c['base']}" if c.get("base") else "") for c in act) or "none"))
    q = view.queue(slug)
    if q:
        L.append(f"queue ({len(q)} offered):")
        for i, o in enumerate(q, 1):
            L.append(f"  {i}. {fmt_item(o)} prio={o.get('prio', DEFAULT_PRIO)} by={o['by']} to={o['to']}"
                     + (f" base={o['base']}" if o.get("base") else "")
                     + (f" — {short(o.get('note', ''), 70)}" if o.get("note") else "")
                     + f" [{o['id']}]")
    else:
        L.append("queue: empty")
    if candidates is not None:
        if candidates:
            L.append(f"board candidates (self-fill, top {len(candidates)}):")
            for i, c in enumerate(candidates, 1):
                L.append(f"  {i}. {c['item']} pos={c.get('pos')} — {short(c.get('title', ''), 70)}")
        else:
            L.append("board candidates: none eligible")
    ms = view.msgs_to(slug)[-n_msgs:]
    if ms:
        L.append(f"msgs (last {len(ms)}):")
        for m in ms:
            L.append(f"  {m['ts']} {m['by']}->{m['to']} [{m.get('kind', 'info')}] {short(m.get('text', ''), 90)} [{m['id']}]")
    dec = view.declined(slug)
    if dec:
        L.append("declined: " + "; ".join(f"{k} ({short(r.get('reason', ''), 40)}"
                                          + (f"; retry when {short(r.get('retry', ''), 30)}" if r.get("retry") else "") + ")"
                                          for k, r in dec.items()))
    ld = view.last_done(slug)
    if ld:
        L.append("last-done: " + "; ".join(f"{fmt_item(d)} {d['ts']}" + (f" {d['url']}" if d.get("url") else "") for d in ld))
    return "\n".join(L)


def render_all(view: View, spec, candidates_for=None, hb_ages: dict[str, float] | None = None) -> str:
    """The status table: one row per role."""
    L = [f"# HAG {view.ledger.hag} — ledger view at {iso()}", ""]
    L.append("| role | fill | state | active | next (max 3) | last event |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for r in spec.roles:
        st = view.role_state(r.slug)
        state = "(none)"
        if st:
            state = st.get("state", "?") + (f"/{st['sub']}" if st.get("sub") else "")
            if st.get("holder"):
                state += f" ({short(st['holder'], 40)})"
            if st.get("state") == "waiting" and st.get("rescan_utc") and st["rescan_utc"] < iso():
                state += " RESCAN-EXPIRED"
        qv = view.current_queue(r.slug)
        if qv is not None:  # the role's declared queue is the truth when it exists
            act = qv.get("active") or "—"
            nxt = list(qv.get("next") or [])[:3]
        else:
            act = ", ".join(fmt_item(c) + (" CONTESTED" if c.get("contested") else "") for c in view.active(r.slug)) or "—"
            nxt = [fmt_item(o) for o in view.queue(r.slug)[:3]]
        held = view.held_unscheduled(r.slug)
        if held:
            act += f" (+held-unscheduled {', '.join(held)})"
        if len(nxt) < 3 and candidates_for is not None:
            for c in (candidates_for(r.slug) or [])[:3 - len(nxt)]:
                nxt.append(c["item"] + " (board)")
        fill = getattr(r, "fill", "assigned")
        last = view.last_by.get(r.slug, "never")
        if hb_ages and r.slug in hb_ages and hb_ages[r.slug] is not None:
            last += f" (hb {hb_ages[r.slug]:.0f}m)"
        if not r.session:
            state = "parked (no session)"
        elif getattr(r, "fill", "") == "none" and r.slug == "hag-relay":
            # the relay writes no state events (a stale one may predate this session), so its
            # row is its waiter's liveness
            try:
                pid = open(os.path.join("/tmp", view.ledger.hag, "relay-watch.pid")).read().strip()
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    state = f"relaying (relay-watch pid {pid})" if b"relay-watch" in fh.read() else "relay-watch DEAD"
            except (OSError, ValueError):
                state = "relay-watch DEAD"
        L.append(f"| **{r.label}** | {fill} | {state} | {act} | {', '.join(nxt) or '—'} | {last} |")
    pool = [o for o in view.offers.values() if o["status"] == "open" and o["to"] == "*"]
    if pool:
        L.append("")
        L.append(f"pool offers (to=*): " + ", ".join(fmt_item(o) for o in pool))
    to_user = [m for m in view.msgs if m.get("to") == "user"][-5:]
    if to_user:
        L.append("")
        L.append("msgs to user (last 5):")
        for m in to_user:
            L.append(f"- {m['ts']} {m['by']} [{m.get('kind', 'info')}] {short(m.get('text', ''), 100)}")
    if view.contested:
        L.append("")
        L.append("CONTESTED claims: " + "; ".join(f"{k}: {', '.join(c['by'] for c in cs)}" for k, cs in view.contested.items()))
    if view.violations:
        L.append("")
        L.append(f"permission violations ignored: {len(view.violations)} (see `hag.py ledger-lint`)")
    return "\n".join(L)


# ---- lint --------------------------------------------------------------------

def lint(view: View) -> list[tuple[str, str]]:
    out = []
    for e, why in view.violations:
        out.append(("E1", f"{e['ts']} {e.get('by')} {e.get('type')} {e.get('item') or e.get('to', '')}: {why}"))
    for key, cs in view.contested.items():
        out.append(("E2", f"contested claim on {key}: {', '.join(c['by'] for c in cs)} (loser must release kind=lost-race)"))
    for e in view.ledger.events():
        if e.get("_writer_file") != re.sub(r"[^A-Za-z0-9_.-]", "-", e.get("by", "")):
            out.append(("E3", f"{e['ts']} event by {e.get('by')} lives in {e.get('_writer_file')}.jsonl (one writer per file)"))
        if e.get("type") == "release" and e.get("kind") not in RELEASE_KINDS:
            out.append(("W1", f"{e['ts']} release by {e.get('by')} kind={e.get('kind')!r} not in {RELEASE_KINDS}"))
        if e.get("type") == "msg" and e.get("kind", "info") not in MSG_KINDS:
            out.append(("W2", f"{e['ts']} msg by {e.get('by')} kind={e.get('kind')!r} not in {MSG_KINDS}"))
        if e.get("type") in ("offer", "claim") and not e.get("verb"):
            out.append(("W3", f"{e['ts']} {e['type']} {e.get('item')} without a verb (:fix/:review/...)"))
        if e.get("type") == "offer" and not e.get("base"):
            out.append(("W4", f"{e['ts']} offer {e.get('item')} to {e.get('to')} without a base (repo@sha)"))
    for o in view.offers.values():
        if o["status"] == "open":
            try:
                age_h = (utcnow() - parse_ts(o["ts"])).total_seconds() / 3600
            except Exception:
                continue
            if age_h > 72:
                out.append(("W5", f"offer {fmt_item(o)} to {o['to']} open {age_h:.0f}h (stale base? withdraw or re-offer)"))
    return out


# ---- telemetry bridge --------------------------------------------------------

def as_telemetry(ledger: Ledger, spec=None, since=None, until=None) -> list[dict]:
    """Ledger events reshaped into telemetry-schema events so hag_report can
    fold them in with the local event log. `state` -> session_transition;
    everything else -> ledger_<type>."""
    eco = {r.slug: r.ecosystem for r in spec.roles} if spec is not None else {}
    sess = {r.slug: (r.session or r.slug) for r in spec.roles} if spec is not None else {}
    out = []
    for e in ledger.events():
        try:
            t = parse_ts(e["ts"])
        except Exception:
            continue
        if since and t < since:
            continue
        if until and t > until:
            continue
        by = e.get("by", "?")
        base = {"ts_utc": e["ts"], "hag": ledger.hag, "session": e.get("session") or sess.get(by, by),
                "ecosystem": eco.get(by, "ledger"), "slug": by, "run": {}, "ledger_id": e.get("id", ""),
                "channel": "ledger"}
        if e["type"] == "state":
            ev = dict(base, type="session_transition", from_state="", to_state=e.get("state", ""),
                      to_sub=e.get("sub", ""), holder=e.get("holder", ""), monitor=e.get("monitor", ""),
                      rescan_min=e.get("rescan_min", ""), note=e.get("note", ""), via="ledger")
        else:
            ev = dict(base, type=f"ledger_{e['type']}")
            for k, v in e.items():
                if k.startswith("_") or k in ("ts", "by", "type", "id"):
                    continue
                ev[k] = v
        out.append(ev)
    return out
