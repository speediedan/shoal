"""Role time accounting: classify each logical role's time and integrate it with conservation.

Pure functions plus a small checkpointed accumulator. No ledger fetch, provider call, harness probe,
wake or model turn happens here: adapters build observation snapshots from host-local caches and
the ledger clone, and this module only classifies and integrates them (architecture doc §9).

Every second of a configured foreground role's membership is charged to exactly one class and
event: work, a non-idle wait, idle, unknown or excluded. Average Active Roles (AAR) is work plus
non-idle waits per elapsed second; Role Work Fraction (RWF) is work over eligible (non-excluded)
role-time. Unknown time is never redistributed: reports show lower and upper bounds beside coverage.

A snapshot is what one role looked like at observation time `t` (epoch seconds):

    {"t": 1000.0, "seq": 7,
     "lifecycle": {"parked": False, "handover": False, "conflict": ""},
     "native": {"state": "turn|between|prompt|absent|<raw>", "observed_at": 998.0, "budget_s": 15,
                "wait_event": ""},
     "ledger": {"observed_at": 990.0, "budget_s": 180, "state": "waiting", "sub": "remote-ci",
                "target": "", "refill_requested": False, "active": "", "next": ["repo#1"],
                "blockers": {"repo#1": {"blocker": "upstream:<owner/repo>#2", "status": "live",
                                        "expires_at": 4600.0, "evidence": "age-only"}}}}

`native.state` is the harness-normalized turn state: `turn` (a turn is running: work), `between`
(no turn), `prompt` (an open native question), `absent` (no live worker for the session).
`native.wait_event` names a positively identified sole blocking wait inside a turn. `observed_at`
is when each source was last read successfully (for the ledger: the clone's last fetch, never the
newest event), and `budget_s` how long that read may be believed. A snapshot holds until the next
one for the same role or until a source budget runs out, whichever is first; a gap is unknown.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blockers  # noqa: E402

POLICY_VERSION = "role-time/v5.1"
# v5.1 (2026-11-01): a role with no session recorded in the spec whose declared posture is paused or
# offline is excluded, not unknown; and a CI wait on a placed job that has not been granted its
# lease yet is wait:lease, not wait:local-ci. Snapshots from v5 segments keep v5 behaviour.
V5_POLICY = "role-time/v5"
# Snapshots from a journal segment stamped with this policy, or from a journal written before
# segment headers existed, are classified with its semantics, so an old report reads the same.
LEGACY_POLICY = "role-time/v4.1"

WORK = ("turn",)
# v5 adds three waits that v4.1 charged to idle: `self` (between turns with runnable ready work),
# `assignment` (an empty queue on a fill=assigned role: nobody assigned it work) and `sources` (a
# fill=self role whose cached priority table is empty). The last two are human waits.
WAITS = ("local-ci", "remote-ci", "research-proto", "lease", "upstream", "session-blocked", "user-auth", "user-design",
         "user-ask", "user-prompt", "hold", "handover", "refill", "mixed", "unverified",
         "self", "assignment", "sources")
IDLE = ("ready-work", "no-work")
EXCLUDED = ("parked", "paused", "offline")
UNKNOWN = ("missing-source", "stale-source", "session-missing", "conflicting-session", "park-conflict",
           "unmapped-state", "no-observation", "gap")
VOCABULARY = {"work": WORK, "wait": WAITS, "idle": IDLE, "excluded": EXCLUDED, "unknown": UNKNOWN}
# display grouping of non-idle waits (no effect on accounting)
WAIT_GROUP = {"local-ci": "system", "remote-ci": "system", "lease": "system", "research-proto": "research",
              "upstream": "upstream",
              "session-blocked": "peer", "user-auth": "human", "user-design": "human", "user-ask": "human",
              "user-prompt": "human", "assignment": "human", "sources": "human", "self": "self",
              "hold": "policy", "handover": "coordination", "refill": "coordination",
              "mixed": "mixed", "unverified": "unverified"}
POSTURE_WAITS = ("local-ci", "remote-ci", "research-proto", "user-auth", "user-design", "session-blocked", "upstream")
BLOCKER_EVENT = {"role": "session-blocked", "upstream": "upstream", "lease": "lease", "ask": "user-ask",
                 "hold": "hold"}
INF = math.inf


def pairs():
    """Every (class, event) in the vocabulary, for zero-initialized series."""
    return [(c, e) for c, evs in VOCABULARY.items() for e in evs]


def _fresh(source, now):
    """(ok, reason, deadline) for one source read."""
    if not source or source.get("observed_at") is None:
        return False, "missing-source", INF
    deadline = float(source["observed_at"]) + float(source.get("budget_s", 0))
    return now < deadline, "stale-source", deadline


def _ready(v5):
    """Between turns with runnable work: v5 charges it as the role's own wait (`wait:self`)."""
    return ("wait", "self") if v5 else ("idle", "ready-work")


