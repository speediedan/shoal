#!/usr/bin/env python3
"""The start-doc contract (docs/start-doc.md): what a session's plan_state must hold for a fresh session to continue from
it alone, and `shoal doc-check`, which says whether a doc meets it.

    shoal doc-check <path> [--since UTC] [--json]
    shoal doc-init <path> --role SLUG --mission TEXT [--shoal ID] [--title T] [--next "a; b"] [--grants "a; b"] [--dry-run]

A start doc is a present-tense live plan. Required:
  - a `Last updated: <UTC timestamp>` line (the handover's proof of refresh);
  - a mission, a current state (or a "resume here" section), the next steps, and a grants-needed section.
Recommended: decisions in force, running monitors, and an append-only companion log that keeps history out of the doc.
Sections are found by keyword in the headings, case-insensitive, so existing docs ("Mission and deliverables", "Current
state (2026-10-22)", "Resume here", "Grants-needed") qualify without a rewrite. Read-only; no model calls.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

REQUIRED = {
    "mission": ("mission",),
    "current state": ("current state", "resume here", "state of play", "where things are"),
    "next steps": ("next", "resume here"),
    "grants-needed": ("grants-needed", "grants needed"),
}
RECOMMENDED = {
    "decisions": ("decision",),
    "running monitors": ("running monitors", "monitors"),
}
STAMP = re.compile(r"^\W*last updated\W*:?\s*\**\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?\s*(?:Z|UTC|[+-][0-9]{2}:?[0-9]{2}))",
                   re.I | re.M)
HEADING = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.M)
FUTURE_SLACK_MIN = 10


def parse_ts(text: str) -> datetime.datetime | None:
    t = text.strip().replace(" UTC", "Z").replace("UTC", "Z").replace(" ", "T", 1)
    t = re.sub(r"T(\d{2}:\d{2})Z$", r"T\1:00Z", t)
    t = t.replace("Z", "+00:00")
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", t)
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


JOB_FOLDER = re.compile(r"(?<![\w-])jobs/([0-9a-f]{8})(?![\w-])")


def job_folders(text: str) -> list[str]:
    """The Claude job ids whose folder (`~/.claude/jobs/<id>/...`) a text names. A job folder goes when its incarnation is
    pruned, so a start doc should not depend on one."""
    return sorted(set(JOB_FOLDER.findall(text)))


def doc_job_folders(path: str) -> list[str]:
    """The job folders a doc at `path` names. Ids are ASCII, so stray bytes do not hide one; an unreadable doc names
    none (`check` reports it)."""
    try:
        with open(os.path.expanduser(path), errors="replace") as fh:
            return job_folders(fh.read())
    except OSError:
        return []


def split_job_folders(path: str) -> tuple[list[str], list[str]]:
    """The job folders a start doc names as (on this host, not on this host). The first is what a next session would
    lose; the second it already cannot use, here."""
    from backends import claude_base
    named = doc_job_folders(path)
    live = [j for j in named if os.path.isdir(os.path.join(claude_base(), "jobs", j))]
    return live, [j for j in named if j not in live]


def last_updated(text: str) -> datetime.datetime | None:
    """The newest `Last updated:` stamp in the doc (a doc may carry one per section; the newest counts)."""
    stamps = [parse_ts(m.group(1)) for m in STAMP.finditer(text)]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def check(path: str, since: datetime.datetime | None = None, now: datetime.datetime | None = None) -> dict:
    """{ok, problems, warnings, last_updated, path}. `since`: the stamp must be at or after it (the refresh proof)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = {"path": path, "ok": False, "problems": [], "warnings": [], "last_updated": None}
    try:
        with open(os.path.expanduser(path), errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        out["problems"].append(f"cannot read the start doc: {exc}")
        return out
    heads = [h.lower() for h in HEADING.findall(text)]
    for name, keys in REQUIRED.items():
        if not any(k in h for h in heads for k in keys):
            out["problems"].append(f"no {name} section (a heading naming one of: {', '.join(keys)})")
    for name, keys in RECOMMENDED.items():
        if not any(k in h for h in heads for k in keys):
            out["warnings"].append(f"no {name} section (recommended)")
    lu = last_updated(text)
    if lu is None:
        out["problems"].append("no `Last updated: <UTC timestamp>` line (for example `Last updated: 2026-10-22T19:05Z`)")
    else:
        out["last_updated"] = lu.strftime("%Y-%m-%dT%H:%M:%SZ")
        if lu > now + datetime.timedelta(minutes=FUTURE_SLACK_MIN):
            out["problems"].append(f"`Last updated` {out['last_updated']} is in the future (use UTC)")
        if since is not None and lu < since:
            out["problems"].append(f"`Last updated` {out['last_updated']} is older than {since.strftime('%Y-%m-%dT%H:%M:%SZ')}: "
                                   f"refresh the doc (current state, decisions, next steps) and its stamp")
    out["ok"] = not out["problems"]
    return out


def cmd_doc_check(a) -> int:
    since = parse_ts(a.since) if a.since else None
    if a.since and since is None:
        print(f"doc-check: --since {a.since!r} is not a timestamp (use UTC, for example 2026-10-22T19:05Z)")
        return 2
    r = check(a.path, since)
    if getattr(a, "sid", "") and getattr(a, "slug", ""):
        try:
            import grants as _g
            from briefs import extract_grants
            from ledger import Ledger, View
            from roles import ShoalSpec, find_spec
            spec = ShoalSpec.load(find_spec(a.sid, getattr(a, "spec", "")))
            if _g.enabled(spec):
                led = Ledger(spec.sid, root=spec.ledger_dir or None, sync=False)
                live = _g.grants_for(View(led, spec), spec, a.slug) if led.exists() else []
                import re as _re
                for g in extract_grants(a.path):
                    m = _re.search(r"\[grant:\s*([^\]]+)\]\s*$", g.strip())
                    if m and not _g.covered(g, live):
                        r["warnings"].append(f"grants-needed marker [grant: {m.group(1).strip()}] names no live "
                                              f"verified grant for {a.slug} (it will still be asked)")
        except Exception as exc:  # noqa: BLE001 - a warning path never fails the check
            r["warnings"].append(f"grant markers not checked: {exc}")
    if a.json:
        print(json.dumps(r))
    else:
        print(f"{'ok' if r['ok'] else 'FAIL'}  {r['path']}  (last updated {r['last_updated'] or 'none'})")
        for p in r["problems"]:
            print(f"  problem: {p}")
        for w in r["warnings"]:
            print(f"  note:    {w}")
    return 0 if r["ok"] else 1


def _items(text: str) -> list[str]:
    """`a; b` (or one per line) as items, blanks and list marks dropped."""
    return [x.strip().lstrip("-*0123456789. ").strip() for x in re.split(r"[;\n]", text or "") if x.strip().lstrip("-*0123456789. ").strip()]


def starter(slug: str, mission: str, sid: str = "", title: str = "", next_steps: str = "", grants: str = "",
            about: str = "", now: datetime.datetime | None = None) -> str:
    """A start doc for a new role from the maintainer's guidance, meeting the contract (`check`): a Last updated stamp,
    the mission as given, a dated current state, the next steps (the guidance's, then keeping this doc current),
    decisions and monitors (none yet), and grants-needed (`- None pending` when nothing was named). The new session
    reads it first and keeps it current; it is a start, not a plan a person must finish."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp, day = now.strftime("%Y-%m-%dT%H:%MZ"), now.strftime("%Y-%m-%d")
    paras = [p.strip() for p in re.split(r"\n\s*\n", (mission or "").strip()) if p.strip()]
    steps = _items(next_steps)
    where = f" of shoal `{sid}`" if sid else ""
    lines = [f"# {slug}: plan state", "",
             f"{title.strip() or slug}: the start doc of the role `{slug}`{where}, written from the maintainer's guidance when the role",
             "was added. Its session reads it first and keeps it current (`shoal doc-check` says whether it meets the contract).", "",
             f"Last updated: {stamp}", "", "## Mission", ""]
    lines += [x for p in paras for x in (p, "")] or ["(no guidance given)", ""]
    lines += [f"## Current state ({day})", "", f"- New role, added {day} with this starter doc. No work done yet: no item taken, nothing decided."]
    if about.strip():
        lines.append(f"- {about.strip()}")
    lines += ["", "## Next steps", "", f"1. Run `shoal next{' --shoal ' + sid if sid else ''} --slug {slug}` and take the first item it gives (or ask for one)."]
    for i, s in enumerate(steps, 2):
        lines.append(f"{i}. {s}")
    lines += [f"{len(steps) + 2}. Keep this doc current at every boundary: Current state, Decisions in force, Next steps and the",
              "   `Last updated` stamp, so a fresh session can continue from it alone.", "",
              "## Decisions in force", "", "- None yet.", "", "## Running monitors", "", "- None.", "", "## grants-needed", ""]
    lines += [f"- {g}" for g in _items(grants)] or ["- None pending"]
    return "\n".join(lines) + "\n"


def cmd_doc_init(a) -> int:
    path = os.path.abspath(os.path.expanduser(a.path))
    if not (a.mission or "").strip():
        print("doc-init: --mission is the guidance the doc is written from")
        return 2
    # a start doc's name, in a folder that is there or one new one under an existing folder: a typo makes no tree
    if not path.endswith(".md"):
        print(f"doc-init: {path}: a start doc is a .md file (plan_state.md)")
        return 2
    if not os.path.isdir(os.path.dirname(os.path.dirname(path))):
        print(f"doc-init: {os.path.dirname(path)}: neither it nor its parent folder exists: refused (check the path)")
        return 2
    if os.path.exists(path) and not a.force:
        print(f"doc-init: {path} exists: refused (it may be a role's live plan); use that doc, or another path")
        return 1
    text = starter(a.role, a.mission, a.sid, a.title, a.next, a.grants, a.about)
    if a.dry_run:
        print(f"[dry-run] would write {path} ({len(text.splitlines())} lines):\n")
        print(text, end="")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        fh.write(text)
    os.replace(path + ".tmp", path)
    r = check(path)
    print(f"wrote {path}: {'meets the start-doc contract' if r['ok'] else 'PROBLEMS: ' + '; '.join(r['problems'])}")
    print("NOTE: it is not committed and only this host has it: commit it where its folder is tracked, or it is easy to lose")
    return 0 if r["ok"] else 1


def add_parser(sub) -> None:
    p = sub.add_parser("doc-check", help="does a start doc (plan_state) meet the start-doc contract? (read-only)")
    p.add_argument("path")
    p.add_argument("--since", default="", help="the Last updated stamp must be at or after this UTC time")
    p.add_argument("--shoal", dest="sid", default="", help="with --slug: warn on [grant: ...] markers no live grant covers")
    p.add_argument("--spec", default="")
    p.add_argument("--slug", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doc_check)
    p = sub.add_parser("doc-init", help="write a new role's starter start doc (plan_state.md) from guidance; never overwrites")
    p.add_argument("path")
    p.add_argument("--role", required=True, help="the role's slug")
    p.add_argument("--mission", required=True, help="the guidance: what the role is for and delivers (paragraphs by blank lines)")
    p.add_argument("--shoal", dest="sid", default="", help="the shoal it joins")
    p.add_argument("--title", default="", help="the role's title")
    p.add_argument("--next", default="", help="first steps, separated by ;")
    p.add_argument("--grants", default="", help="authorizations it needs (scope and giver), separated by ;")
    p.add_argument("--about", default="", help="one line for the current state (provider, host)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--dry-run", action="store_true", help="print the doc, write nothing")
    p.set_defaults(func=cmd_doc_init)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    add_parser(ap.add_subparsers(dest="cmd", required=True))
    a = ap.parse_args()
    sys.exit(a.func(a))
