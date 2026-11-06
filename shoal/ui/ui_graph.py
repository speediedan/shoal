#!/usr/bin/env python3
"""The graph feed (v1): one read-only document of every shoal on a host as a network.

  shoal ui graph [--shoal ID ...] [--json] [--since CURSOR] [--events N] [--window-rate S]
  shoal ui graph-feed [--interval S] [--out PATH] [--once]

Nodes are roles (their rows from the `ui status` read model, their state from `role_time.classify`
over cached native status), the maintainer (`user`), each shoal's system writer (which also carries
the retired wake relay's historical traffic), and outside seats (`ext:<slug>`). Edges are ledger events aggregated per
(src, dst, class) with counts over a rate window, an hour and a day, plus edges that need no event:
`blocked-on` (a next item's blocker names another role) and `alias` (an outside seat that is a role
in another shoal). Events are the capped tail, numbered by `seq`, with an opaque cursor of per-file
byte offsets so a client asks only for what is new. Contract and sources: docs/ui.md.

READ-ONLY by construction: ledger files are only opened for reading (never pulled, committed or
written), no role is messaged or woken, `next` is never called, and the tick makes no provider or
network call: role rows come from the status cache when it is fresh, else from `role_row` with a local
harness listing (`claude agents`, read-only, at most once per ROWS_MAX_AGE_S, no wake), and native turn
state only from role-time cache files.
"""
from __future__ import annotations