def _empty(ledger, now, v5):
    """Between turns with nothing to do. v5 names who owes the role work: a fill=assigned role
    waits on assignment; a fill=self role whose cached priority table is fresh and empty waits on
    its sources. Anything else (fill none, or no fresh table) stays idle:no-work, never unknown.
    Returns (class, event, until)."""
    if not v5:
        return "idle", "no-work", INF
    fill = ledger.get("fill") or ""
    if fill == "assigned":
        return "wait", "assignment", INF
    if fill == "self":
        src = ledger.get("sources")
        ok, _why, dead = _fresh(src, now)
        if ok and src.get("candidates") == 0:
            return "wait", "sources", dead
        return "idle", "no-work", dead if ok else INF
    return "idle", "no-work", INF


def _queue(ledger, now, quality, v5=True):
    """Between turns with an idle/active posture: classify by the queue and its blockers.
    Returns (class, event, until)."""
    if ledger.get("active"):
        return (*_ready(v5), INF)
    nxt = list(ledger.get("next") or [])
    if not nxt:
        return ("wait", "refill", INF) if ledger.get("refill_requested") else _empty(ledger, now, v5)
    bl = ledger.get("blockers") or {}
    runnable, kinds, unproven, until = False, set(), False, INF
    for item in nxt:
        b = bl.get(item)
        if not b:
            runnable = True
            continue
        status, exp = b.get("status", "unknown"), b.get("expires_at")
        if status == "live" and exp is not None and now >= float(exp):
            status = "expired"
        if status in ("resolved", "expired"):
            runnable = True
            quality.add("stale-blocked")
            continue
        if b.get("evidence") == "age-only":
            quality.add("age-only-blockers")
        if status == "live":
            kinds.add(BLOCKER_EVENT[blockers.kind(b.get("blocker", ""))])
            if exp is not None:
                until = min(until, float(exp))
        else:
            unproven = True
    if runnable:
        return (*_ready(v5), until)
    if unproven:
        return "wait", "unverified", until
    return "wait", (kinds.pop() if len(kinds) == 1 else "mixed"), until


