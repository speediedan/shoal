#!/usr/bin/env python3
"""The deep pass over a role directory (docs/lifecycle.md, "Deep pass"): what the model's periodic work needs, and
whether that work is due. Advisory and read-only; it moves nothing, runs no hook and makes no model calls.

    shoal lifecycle deep-pass <start-doc> [--json]

The pass is due every `[deep_pass] every_days` days or `every_restarts` restarts, whichever comes first. The last pass
is the last `Deep pass <UTC>:` line in the start doc's companion log (append-only, so the last marker is the last pass;
one inside a fenced block, or with a time that does not parse, is skipped), and restarts since it are the log's
`After ...` snapshot headings below that line. The report lists, for the role to
act on:

- **keep reasons** to review: each file whose front matter has `keep:`, with the days since it last changed;
- **consolidation candidates**: live auxiliary files (not a root, not history), oldest change first (one not yet
  committed last), to fold into the start doc, design doc or log when concluded;
- **cold candidates**: archived entries of the directory's manifest warm for at least `[cold] warm_days` with no live
  reference now, each with its `shoal archive --cold` command (which checks again before it moves anything);
- **the trend**: a marker line for the role to append to its log snapshot, and the previous marker's numbers.
"""
from __future__ import annotations

import datetime
import json
import os
import posixpath
import re
import shlex
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MARKER = re.compile(r"^\W*Deep pass (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)\b:?\s*(.*)$")
LIST_MAX = 10


def _ts(text: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ" if text.count(":") == 2 else "%Y-%m-%dT%H:%MZ"
    return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc).timestamp()


def due(log_text: str, every_days: float, every_restarts: int, now: float | None = None) -> dict:
    """When the last pass ran (its marker), days and restarts since, and whether the next one is due."""
    import logentry
    import startdoc
    now = time.time() if now is None else now
    lines = log_text.splitlines()
    # a marker in a fenced block (a log quoting the docs' example) is content, as a fenced heading is
    prose = startdoc.prose(lines)
    marks = [(i, m) for i, m in ((i, MARKER.match(x)) for i, x in enumerate(lines)) if m and prose[i]]
    out = {"every_days": every_days, "every_restarts": every_restarts, "last": None, "days_since": None,
           "restarts_since": None, "previous_trend": "", "due": True, "why": "no deep pass recorded in the log"}
    when = None
    for at, m in reversed(marks):  # the last marker that parses: a malformed one (25:61Z) is skipped, never raised
        try:
            when = _ts(m.group(1))
            break
        except ValueError:
            continue
    if when is None:
        return out
    days = round((now - when) / 86400, 1)
    restarts = sum(1 for e in logentry.entries(log_text) if e["kind"] == "snapshot" and e["line"] > at + 1)
    why = [f"{days} days since (every {every_days})"] if days >= every_days else []
    why += [f"{restarts} restarts since (every {every_restarts})"] if restarts >= every_restarts else []
    out.update(last=m.group(1), days_since=days, restarts_since=restarts, previous_trend=m.group(2).strip(),
               due=bool(why), why="; ".join(why) or f"{days} days and {restarts} restarts since the last")
    return out


def _last_change(repo, d: str) -> dict[str, float]:
    """When each path under `d` last changed (commit time), in one `git log`."""
    import refs
    out: dict[str, float] = {}
    ts = None
    for line in refs._git(repo.root, "log", "--no-renames", "--format=@%ct", "--name-only", "--", d or ".").splitlines():
        if line.startswith("@"):
            ts = float(line[1:])
        elif line.strip() and ts is not None:
            out.setdefault(line.strip(), ts)  # newest first: the first sighting is the last change
    return out