import base64
import calendar
import collections
import glob
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
for _p in (PKG, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
import ui_status  # noqa: E402

GRAPH_V = 1
RATE_WINDOW_S = 300
RECENT_S = 3600
DAY_S = 86400
WEEK_S = 7 * DAY_S
EVENTS_MAX = 200
FEED_INTERVAL_S = 5.0
ROWS_MAX_AGE_S = ui_status.CACHE_MAX_AGE_S  # a role row built here is reused this long (no re-probe per tick)
RATE_HALF_LIFE_S = 3600.0
DONE_RECENT_MAX = 10
EDGE_RECENT_MAX = 3
SUMMARY_MAX = 120
RELAY_WRITER = "wake-relay"
EVENT_TYPES = ("msg", "offer", "withdraw", "claim", "release", "done", "grant")
EDGE_CLASSES = ("msg", "broadcast", "ask", "health", "offer", "grant", "handoff", "blocked-on", "alias")
ASK_KINDS = ("request", "ask")
JOB_WAITS = ("local-ci", "remote-ci", "lease")
# role-time reads unknown when a source is past its budget; the graph shows the posture instead
STALE_EVENTS = ("stale-source", "missing-source")


def _epoch(ts: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime((ts or "")[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return None


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _short(s, n: int = SUMMARY_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "~"


# ---- incremental ledger reading ---------------------------------------------------------

class LedgerTail:
    """Per-file byte offsets over the ledger JSONL files, kept in memory: each `refresh` reads only
    the bytes appended since the last one, and only whole lines (a line still being written waits
    for the next read). Events are parsed the way `Ledger.events` parses them (`by` defaults to the
    writer file, `_writer_file`, `_n` line number) plus `_off` (the line's start byte), `_key`
    (`<shoal>/<file>`) and `_t` (epoch seconds). Files are opened read-only; nothing is written."""

    def __init__(self):
        self.files: dict[str, dict] = {}  # key -> {"off", "n", "events"}

    def refresh(self, sid: str, ldir: str) -> list[dict]:
        new: list[dict] = []
        try:
            names = sorted(f for f in os.listdir(ldir) if f.endswith(".jsonl"))
        except OSError:
            return new
        for fn in names:
            key = f"{sid}/{fn}"
            st = self.files.setdefault(key, {"off": 0, "n": 0, "events": []})
            path = os.path.join(ldir, fn)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < st["off"]:
                # the ledger is append-only; a shorter file was rewritten under us: start it over
                st.update(off=0, n=0, events=[])
            if size == st["off"]:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(st["off"])
                    chunk = fh.read(size - st["off"])
            except OSError:
                continue
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            writer = fn[:-6]
            pos = st["off"]
            for raw in chunk[: end + 1].split(b"\n")[:-1]:
                start, n = pos, st["n"]
                pos += len(raw) + 1
                st["n"] += 1
                line = raw.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or "ts" not in e or "type" not in e:
                    continue
                e.setdefault("by", writer)
                e.update(_writer_file=writer, _n=n, _off=start, _key=key, _sid=sid, _t=_epoch(e["ts"]))
                st["events"].append(e)
                new.append(e)
            st["off"] = pos
        return new

    def events(self, sid: str) -> list[dict]:
        """Every event of one shoal in `Ledger.events` order (ts, writer, line)."""
        evs = [e for k, st in self.files.items() if k.split("/", 1)[0] == sid for e in st["events"]]
        evs.sort(key=lambda e: (e["ts"], e.get("by", ""), e["_n"]))
        return evs

    def offsets(self) -> dict[str, int]:
        return {k: st["off"] for k, st in self.files.items()}

    def writers(self, sid: str) -> list[str]:
        return [k.split("/", 1)[1][:-6] for k in self.files if k.split("/", 1)[0] == sid]


def encode_cursor(offsets: dict[str, int], seq: int) -> str:
    raw = json.dumps({"o": offsets, "s": int(seq)}, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c: str) -> tuple[dict[str, int], int] | None:
    try:
        raw = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4))
        d = json.loads(raw)
        return {str(k): int(v) for k, v in d["o"].items()}, int(d["s"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


# ---- node ids, item URLs, edge classes ------------------------------------------------------

def node_id(sid: str, name: str, roles: set[str]) -> str:
    """Writer or target name to node id: `user`, `<shoal>:shoal` (also the retired wake relay's
    writer, folded into the system node), `<shoal>:*` (a broadcast target, not a node), `ext:<slug>`,
    else `<shoal>:<slug>`."""
    name = str(name or "")
    if name == "user":
        return "user"
    if name in roles:
        return f"{sid}:{name}"
    if name in ("shoal", RELAY_WRITER):
        return f"{sid}:shoal"
    if name == "*":
        return f"{sid}:*"
    if name.startswith("ext:") or name.startswith("ext-"):
        return "ext:" + name[4:]
    return f"{sid}:{name}"


def repo_map(spec) -> dict[str, str]:
    """{repo name (lower): `owner/repo`} from the spec's repos (else the host's default repos)."""
    out = {}
    for r in spec.effective_repos:
        r = str(r).strip().strip("/")
        if "/" in r:
            out.setdefault(r.split("/")[-1].lower(), r)
    return out


def item_url(item: str, repos: dict[str, str]) -> str | None:
    """`https://github.com/<owner>/<repo>/issues/<n>` for `repo#n[:verb]` or `owner/repo#n[:verb]`;
    the owner comes from the item, else the spec's repos, else the configured default owner.
    GitHub redirects issues/ to pull/ for a pull request, so one form serves both."""
    from ledger import split_item
    key, _verb = split_item(str(item or ""))
    if "#" not in key:
        return None
    repo, num = key.rsplit("#", 1)
    if not num.isdigit() or not repo:
        return None
    if "/" not in repo:
        full = repos.get(repo.lower())
        if not full:
            owner = config.default_owner()
            full = f"{owner}/{repo}" if owner else ""
        repo = full
    if not repo or repo.count("/") != 1:
        return None
    return f"https://github.com/{repo}/issues/{num}"


def standalone_doc(e: dict, rows: list[dict]) -> dict:
    """A saved standalone session for its lattice card: which one it is, and whether it runs now. Only Claude can be read here."""
    session, harness = str(e.get("session") or ""), str(e.get("harness") or "claude")
    state = "unread"
    if harness == "claude":
        row = ui_status.claude_session(rows, session, str(e.get("job") or "")) or {}
        if not row or row.get("state") in ("stopped", "done"):
            state = "stopped"
        elif row.get("state") == "blocked":
            state = "blocked"
        else:
            state = "busy" if row.get("status") == "busy" else "alive"
    return {"harness": harness, "id": session[:12], "state": state}


def board_doc(spec, rows: list[dict], digest: dict | None, now: float) -> dict | None:
    """The shoal's priority boards for the rail, None when it has none. Each is a link and, from this host's last
    digest (the only board read), its items as done, open, in flight (a role's active item) and assigned (in a role's
    queue, nobody's active item). Queued work not open on a board counts under `off_board`, so the four add up to the board."""
    from digest import DIGEST_MAX_AGE_MIN
    from ledger import item_key
    from sources import project_url
    srcs = [s for s in spec.sources or [] if s.get("kind") == "github-project"]
    if not srcs:
        return None
    active = {item_key(r["active"]) for r in rows if r.get("active")}
    queued = {item_key(str(n.get("item") or "")) for r in rows for n in r.get("next") or []} - active
    digest = digest or {}
    read = {b.get("source"): b for b in digest.get("boards") or []}
    entries, on_board = [], set()
    for src in srcs:
        desc = f"github-project:{src['owner']}/{src['number']}"
        got = read.get(desc)
        entry = {"id": f"{src['owner']}/{src['number']}", "title": (got or {}).get("title") or "",
                 "url": (got or {}).get("url") or project_url(src)}
        if got:
            keys = {item_key(k) for k, it in (digest.get("items") or {}).items() if it.get("source") == desc}
            on_board |= keys
            flying, assigned = len(keys & active), len(keys & queued)
            entry["counts"] = {"done": int(got.get("done") or 0), "open": len(keys) - flying - assigned,
                               "in_flight": flying, "assigned": assigned}
        entries.append(entry)
    out = {"items": entries}
    if read:
        out["off_board"] = {"in_flight": len(active - on_board), "assigned": len(queued - on_board)}
        t = _epoch(str(digest.get("generated_utc") or ""))
        if t is not None:
            age = int(max(0.0, now - t))
            out.update(age_s=age, stale=age > DIGEST_MAX_AGE_MIN * 60)
    return out


def classify_event(sid: str, e: dict, roles: set[str]) -> tuple[str, str, str | None]:
    """(class, src, dst) for one ledger event. `dst` is None for events with no target."""
    typ, by = e.get("type"), e.get("by", "")
    src = node_id(sid, by, roles)
    if typ == "msg":
        to = str(e.get("to") or "")
        kind = e.get("kind") or ""
        if to == "*":
            return "broadcast", src, f"{sid}:*"
        dst = node_id(sid, to, roles)
        if kind == "health":
            return "health", src, dst
        if dst == "user" and kind in ASK_KINDS:
            return "ask", src, dst
        return "msg", src, dst
    if typ == "offer":
        return "offer", src, node_id(sid, e.get("to") or "*", roles)
    if typ == "grant":
        return "grant", src, (node_id(sid, e["role"], roles) if e.get("role") else None)
    if typ == "release" and e.get("kind") == "handoff" and e.get("to"):
        return "handoff", src, node_id(sid, e["to"], roles)
    if typ == "system" and e.get("mode"):
        return "system", src, None
    return str(typ), src, None


def summary(e: dict) -> str:
    typ = e.get("type")
    item = e.get("item") or ""
    if typ == "msg":
        return _short(e.get("text"))
    if typ == "done":
        return _short(f"done {item} {e.get('kind') or e.get('verb') or ''}".strip())
    if typ == "offer":
        return _short(f"offer {item} to {e.get('to', '')}" + (f": {e['note']}" if e.get("note") else ""))
    if typ == "release":
        return _short(f"release {item} ({e.get('kind', '')})" + (f": {e['reason']}" if e.get("reason") else ""))
    if typ == "grant":
        return _short(f"grant {e.get('state', '')} {e.get('kind') or ''}: {e.get('scope') or e.get('grant') or ''}")
    if typ == "system" and e.get("mode"):
        return _short(f"mode {e['mode']} {e.get('state', '')}")
    return _short(f"{typ} {item}".strip())


def recent_doc(e: dict, now: float) -> dict:
    """One entry of an edge's `recent` list (newest first): what its hover card shows."""
    d = {"ts": e["ts"], "age_s": int(max(0.0, now - e["_t"])), "kind": str(e.get("kind") or e.get("verb") or e.get("mode") or ""),
         "summary": summary(e)}
    if e.get("item"):
        d["item"] = str(e["item"])
    if e.get("url"):
        d["url"] = e["url"]
    return d


def tail_event(e: dict) -> bool:
    """Events the tail carries: traffic and work changes, not postures, queue versions or beats."""
    typ = e.get("type")
    return typ in EVENT_TYPES or (typ == "system" and bool(e.get("mode")))


# ---- role state ---------------------------------------------------------------------------

def native_cache_paths(sid: str) -> list[str]:
    """This host's role-time native cache plus copies of other hosts' (`role-time-native-<host>.json`,
    written beside the observation journal by `role-time observe`). Only ever read."""
    from role_time_adapters import default_cache_path
    p = default_cache_path(sid)
    return [p] + sorted(glob.glob(os.path.join(os.path.dirname(p), "role-time-native-*.json")))


def posture_state(row: dict) -> tuple[str, str]:
    """Best-effort (class, event) from a status row when no native observation exists for the role.
    Same vocabulary as role_time; `state.source` says it is a posture reading."""
    import blockers
    import role_time as rt
    reasons = set((row.get("can_progress") or {}).get("reasons") or [])
    posture, sub, sess = row.get("posture") or "", row.get("sub") or "", row.get("session") or ""
    if posture == "parked" or sess == "parked":
        return "excluded", "parked"
    if posture in ("offline", "paused"):
        return "excluded", posture
    if "dead-worker" in reasons:
        return "unknown", "session-missing"
    if "blocked-on-question" in reasons:
        return "wait", "user-prompt"
    if posture == "wait" and sub in rt.WAITS:
        return "wait", sub
    if sess == "working":
        return "work", "turn"
    if posture == "wait" and sub == "queue-empty":
        return ("wait", "assignment") if row.get("fill") == "assigned" else ("idle", "no-work")
    if posture == "wait":
        return "unknown", "unmapped-state"
    nxt = row.get("next") or []
    if row.get("active") or (nxt and any(not n.get("blocker") or n.get("stale") for n in nxt)):
        return "wait", "self"
    if nxt:
        kinds = {rt.BLOCKER_EVENT.get(blockers.kind(n.get("blocker") or ""), "hold") for n in nxt}
        return "wait", kinds.pop() if len(kinds) == 1 else "mixed"
    if row.get("asks"):
        return "wait", "refill"
    return ("wait", "assignment") if row.get("fill") == "assigned" else ("idle", "no-work")


def role_states(spec, view, rows: list[dict], now: float) -> dict[str, dict]:
    """{slug: {class, event, group, source}}: `role_time.classify` over snapshots built from the
    cached native status and the ledger part (no probe); a role with no native observation reads its
    posture instead (`source: posture`)."""
    import role_time as rt
    from role_time_adapters import build_snapshots, ledger_fetched_at, read_native_caches
    out: dict[str, dict] = {}
    try:
        natives = read_native_caches(native_cache_paths(spec.sid), spec.sid)
        snaps = build_snapshots(spec, view, ledger_fetched_at(spec.ledger_dir), natives, now) if natives else {}
    except Exception:  # noqa: BLE001 - a read model never fails on an advisory source
        natives, snaps = {}, {}
    for row in rows:
        slug = row.get("slug")
        snap = snaps.get(slug)
        if snap is not None and snap.get("native"):
            c, e, q, _until = rt.classify(snap, now)
            src = "role-time"
            if c == "unknown" and e in STALE_EVENTS:
                # a source past its budget says nothing about the role; its posture still does
                stale = e
                c, e = posture_state(row)
                q, src = (*q, f"role-time:{stale}"), "posture"
        else:
            c, e = posture_state(row)
            q, src = (), "posture"
        out[slug] = {"class": c, "event": e, "group": rt.WAIT_GROUP.get(e, "") if c == "wait" else "",
                     "source": src, "quality": list(q)}
    return out


# ---- the builder ------------------------------------------------------------------------------

class Edge:
    __slots__ = ("src", "dst", "cls", "sid", "n_rate", "n_1h", "n_24h", "decayed", "last_t", "structural", "open",
                 "stale", "recent")

    def __init__(self, src, dst, cls, sid, structural=False):
        self.src, self.dst, self.cls, self.sid = src, dst, cls, sid
        self.n_rate = self.n_1h = self.n_24h = 0
        self.decayed = 0.0
        self.last_t = None
        self.structural = structural
        self.open = 0
        self.stale = None
        self.recent = collections.deque(maxlen=EDGE_RECENT_MAX)

    def add(self, t: float, now: float, rate_s: float, e: dict | None = None) -> None:
        age = max(0.0, now - t)
        if age < DAY_S:
            self.n_24h += 1
            self.decayed += 0.5 ** (age / RATE_HALF_LIFE_S)
            if age < RECENT_S:
                self.n_1h += 1
            if age < rate_s:
                self.n_rate += 1
            if e is not None:  # `Tail.events` sorts by timestamp, so the deque keeps the newest
                self.recent.append(e)
        if self.last_t is None or t > self.last_t:
            self.last_t = t

    def doc(self, now: float) -> dict:
        d = {"id": f"{self.src}|{self.dst}|{self.cls}", "src": self.src, "dst": self.dst, "class": self.cls,
             "n_rate": self.n_rate, "n_1h": self.n_1h, "n_24h": self.n_24h,
             # an exponentially weighted rate with a one-hour half-life: a steady r per hour reads r
             "rate_per_h": round(self.decayed * math.log(2) * 3600.0 / RATE_HALF_LIFE_S, 3),
             "last_ts": _iso(self.last_t) if self.last_t is not None else None,
             "last_age_s": int(now - self.last_t) if self.last_t is not None else None,
             "shoal": self.sid}
        if self.structural:
            d["structural"] = True
        if self.cls == "ask":
            d["open"] = self.open
        if self.stale is not None:
            d["stale"] = self.stale
        if self.recent:
            d["recent"] = [recent_doc(e, now) for e in reversed(self.recent)]
        return d


class GraphBuilder:
    """Builds the graph document. One instance per process: the feed keeps it across ticks, so the
    ledger tail, event numbering and role rows it built itself persist in memory."""

    def __init__(self, ids: list[str] | None = None, root: str = "", rate_s: float = RATE_WINDOW_S,
                 max_events: int = EVENTS_MAX, use_cache: bool = True):
        self.ids = ids or None
        self.root = root
        self.rate_s = float(rate_s)
        self.max_events = int(max_events)
        self.use_cache = use_cache and not root
        self.tail = LedgerTail()
        self.seq = 0
        self.recent: collections.deque = collections.deque(maxlen=max(self.max_events, 1))
        self._rows: dict[str, tuple[float, list[dict]]] = {}
        self._harness: tuple[float, list[dict]] | None = None
        self._slugs: dict[str, set[str]] = {}
        self._digests: dict[str, tuple[float, dict]] = {}

    def _digest(self, sid: str) -> dict | None:
        """This host's last digest of the shoal, only what the rail needs; reread when the file changes, since it holds
        a board's worth of bodies and the feed ticks every few seconds."""
        from digest import digest_path
        p = digest_path(sid)
        try:
            mtime = os.path.getmtime(p)
            memo = self._digests.get(sid)
            if memo is not None and memo[0] == mtime:
                return memo[1]
            with open(p) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
        keep = {"generated_utc": d.get("generated_utc"), "boards": d.get("boards") or [],
                "items": {k: {"source": it.get("source")} for k, it in (d.get("items") or {}).items()}}
        self._digests[sid] = (mtime, keep)
        return keep

    # -- role rows: the status cache when fresh, else role_row over this builder's own View
    def _cached_shoals(self) -> dict[str, dict]:
        if not self.use_cache:
            return {}
        d = ui_status.read_cache()
        return {s["id"]: s for s in (d or {}).get("shoals", []) if s.get("id") and not s.get("error")}

    def _harness_rows(self, now: float) -> list[dict]:
        if self._harness is None or now - self._harness[0] > ROWS_MAX_AGE_S:
            self._harness = (now, ui_status.claude_rows())
        return self._harness[1]

    def _rows_for(self, spec, view, now: float) -> list[dict]:
        memo = self._rows.get(spec.sid)
        if memo is not None and now - memo[0] <= ROWS_MAX_AGE_S:
            return memo[1]
        import datetime
        dnow = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
        hrows = self._harness_rows(now)
        rows = [ui_status.role_row(spec, view, r, hrows, dnow, opencode_probe=False) for r in spec.roles]
        self._rows[spec.sid] = (now, rows)
        return rows

    # -- one document
    def build(self, since: str = "", now: float | None = None) -> dict:
        from ledger import Ledger, View
        from roles import ShoalSpec
        now = time.time() if now is None else float(now)
        this_host = config.server()
        cached = self._cached_shoals()
        specs, new_events = [], []
        for p in ui_status.discover_all(self.root, self.ids):
            try:
                spec = ShoalSpec.load(p)
            except Exception as exc:  # noqa: BLE001 - one broken spec never hides the others
                specs.append((p, None, str(exc)))
                continue
            led = Ledger(spec.sid, root=spec.ledger_dir or None, sync=False)
            if not led.exists():
                specs.append((p, spec, f"no ledger at {led.ldir}"))
                continue
            new_events += self.tail.refresh(spec.sid, led.ldir)
            specs.append((p, spec, ""))
        # number the new tail events in ts order (the first build numbers the whole history)
        fresh = sorted((e for e in new_events if tail_event(e)), key=lambda e: (e["ts"], e.get("by", ""), e["_n"]))
        for e in fresh:
            self.seq += 1
            e["_seq"] = self.seq
            self.recent.append(e)

        role_slugs = {spec.sid: {r.slug for r in spec.roles} for _p, spec, _err in specs if spec is not None}
        self._slugs = role_slugs
        nodes: dict[str, dict] = {}
        edges: dict[tuple, Edge] = {}
        shoals, hosts = [], {this_host} if this_host else set()
        user = {"id": "user", "kind": "user", "shoal": None, "label": "you",
                "state": {"class": "work", "event": "", "group": "", "needs_you": False},
                "work": {"asks_open": 0, "grants_pending": 0}, "metrics": {"msgs_out_1h": 0, "msgs_in_1h": 0}}
        nodes["user"] = user
        ext_seen: dict[str, set[str]] = {}
        ext_metrics: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

        def edge(src, dst, cls, sid, structural=False) -> Edge:
            k = (src, dst, cls)
            if k not in edges:
                edges[k] = Edge(src, dst, cls, sid, structural)
            return edges[k]

        for p, spec, err in specs:
            if spec is None or err:
                shoals.append({"id": spec.sid if spec else os.path.basename(os.path.dirname(p)), "error": _short(err, 200)})
                continue
            sid, slugs = spec.sid, role_slugs[spec.sid]
            led = Ledger(sid, root=spec.ledger_dir or None, sync=False)
            evs = self.tail.events(sid)
            led._events = evs  # the View folds from memory: the tail already read the files
            view = View(led, spec)
            cs = cached.get(sid)
            rows = cs["roles"] if cs and isinstance(cs.get("roles"), list) else self._rows_for(spec, view, now)
            states = role_states(spec, view, rows, now)
            repos = repo_map(spec)
            home = spec.home or this_host

            # -- per-event aggregation (the last 7 days; edges only from the last day)
            done_by: dict[str, list[dict]] = collections.defaultdict(list)
            per: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)  # by node id
            m = collections.Counter()
            for e in evs:
                t = e.get("_t")
                if t is None:
                    continue
                age = now - t
                if age >= WEEK_S:
                    continue
                typ, by = e.get("type"), e.get("by", "")
                if typ == "done":
                    m["done_7d"] += 1
                if age >= DAY_S:
                    continue
                cls, src, dst = classify_event(sid, e, slugs)
                if typ == "done":
                    m["done_24h"] += 1
                    per[src]["done_24h"] += 1
                    done_by[by].append(e)
                elif typ == "claim":
                    m["claims_24h"] += 1
                    per[src]["claims_24h"] += 1
                elif typ == "offer":
                    m["offers_24h"] += 1
                elif typ == "msg":
                    m["msgs_24h"] += 1
                    if cls == "health" and dst == "user":
                        m["pages_24h"] += 1
                    if age < RECENT_S:
                        m["msgs_1h"] += 1
                        per[src]["msgs_out_1h"] += 1
                        if cls != "broadcast":
                            per[dst]["msgs_in_1h"] += 1
                for name in (by, str(e.get("to") or "")):
                    if (name.startswith("ext:") or name.startswith("ext-")) and name not in slugs:
                        ext_seen.setdefault("ext:" + name[4:], set()).add(sid)
                if dst is not None and cls in EDGE_CLASSES:
                    edge(src, dst, cls, sid).add(t, now, self.rate_s, e)
            for w in self.tail.writers(sid):
                if w.startswith("ext-") and w not in slugs:
                    ext_seen.setdefault("ext:" + w[4:], set()).add(sid)

            # -- the system writer (the retired wake relay's writer folds into it: node_id)
            nodes[f"{sid}:shoal"] = {"id": f"{sid}:shoal", "kind": "system", "shoal": sid, "label": "shoal", "host": home,
                                     "state": {"class": "work", "event": "", "group": ""},
                                     "metrics": {"pages_24h": m["pages_24h"]}}

            # -- roles
            off, frz = view.user_offline(), view.freeze()
            counts = collections.Counter()
            for row in rows:
                slug = row.get("slug")
                nid = f"{sid}:{slug}"
                st = states.get(slug) or {"class": "unknown", "event": "no-observation", "group": "", "source": "posture"}
                counts[st["class"]] += 1
                reasons = list((row.get("can_progress") or {}).get("reasons") or [])
                if row.get("host"):
                    hosts.add(row["host"])
                active = row.get("active")
                nxt = []
                for n in row.get("next") or []:
                    x = {"item": n.get("item"), "url": item_url(n.get("item"), repos)}
                    if n.get("blocker"):
                        x["blocker"] = n["blocker"]
                        x["stale"] = bool(n.get("stale"))
                        dst_role = blocker_role(n["blocker"], slugs)
                        if dst_role and dst_role != slug:
                            be = edge(nid, f"{sid}:{dst_role}", "blocked-on", sid, structural=True)
                            # one edge per role pair: stale only when every blocker behind it is stale
                            be.stale = bool(n.get("stale")) and be.stale is not False
                    nxt.append(x)
                asks = list(row.get("asks") or [])
                if asks:
                    edge(nid, "user", "ask", sid).open = len(asks)
                done = sorted(done_by.get(slug, []), key=lambda e: e["ts"])[-DONE_RECENT_MAX:]
                pr = per.get(nid, {})
                rhost = row.get("host") or home
                job = job_detail(sid, slug, row, this_host) if st["class"] == "wait" and st["event"] in JOB_WAITS else None
                nodes[nid] = {
                    "id": nid, "kind": "role", "shoal": sid, "slug": slug, "label": slug,
                    "ecosystem": row.get("ecosystem") or row.get("harness"), "model": row.get("model"),
                    "host": rhost, "fill": row.get("fill"),
                    "state": {"class": st["class"], "event": st["event"], "group": st["group"],
                              "source": st["source"], "quality": st.get("quality") or [],
                              "posture": row.get("posture") or "", "sub": row.get("sub") or "",
                              "session": row.get("session") or "", "since_s": row.get("posture_age_s"),
                              "needs_you": bool(row.get("needs_you")), "reasons": reasons,
                              "shoal_offline": bool(off),
                              "says": row.get("says") or "",
                              "restart_ready": bool((view.current_queue(slug) or {}).get("restart_ready"))},
                    "work": {"active": {"item": active, "url": item_url(active, repos)} if active else None,
                             "next": nxt, "asks": asks, "offers_open": int(row.get("offers_open") or 0),
                             "done_recent": [{"item": d.get("item"), "url": d.get("url") or item_url(d.get("item"), repos),
                                              "ts": d["ts"]} for d in done]},
                    "metrics": {"done_24h": pr.get("done_24h", 0), "claims_24h": pr.get("claims_24h", 0),
                                "msgs_out_1h": pr.get("msgs_out_1h", 0), "msgs_in_1h": pr.get("msgs_in_1h", 0),
                                "last_write_age_s": row.get("last_write_age_s"),
                                "heartbeat_age_s": row.get("heartbeat_age_s"), "context_k": row.get("context_k")},
                    "view": row.get("view") or [config.cli(), "view", sid, slug],
                }
                if job:
                    nodes[nid]["state"]["job"] = job
                    nodes[nid]["state"]["job_host"] = job["host"]
                    hosts.add(job["host"])
                if isinstance(row.get("grants_pending"), list):  # the status row's own list, when it has one
                    nodes[nid]["work"]["grants_pending"] = row["grants_pending"]
                    user["work"]["grants_pending"] += len(row["grants_pending"])
            user["work"]["asks_open"] += sum(len(r.get("asks") or []) for r in rows)
            umet = per.get("user", {})
            user["metrics"]["msgs_out_1h"] += umet.get("msgs_out_1h", 0)
            user["metrics"]["msgs_in_1h"] += umet.get("msgs_in_1h", 0)
            if home:
                hosts.add(home)
            from role_time_adapters import ledger_fetched_at
            fetched = ledger_fetched_at(spec.ledger_dir) if spec.ledger_dir else None
            sdoc = {
                "id": sid, "gen": f"r{spec.incarnation + 1}", "retired": bool(spec.retired_utc), "home": home,
                # how long ago this host's clone last fetched: the feed never pulls, so a renderer warns when it is old
                "ledger_age_s": int(max(0.0, now - fetched)) if fetched else None,
                "mode": {"offline": bool(off), "freeze": bool(frz)},
                "metrics": {"roles": len(rows), "working": counts["work"], "waiting": counts["wait"],
                            "needs_you": sum(1 for r in rows if r.get("needs_you")),
                            "stalled": sum(1 for r in rows if not (r.get("can_progress") or {}).get("ok", True)
                                           and r.get("session_id")),
                            "asks_open": sum(len(r.get("asks") or []) for r in rows),
                            "pages_24h": m["pages_24h"], "done_24h": m["done_24h"], "done_7d": m["done_7d"],
                            "claims_24h": m["claims_24h"], "offers_24h": m["offers_24h"],
                            "msgs_1h": m["msgs_1h"], "msgs_24h": m["msgs_24h"], "work_ratio_24h": None},
            }
            gp = (cs or {}).get("grants_pending_n")
            if gp is None and any(isinstance(r.get("grants_pending"), list) for r in rows):
                gp = sum(len(r.get("grants_pending") or []) for r in rows)
            if gp is not None:
                sdoc["metrics"]["grants_pending_n"] = gp
            boards = board_doc(spec, rows, self._digest(sid), now)
            if boards:
                sdoc["boards"] = boards
            shoals.append(sdoc)
            # metrics keyed by node id for the synthetic nodes
            for nid_, c in per.items():
                if nid_ in nodes and nodes[nid_]["kind"] == "system":
                    nodes[nid_]["metrics"].update({k: v for k, v in c.items() if k.startswith("msgs_")})
            for nid_, c in per.items():
                if nid_.startswith("ext:"):
                    ext_metrics[nid_].update({k: v for k, v in c.items() if k.startswith("msgs_")})

        user["state"]["needs_you"] = user["work"]["asks_open"] > 0 or any(
            n["state"].get("needs_you") for n in nodes.values() if n["kind"] == "role")

        # -- outside seats, aliased to a role of the same slug in another shoal
        import ui_herdr
        saved = {str(e.get("label") or str(e.get("session"))[:12]): e for e in ui_herdr.load_standalone(raw=True) if not e.get("host")}
        for label in saved:  # a saved session is on the picture whether or not it ever wrote to a ledger
            ext_seen.setdefault("ext:" + label, set())
        for eid in sorted(ext_seen):
            slug = eid[4:]
            home_of = sorted(s for s, sl in role_slugs.items() if slug in sl and s not in ext_seen[eid])
            alias = f"{home_of[0]}:{slug}" if home_of else None
            st = nodes.get(alias, {}).get("state") if alias else None
            em = ext_metrics.get(eid, {})
            nodes[eid] = {"id": eid, "kind": "external", "shoal": None, "label": slug, "alias_of": alias, "saved": slug in saved,
                          "host": nodes.get(alias, {}).get("host") if alias else None,
                          "state": ({"class": st["class"], "event": st["event"], "group": st["group"]} if st
                                    else {"class": "unknown", "event": "", "group": ""}),
                          "metrics": {"msgs_out_1h": em.get("msgs_out_1h", 0), "msgs_in_1h": em.get("msgs_in_1h", 0)}}
            if slug in saved:
                nodes[eid]["standalone"] = standalone_doc(saved[slug], self._harness_rows(now))
            if alias:
                edge(eid, alias, "alias", None, structural=True)

        # -- every edge and event endpoint is a node (a removed role, an unknown writer)
        tail_events = self._tail(since)
        ends = {x for e in edges.values() for x in (e.src, e.dst)}
        ends |= {x for ev in tail_events["events"] for x in (ev["src"], ev["dst"]) if x}
        for x in sorted(ends):
            if x in nodes or x.endswith(":*"):
                continue
            sid, _, name = x.partition(":")
            nodes[x] = {"id": x, "kind": "writer", "shoal": sid or None, "label": name or x,
                        "state": {"class": "unknown", "event": "", "group": ""}, "metrics": {}}

        hagg = {h: collections.Counter() for h in hosts}
        for n in nodes.values():
            if n["kind"] == "role" and n.get("host"):
                c = hagg.setdefault(n["host"], collections.Counter())
                c["roles"] += 1
                c["working"] += n["state"]["class"] == "work"
                c["waiting"] += n["state"]["class"] == "wait"
                c["needs_you"] += bool(n["state"]["needs_you"])
                c["msgs_1h"] += n["metrics"]["msgs_out_1h"]
        host_of = {i: n.get("host") for i, n in nodes.items()}
        edocs = []
        for e in sorted(edges.values(), key=lambda e: (e.sid or "", e.src, e.dst, e.cls)):
            d = e.doc(now)
            hs, hd = host_of.get(e.src), host_of.get(e.dst)
            d["cross_host"] = bool(hs and hd and hs != hd)
            edocs.append(d)
        doc = {"v": GRAPH_V, "kind": "shoal-graph", "host": this_host, "ts": _iso(now),
               "cursor": tail_events["cursor"],
               "windows": {"rate_s": int(self.rate_s), "recent_s": RECENT_S, "day_s": DAY_S},
               "hosts": [{"id": f"host:{h}", "name": h, "home": h == this_host,
                          **{k: hagg[h][k] for k in ("roles", "working", "waiting", "needs_you", "msgs_1h")}}
                         for h in sorted(hagg)],
               "shoals": shoals, "nodes": list(nodes.values()), "edges": edocs,
               "events": tail_events["events"]}
        if tail_events.get("cursor_reset"):
            doc["cursor_reset"] = True
        return doc

    def _event_doc(self, e: dict, seq: int) -> dict:
        sid = e["_sid"]
        cls, src, dst = classify_event(sid, e, self._slugs.get(sid, set()))
        d = {"seq": seq, "ts": e["ts"], "shoal": sid, "src": src, "dst": dst, "type": e.get("type"),
             "class": cls, "kind": str(e.get("kind") or e.get("verb") or e.get("mode") or ""),
             "item": str(e.get("item") or ""), "summary": summary(e)}
        if e.get("url"):
            d["url"] = e["url"]
        return d

    def _tail(self, since: str) -> dict:
        """The events section and the next cursor. Without `since`: the last N tail events this
        builder has numbered. With it: only events whose bytes lie past the cursor's offsets,
        numbered on from the cursor's `seq`."""
        offsets = self.tail.offsets()
        if not since:
            evs = list(self.recent)[-self.max_events:] if self.max_events > 0 else []
            return {"events": [self._event_doc(e, e["_seq"]) for e in evs],
                    "cursor": encode_cursor(offsets, self.seq)}
        cur = decode_cursor(since)
        if cur is None:
            out = self._tail("")
            out["cursor_reset"] = True
            return out
        coff, cseq = cur
        newer = [e for st in self.tail.files.values() for e in st["events"]
                 if tail_event(e) and e["_off"] >= coff.get(e["_key"], 0)]
        newer.sort(key=lambda e: (e["ts"], e.get("by", ""), e["_n"]))
        numbered = [(e, cseq + i + 1) for i, e in enumerate(newer)]
        keep = numbered[-self.max_events:] if self.max_events > 0 else []
        return {"events": [self._event_doc(e, s) for e, s in keep],
                "cursor": encode_cursor(offsets, cseq + len(newer))}


def job_detail(sid: str, slug: str, row: dict, this_host: str) -> dict | None:
    """What the job meta says about the job a CI or lease wait names (`job:<name>` target): its unit,
    where it runs (a placement's host, else a remote `--on` host, else this host, where the meta
    lives), whether it was placed and granted, and how long ago it started. File reads only, the same
    records `role_time_adapters.job_part` reads; None when the wait names no job this host knows."""
    target = str((row.get("wait") or {}).get("target") or "")
    if not target.startswith("job:"):
        return None
    try:
        import jobs
        m = jobs.job_meta(sid, target[len("job:"):], slug)
        if not m:
            return None
        pl = m.get("placement") or {}
        granted = bool(m.get("lease_granted"))
        if not granted and not m.get("host") and m.get("unit"):
            granted = os.path.exists(jobs.paths(sid, m["unit"])["granted"])
    except Exception:  # noqa: BLE001 - a missing or unreadable record is no evidence either way
        return None
    t = _epoch(m.get("started_utc") or "")
    return {"unit": m.get("unit") or "", "host": pl.get("host") or m.get("host") or this_host,
            "placed": bool(pl), "granted": granted,
            "started_age_s": int(time.time() - t) if t is not None else None}


def blocker_role(blocker: str, slugs: set[str]) -> str:
    """The role a blocker names: `<role>:<item>`, or `role:<role>[:...]`; "" when none."""
    parts = str(blocker or "").split(":")
    if parts[0] in slugs:
        return parts[0]
    if parts[0] == "role" and len(parts) > 1 and parts[1] in slugs:
        return parts[1]
    return ""


def build(ids: list[str] | None = None, root: str = "", since: str = "", max_events: int = EVENTS_MAX,
          rate_s: float = RATE_WINDOW_S, now: float | None = None) -> dict:
    return GraphBuilder(ids, root, rate_s, max_events).build(since, now)


# ---- output ---------------------------------------------------------------------------------

def graph_path() -> str:
    """`graph.json` beside the status cache (`status.json`), in the per-user runtime dir."""
    return os.path.join(os.path.dirname(ui_status.cache_path()), "graph.json")


def write_doc(doc: dict, path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def render_text(doc: dict, top: int = 5) -> str:
    nodes = {n["id"]: n for n in doc["nodes"]}
    out = []
    for s in doc["shoals"]:
        if s.get("error"):
            out.append(f"{s['id']}: ERROR {s['error']}")
            continue
        m = s["metrics"]
        roles = [n for n in doc["nodes"] if n["kind"] == "role" and n["shoal"] == s["id"]]
        by = collections.Counter(n["state"]["class"] + (f"/{n['state']['group']}" if n["state"]["group"] else "")
                                 for n in roles)
        flags = [x for x, on in (("OFFLINE", s["mode"]["offline"]), ("FREEZE", s["mode"]["freeze"])) if on]
        out.append("  ".join([s["id"], f"gen {s['gen']}", f"home {s['home']}"] + flags))
        out.append("  roles: " + (", ".join(f"{k} {v}" for k, v in sorted(by.items())) or "none"))
        out.append(f"  done 24h {m['done_24h']}  7d {m['done_7d']}  claims 24h {m['claims_24h']}  offers 24h "
                   f"{m['offers_24h']}  msgs 1h {m['msgs_1h']}  24h {m['msgs_24h']}  pages 24h {m['pages_24h']}")
        need = [n for n in roles if n["state"].get("needs_you")]
        out.append("  needs you: " + (", ".join(
            f"{n['slug']} ({n['state']['class']}:{n['state']['event']}; {', '.join(n['state']['reasons']) or '-'})"
            for n in need) or "none"))
        es = sorted((e for e in doc["edges"] if e["shoal"] == s["id"] and e["n_24h"]),
                    key=lambda e: (-e["rate_per_h"], -e["n_24h"]))[:top]
        if es:
            out.append("  top edges (rate/h, 1h, 24h):")
            for e in es:
                lab = lambda i: "*" if i.endswith(":*") else nodes.get(i, {}).get("label", i)  # noqa: E731
                out.append(f"    {lab(e['src'])} -> {lab(e['dst'])} {e['class']}: "
                           f"{e['rate_per_h']:.2f}, {e['n_1h']}, {e['n_24h']}")
        out.append("")
    u = nodes.get("user", {}).get("work", {})
    out.append(f"you: asks open {u.get('asks_open', 0)}, grants pending {u.get('grants_pending', 0)}; "
               f"{len(doc['nodes'])} nodes, {len(doc['edges'])} edges, {len(doc['events'])} events")
    return "\n".join(out)


def add_args(p) -> None:
    p.add_argument("--shoal", dest="ids", action="append", default=[], help="a shoal id (repeatable); default all")
    p.add_argument("--ledger-root", default="", help="ledger clone root (default: host config); skips the status cache")
    p.add_argument("--events", type=int, default=EVENTS_MAX, help=f"events in the tail (default {EVENTS_MAX})")
    p.add_argument("--window-rate", type=float, default=RATE_WINDOW_S,
                   help=f"rate window in seconds for n_rate (default {RATE_WINDOW_S})")


def cmd_graph(a) -> int:
    doc = build(a.ids or None, a.ledger_root, a.since, a.events, a.window_rate)
    print(json.dumps(doc, indent=1, default=str) if a.json else render_text(doc))
    return 0 if not any(s.get("error") for s in doc["shoals"]) else 1


def cmd_graph_feed(a) -> int:
    """Write the graph document atomically every `--interval` seconds (a systemd user unit can run
    this as it is). The builder lives across ticks, so each tick reads only appended ledger bytes."""
    path = a.out or graph_path()
    b = GraphBuilder(a.ids or None, a.ledger_root, a.window_rate, a.events)
    while True:
        t0 = time.time()
        try:
            doc = b.build("", t0)
            write_doc(doc, path)
            if a.verbose:
                print(f"graph-feed: {len(doc['nodes'])} nodes, {len(doc['edges'])} edges, seq {b.seq}, "
                      f"{(time.time() - t0) * 1000:.0f} ms -> {path}", flush=True)
        except Exception as exc:  # noqa: BLE001 - a feed reports and retries, it does not die
            print(f"graph-feed: {exc}", file=sys.stderr, flush=True)
            if a.once:
                return 1
        if a.once:
            return 0
        time.sleep(max(0.0, a.interval - (time.time() - t0)))