def classify(snap, now):
    """(class, event, quality, until) for a snapshot at time `now`. `until` is the earliest later
    time at which the result can change without a new observation (a source budget or a blocker
    expiry); integrators split intervals there. A snapshot read from a journal segment stamped with
    an older policy carries it as `policy` and keeps that policy's semantics; a live snapshot has
    none and gets the current one."""
    q = set()
    pol = snap.get("policy") or POLICY_VERSION
    v5 = pol != LEGACY_POLICY
    v51 = pol not in (LEGACY_POLICY, V5_POLICY)
    life = snap.get("lifecycle") or {}
    native, ledger = snap.get("native"), snap.get("ledger")
    n_ok, n_why, n_dead = _fresh(native, now)
    l_ok, l_why, l_dead = _fresh(ledger, now)
    nstate = (native or {}).get("state") if n_ok else None

    def out(c, e, until=INF):
        return c, e, tuple(sorted(q)), until

    if life.get("conflict"):
        return out("unknown", "conflicting-session")
    if life.get("parked"):
        if nstate in ("turn", "prompt"):
            return out("unknown", "park-conflict", n_dead)
        return out("excluded", "parked", n_dead if n_ok else INF)
    if not n_ok:
        return out("unknown", n_why)
    if nstate not in ("turn", "between", "prompt", "absent"):
        return out("unknown", "unmapped-state", n_dead)
    if nstate == "turn":
        ev = native.get("wait_event") or ""
        if ev:
            return out("wait", ev, n_dead) if ev in WAITS else out("unknown", "unmapped-state", n_dead)
        return out("work", "turn", n_dead)
    if nstate == "prompt":
        return out("wait", "user-prompt", n_dead)
    if life.get("handover"):
        return out("wait", "handover", n_dead)
    if nstate == "absent":
        # v5.1: no session recorded at all (not a missing process) and a declared posture that
        # says the role is out: excluded. A recorded session with no live worker stays unknown.
        if (v51 and life.get("session_recorded") is False and l_ok
                and (ledger or {}).get("state") in ("paused", "offline")):
            return out("excluded", ledger["state"], min(n_dead, l_dead))
        return out("unknown", "session-missing", n_dead)
    # between turns: the ledger classifies
    if not l_ok:
        return out("unknown", l_why, n_dead)
    until = min(n_dead, l_dead)
    state, sub = ledger.get("state", ""), ledger.get("sub", "") or ""
    if state in ("paused", "offline"):
        return out("excluded", state, until)
    if state == "waiting":
        if sub == "research-proto":
            return out("wait", sub, until)  # research waits are research whatever they wait on, a lease included
        if str(ledger.get("target") or "").startswith("lease:"):
            return out("wait", "lease", until)
        job = ledger.get("job") or {}
        if v51 and sub == "local-ci" and job.get("placed") and job.get("granted") is False and not job.get("done"):
            return out("wait", "lease", until)  # queued for its lease, not yet running
        if sub in POSTURE_WAITS:
            return out("wait", sub, until)
        if sub == "queue-empty":
            if ledger.get("refill_requested"):
                return out("wait", "refill", until)
            c, e, u = _empty(ledger, now, v5)
            return out(c, e, min(until, u))
        return out("unknown", "unmapped-state", until)
    if state == "active":
        # an `active` sleep posture with no turn running: a turn ended without its closing write
        q.add("stale-active")
        if ledger.get("active") or ledger.get("next"):
            return out(*_ready(v5), until)
        c, e, u = _empty(ledger, now, v5)
        return out(c, e, min(until, u))
    if state == "idle":
        c, e, u = _queue(ledger, now, q, v5)
        return out(c, e, min(until, u))
    return out("unknown", "unmapped-state", until)


def _ordered(snaps):
    """Observation order: time, then append order (`seq`) for same-time records of one writer."""
    return sorted(snaps, key=lambda s: (float(s["t"]), int(s.get("seq", 0))))


def _charge(snap, a, b, acc, segs, role):
    """Charge [a, b) for one held snapshot, splitting at every time-driven change."""
    while a < b:
        if snap is None:
            c, e, qual, until = "unknown", "no-observation", (), INF
        else:
            c, e, qual, until = classify(snap, a)
        end = min(b, until) if until > a else b
        acc[(c, e)] += end - a
        if segs is not None:
            if segs and segs[-1][0] == role and segs[-1][2] == a and segs[-1][3:5] == (c, e) and segs[-1][5] == qual:
                segs[-1] = segs[-1][:2] + (end,) + segs[-1][3:]
            else:
                segs.append((role, a, end, c, e, qual))
        a = end


def account(observations, membership, t0, t1, timeline=False):
    """Reconciled accounting over [t0, t1). `observations`: {role: [snapshot]}; `membership`:
    {role: [(start, end)]} for configured foreground roles (end may be None for open). Returns
    ({role: Counter{(class, event): seconds}}, segments or None). Conserves membership seconds."""
    out, segs = {}, ([] if timeline else None)
    for role, spans in membership.items():
        acc = Counter()
        snaps = _ordered(observations.get(role, []))
        for s0, s1 in spans:
            a, b = max(t0, float(s0)), min(t1, INF if s1 is None else float(s1))
            if b <= a:
                continue
            cur, cur_t = None, a
            for s in snaps:
                st = float(s["t"])
                if st <= a:
                    cur = s
                    continue
                if st >= b:
                    break
                _charge(cur, cur_t, st, acc, segs, role)
                cur, cur_t = s, st
            _charge(cur, cur_t, b, acc, segs, role)
        out[role] = acc
    return out, segs