def probe(path: str, now: float | None = None) -> dict:
    import archive
    import lifecycle
    import refs

    now = time.time() if now is None else now
    path = os.path.abspath(os.path.expanduser(path))
    lc = lifecycle.load(path)
    v = lc["values"]
    known = refs.roots(False)
    scan = refs.scan(os.path.dirname(path), docs=[path], known_roots=known["roots"], now=now)
    repo = refs.Repo(os.path.dirname(path))
    d, doc = scan["dir"], repo.rel(path)
    log = refs.companion_log(doc, repo.mode) if doc else None
    out = {"v": 1, "advisory": True, "doc": doc, "dir": d, "log": log, "notes": list(lc["notes"]) + known["notes"]}
    if not log:
        out["notes"].append("no companion log beside the start doc: the last pass cannot be read, so it reads as due")
    out["due"] = due(repo.read(log) if log else "", v["deep_pass"]["every_days"], v["deep_pass"]["every_restarts"], now)

    changed = _last_change(repo, d)
    age = {p: round((now - changed[p]) / 86400, 1) if p in changed else None for p in repo.mode}
    size = {p: os.path.getsize(os.path.join(repo.root, p)) if os.path.isfile(os.path.join(repo.root, p)) else 0
            for p in repo.mode if p.startswith(d + "/" if d else "")}
    history = v["tiers"]["history"]
    out["keep"] = [{"path": f["path"], "keep": f["keep"], "days_since_change": age.get(f["path"])}
                   for f in scan["files"] if f.get("keep")]
    aux = [f["path"] for f in scan["files"] if f["class"] == "live" and f["path"] != log
           and not refs._matches(f["path"], history)]
    # oldest change first; a file with no commit yet (age None) sorts last, as the newest
    out["consolidate"] = sorted(({"path": p, "days_since_change": age.get(p), "bytes": size.get(p, 0)} for p in aux),
                                key=lambda x: -(x["days_since_change"] or 0))

    # cold candidates: every manifest under the directory's archive mirror (a subdirectory's files keep their own
    # manifest), latest event per original path
    plan = archive.Plan(path)
    mpath = plan.manifest_path(posixpath.join(d, "x") if d else "x")
    top, name = posixpath.dirname(mpath), posixpath.basename(mpath)
    manifests = sorted(p for p in repo.mode if posixpath.basename(p) == name and (p == mpath or p.startswith(top + "/")))
    latest: dict[str, dict] = {}
    for m in manifests:
        for e in archive.read_manifest(os.path.join(repo.root, m)):
            if e.get("path"):
                latest[e["path"]] = e
    warm = {p: e for p, e in latest.items() if e.get("event") in ("archive", "migrate")
            and e.get("archive_path") in repo.mode}
    named = refs.edges_to(repo, [e["archive_path"] for e in warm.values()],
                          [p for p in repo.mode if posixpath.splitext(p)[1] in refs.TEXT_EXT
                           and not refs._matches(p, v["tiers"]["exclude"]) and not refs._matches(p, history)],
                          lambda p: "live") if warm else {}
    cold = []
    for p, e in sorted(warm.items()):
        days = round((now - _ts(e["ts"])) / 86400, 1) if e.get("ts") else None
        if days is not None and days >= v["cold"]["warm_days"] and not named.get(e["archive_path"]):
            cold.append({"path": p, "archive_path": e["archive_path"], "warm_days": days})
    out["cold"] = cold
    if cold:
        out["cold_command"] = "shoal archive --cold " + " ".join(shlex.quote(c["path"]) for c in cold)

    stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    kb = round(sum(size.values()) / 1024)
    out["trend"] = {"live_auxiliary": len(aux), "kb": kb, "kept": len(out["keep"]), "warm": len(warm),
                    "cold_candidates": len(cold)}
    out["marker"] = (f"Deep pass {stamp}: {len(aux)} live auxiliary, {kb} KB, {len(out['keep'])} kept, {len(warm)} warm, "
                     f"{len(cold)} cold candidate{'' if len(cold) == 1 else 's'}")
    out["notes"].append("cold candidates are checked against live references now only; archive --cold checks again")
    return out


def report(out: dict) -> None:
    dd = out["due"]
    print(f"deep pass for {out['doc']}: {'DUE' if dd['due'] else 'not due'} ({dd['why']})")
    if dd["last"]:
        print(f"  last pass {dd['last']}" + (f": {dd['previous_trend']}" if dd["previous_trend"] else ""))
    for title, key, fmt in (
            ("keep reasons to review", "keep", lambda x: f"{x['path']}: keep: {x['keep']} ({x['days_since_change']} d)"),
            ("consolidation candidates, oldest change first", "consolidate",
             lambda x: f"{x['path']} ({x['days_since_change']} d, {x['bytes']} B)"),
            ("cold candidates", "cold", lambda x: f"{x['path']} (warm {x['warm_days']} d at {x['archive_path']})")):
        items = out[key]
        print(f"  {title}: {len(items)}")
        for x in items[:LIST_MAX]:
            print(f"    {fmt(x)}")
        if len(items) > LIST_MAX:
            print(f"    ({len(items) - LIST_MAX} more; --json lists all)")
    if out.get("cold_command"):
        print(f"  go cold: {out['cold_command']}")
    print(f"  record in the log snapshot: {out['marker']}")
    for n in out["notes"][:LIST_MAX]:
        print(f"  note: {n}")


def cmd_deep_pass(a) -> int:
    try:
        out = probe(a.path)
    except (RuntimeError, OSError) as exc:
        print(json.dumps({"v": 1, "ok": False, "error": str(exc)}) if a.json else f"deep-pass: {exc}")
        return 2
    print(json.dumps(out, indent=1)) if a.json else report(out)
    return 0


def add_parser(s) -> None:
    q = s.add_parser("deep-pass", help="read-only deep-pass advice for a start doc's directory: due?, keep reasons, "
                                       "consolidation and cold candidates, the trend (never moves anything)")
    q.add_argument("path")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_deep_pass)