def summarize(per_role, delta):
    """AAR, its work and per-wait contributions, RWF, coverage and the unknown bounds."""
    tot = Counter()
    for acc in per_role.values():
        tot.update(acc)
    T = {c: sum(v for (k, _), v in tot.items() if k == c) for c in VOCABULARY}
    Tp = sum(T.values())
    Te = Tp - T["excluded"]
    d = float(delta)
    res = {"delta_s": d, "T": T, "T_p": Tp, "T_e": Te,
           "eligible_population": Te / d if d else None,
           "AAR": (T["work"] + T["wait"]) / d if d else None,
           "AAR_upper": (T["work"] + T["wait"] + T["unknown"]) / d if d else None,
           "A_work": T["work"] / d if d else None,
           "A_wait": {e: tot[("wait", e)] / d for e in WAITS if tot[("wait", e)]} if d else {},
           "RWF": T["work"] / Te if Te else None,
           "RWF_upper": (T["work"] + T["unknown"]) / Te if Te else None,
           "coverage": (Te - T["unknown"]) / Te if Te else None,
           "events": {f"{c}:{e}": v for (c, e), v in sorted(tot.items()) if v}}
    return res


class Accumulator:
    """Live observation-time accounting with monotonic counters and an atomic checkpoint.

    An observation is charged from when it is applied, never backdated: a record that arrives
    after time already accounted is applied from the accounted time and counted as late. A restart
    from a checkpoint charges the unobserved interval to unknown:gap; a missing or unreadable
    checkpoint starts a new epoch, which the exported epoch gauge makes visible."""

    def __init__(self, shoal, roles, now=None, epoch=None):
        self.shoal = shoal
        self.epoch = epoch if epoch is not None else int(time.time() if now is None else now)
        self.counters = {r: Counter({p: 0.0 for p in pairs()}) for r in roles}
        self.until = {r: float(now if now is not None else time.time()) for r in roles}
        self.current = {r: None for r in roles}
        self.member = {r: True for r in roles}
        self.late = Counter()
        self.state = {}

    def set_member(self, role, member, at):
        self.ensure(role, at)
        self.advance(role, at)
        self.member[role] = bool(member)

    def ensure(self, role, at):
        if role not in self.counters:
            self.counters[role] = Counter({p: 0.0 for p in pairs()})
            self.until[role], self.current[role], self.member[role] = float(at), None, True

    def advance(self, role, now):
        a = self.until[role]
        if now <= a:
            return
        if self.member[role]:
            acc = Counter()
            _charge(self.current[role], a, float(now), acc, None, role)
            self.counters[role].update(acc)
        self.until[role] = float(now)
        cur = self.current[role]
        self.state[role] = classify(cur, now)[:2] if cur else ("unknown", "no-observation")

    def observe(self, role, snap):
        t = float(snap["t"])
        self.ensure(role, t)
        if t < self.until[role]:
            self.late[role] += 1
            snap = dict(snap, t=self.until[role])
        else:
            self.advance(role, t)
        self.current[role] = snap
        self.state[role] = classify(snap, float(snap["t"]))[:2]

    def advance_all(self, now):
        for r in self.counters:
            self.advance(r, now)

    def to_json(self):
        return {"v": 1, "policy": POLICY_VERSION, "shoal": self.shoal, "epoch": self.epoch,
                "counters": {r: [[c, e, v] for (c, e), v in cs.items()] for r, cs in self.counters.items()},
                "until": self.until, "current": self.current, "member": self.member, "late": dict(self.late)}

    def save(self, path):
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(self.to_json(), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, shoal, roles, now):
        """Resume from a checkpoint, charging [checkpoint, now) to unknown:gap. A missing or
        corrupt checkpoint starts a new epoch at `now`."""
        try:
            with open(path) as fh:
                d = json.load(fh)
            if d.get("v") != 1 or d.get("shoal") != shoal:
                raise ValueError("checkpoint does not match")
        except (OSError, ValueError):
            return cls(shoal, roles, now=now)
        self = cls(shoal, [], now=now, epoch=d["epoch"])
        for r, rows in d["counters"].items():
            self.counters[r] = Counter({(c, e): float(v) for c, e, v in rows})
            for p in pairs():
                self.counters[r].setdefault(p, 0.0)
            self.until[r] = float(d["until"][r])
            self.current[r] = None  # an old snapshot is not evidence for the unobserved interval
            self.member[r] = bool(d["member"].get(r, True))
        self.late = Counter(d.get("late") or {})
        for r in roles:
            self.ensure(r, now)
        for r in self.counters:
            if self.member[r] and now > self.until[r]:
                self.counters[r][("unknown", "gap")] += now - self.until[r]
            self.until[r] = max(self.until[r], float(now))
        return self

    def render_prometheus(self, expected=None):
        """Text exposition of the stable counters and current state. No host or session labels."""
        sh = _esc(self.shoal)
        L = ["# HELP shoal_role_time_seconds_total Observed role-seconds by class and event (live observation time).",
             "# TYPE shoal_role_time_seconds_total counter"]
        for r in sorted(self.counters):
            for (c, e), v in sorted(self.counters[r].items()):
                L.append(f'shoal_role_time_seconds_total{{shoal="{sh}",role="{_esc(r)}",class="{c}",event="{e}"}} {v:.3f}')
        L += ["# HELP shoal_role_observed_state Current classification, one-hot per role.",
              "# TYPE shoal_role_observed_state gauge"]
        for r in sorted(self.counters):
            cur = self.state.get(r, ("unknown", "no-observation"))
            for c, e in pairs():
                L.append(f'shoal_role_observed_state{{shoal="{sh}",role="{_esc(r)}",class="{c}",event="{e}"}} '
                         f'{1 if (c, e) == cur and self.member[r] else 0}')
        L += ["# HELP shoal_role_expected Role is in the configured foreground population.",
              "# TYPE shoal_role_expected gauge"]
        for r in sorted(self.counters):
            want = self.member[r] if expected is None else (r in expected)
            L.append(f'shoal_role_expected{{shoal="{sh}",role="{_esc(r)}"}} {1 if want else 0}')
        L += ["# HELP shoal_role_late_observations_total Observations applied after their time was accounted.",
              "# TYPE shoal_role_late_observations_total counter"]
        for r in sorted(self.counters):
            L.append(f'shoal_role_late_observations_total{{shoal="{sh}",role="{_esc(r)}"}} {self.late.get(r, 0)}')
        L += ["# HELP shoal_role_accounting_epoch Start of the current accounting epoch (changes on state loss).",
              "# TYPE shoal_role_accounting_epoch gauge",
              f'shoal_role_accounting_epoch{{shoal="{sh}",policy="{POLICY_VERSION}"}} {self.epoch}']
        return "\n".join(L) + "\n"


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# -- reconciled report over an observation file

def _epoch(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except ValueError:
        pass
    t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"timestamp {v!r} needs a UTC offset")
    return t.timestamp()


def observation_files(path):
    """The journal files behind one `--input`, oldest first. A directory, or a live journal
    `<stem>.jsonl`, means every closed daily segment `<stem>-YYYYMMDD.jsonl[.gz]` beside it and
    then the live file; any other path (a single segment, compressed or not) is read alone."""
    import glob
    if os.path.isdir(path):
        live = sorted(glob.glob(os.path.join(path, "*.jsonl")))
        live = [p for p in live if not _segment_day(p)]
        stems = [p[:-len(".jsonl")] for p in live] or [os.path.join(path, "role-time-observations")]
    elif path.endswith(".jsonl") and not _segment_day(path):
        live, stems = [path], [path[:-len(".jsonl")]]
    else:
        return [path]
    segs = []
    for stem in stems:
        segs += [p for p in glob.glob(glob.escape(stem) + "-*.jsonl*") if _segment_day(p)]
    # a day closed but not yet compressed and its compressed form never coexist for long; if both
    # do (a crash between the two writes), the compressed copy is complete, so it wins
    by_day = {}
    for p in sorted(segs, key=lambda p: (_segment_day(p), p.endswith(".gz"))):
        by_day[(os.path.dirname(p), _segment_day(p))] = p
    return [by_day[k] for k in sorted(by_day, key=lambda k: k[1])] + [p for p in live if os.path.exists(p)]


def _segment_day(path):
    name = os.path.basename(path)
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            day = name[:-len(suffix)].rsplit("-", 1)[-1]
            return day if len(day) == 8 and day.isdigit() else None
    return None


def load_observations(path, policies=None):
    """JSONL with `{"type": "segment", "policy", "opened"}` (one per daily segment, newer journals
    only), `{"type": "membership", "role", "spans": [[start, end|null]]}` and
    `{"type": "snapshot", "role", "t", ...}` lines, across every file `observation_files` finds.
    Line order is append order (`seq`). Membership is rewritten in full at the head of each
    segment, so the last record for a role wins. A `policies` list collects segment policies."""
    import gzip
    obs, mem = {}, {}
    for f in observation_files(path):
        opener = gzip.open if f.endswith(".gz") else open
        policy = LEGACY_POLICY  # until a segment header says otherwise: older journals carry none
        with opener(f, "rt") as fh:
            for n, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue  # a torn final append is not an observation
                if d.get("type") == "segment":
                    policy = d.get("policy") or LEGACY_POLICY
                    if policies is not None:
                        policies.append(d.get("policy"))
                elif d.get("type") == "membership":
                    mem[d["role"]] = [(_epoch(s), None if e is None else _epoch(e)) for s, e in d["spans"]]
                elif d.get("type") == "snapshot":
                    d = dict(d, t=_epoch(d["t"]), seq=d.get("seq", n), policy=d.get("policy") or policy)
                    obs.setdefault(d["role"], []).append(d)
    return obs, mem


def _pct(v):
    return "-" if v is None else f"{100 * v:.1f}%"


def cmd_report(a):
    policies = []
    obs, mem = load_observations(a.input, policies)
    t0, t1 = _epoch(a.since), _epoch(a.until)
    if t1 <= t0:
        print("role-time: --until must be after --since", file=sys.stderr)
        return 2
    per_role, segs = account(obs, mem, t0, t1, timeline=a.timeline)
    # `policy` classified this report; `segment_policies` wrote the journal segments it read
    # (older journals carry no segment header and contribute none)
    doc = {"policy": POLICY_VERSION, "segment_policies": sorted({p for p in policies if p}),
           "window": [t0, t1], "aggregate": summarize(per_role, t1 - t0),
           "roles": {r: summarize({r: acc}, t1 - t0) for r, acc in per_role.items()}}
    if a.timeline:
        doc["timeline"] = [list(s[:5]) + [list(s[5])] for s in segs]
    if a.json:
        print(json.dumps(doc, indent=1, default=str))
        return 0
    g = doc["aggregate"]
    print(f"role time {POLICY_VERSION}: {t1 - t0:.0f} s, eligible population {g['eligible_population'] or 0:.2f}, "
          f"coverage {_pct(g['coverage'])}")
    print(f"AAR {g['AAR'] or 0:.2f} (upper {g['AAR_upper'] or 0:.2f}), work {g['A_work'] or 0:.2f}, "
          f"RWF {_pct(g['RWF'])} (upper {_pct(g['RWF_upper'])})")
    for r, s in sorted(doc["roles"].items()):
        top = ", ".join(f"{k} {v:.0f}s" for k, v in sorted(s["events"].items(), key=lambda kv: -kv[1])[:4])
        print(f"  {r}: RWF {_pct(s['RWF'])}, coverage {_pct(s['coverage'])}; {top}")
    return 0


def add_parser(sub):
    p = sub.add_parser("role-time", help="role time accounting (AAR, Role Work Fraction) from observation records")
    s = p.add_subparsers(dest="role_time_cmd", required=True)
    r = s.add_parser("report", help="reconciled event-time report over a window of an observation file")
    r.add_argument("--input", required=True,
                   help="observation journal: the live JSONL (its closed daily segments beside it, "
                        "compressed or not, are read with it), its directory, or one segment")
    r.add_argument("--since", required=True, help="window start (ISO-8601 or epoch seconds)")
    r.add_argument("--until", required=True, help="window end (ISO-8601 or epoch seconds)")
    r.add_argument("--timeline", action="store_true", help="include merged per-role class/event segments")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_report)
    import role_time_adapters  # the impure edge registers its verbs here (deferred: it imports us)
    role_time_adapters.add_subcommands(s)
