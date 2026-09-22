#!/usr/bin/env python3
"""Shared board digest, batch triage, and working context (architecture doc §4).

One board read per HAG, not one per self-filling watcher: `hag.py digest --refresh`
(a systemd timer when the spec opts in with `features.digest`) fetches the priority
table ONCE, with body excerpts, blocking relations and linked PRs, scores every open
item against every self-filling role's working context, and writes
`$HAG_DATA_ROOT/<hag>/board_digest.json`. A role's ledger-watch then reads that file
instead of GitHub, and a board wake carries enough (excerpt, score, why tokens) to
claim or pass without opening each issue.

Scoring (soft, never a gate except `never`):
  score = sum of affinity weights matched (repo, labels, keywords in title+body,
          path prefixes in body) + routing bonus (+2 when another role's decline
          suggested this role) - 1 when an open blocker exists
  rank  = score - board_pos / N          (board order breaks ties among equal fits)
An item matching a role's `never` list is excluded for that role. An item every
self-filling role has passed on is listed under `unowned`.

`hag.py triage` records a role's decisions in one go: each claim keeps the
first-landed-wins check of `claim`; all passes land as ONE ledger commit with a
reason class from a fixed vocabulary, so routing and reports can count them.
"""
from __future__ import annotations

import datetime
import fnmatch
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))

from roles import HagSpec, data_dir, find_spec, scope_to_context  # noqa: E402

PASS_CLASSES = ("out-of-lane", "blocked", "needs-maintainer", "needs-hardware", "duplicate-or-stale")
EXCERPT_CHARS = 600
DIGEST_MAX_AGE_MIN = 30.0
ROUTING_BONUS = 2.0
BLOCKED_PENALTY = 1.0
DEFAULT_MIN_SCORE = 0.5

QUERY = """
query($login:String!, $number:Int!, $after:String) {
  %s(login:$login) {
    projectV2(number:$number) {
      title
      items(first:50, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          status: fieldValueByName(name:"Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            __typename
            ... on Issue { number title state url bodyText updatedAt
              repository { nameWithOwner }
              labels(first:20) { nodes { name } }
              milestone { title }
              assignees(first:5) { nodes { login } }
              parent { number repository { nameWithOwner } }
              subIssuesSummary { total completed }
              blockedBy(first:10) { nodes { number state repository { nameWithOwner } } }
              closedByPullRequestsReferences(first:5) { nodes { number state } } }
            ... on PullRequest { number title state isDraft url bodyText updatedAt
              repository { nameWithOwner }
              labels(first:20) { nodes { name } } }
          }
        }
      }
    }
  }
}
"""


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest_path(hag: str) -> str:
    return os.path.join(data_dir(hag), "board_digest.json")


def _short(repo: str) -> str:
    return repo.split("/", 1)[1] if "/" in repo else repo


def fetch_board(owner: str, number: int) -> list[dict]:
    """All board items in board order, with the fields the digest needs."""
    from sources import gh_graphql
    items: list[dict] = []
    after, scope = None, None
    while True:
        data = None
        for s in ([scope] if scope else ["user", "organization"]):
            try:
                d = gh_graphql(QUERY % s, login=owner, number=int(number), after=after or "")
            except RuntimeError:
                continue
            node = (d.get("data") or {}).get(s) or {}
            if node.get("projectV2"):
                data, scope = node["projectV2"], s
                break
        if data is None:
            raise RuntimeError(f"project {owner}/{number} not readable as user or organization project")
        for n in data["items"]["nodes"]:
            c = n.get("content") or {}
            if not c or "number" not in c:
                continue
            repo = (c.get("repository") or {}).get("nameWithOwner", "")
            blockers = [f"{_short((b.get('repository') or {}).get('nameWithOwner', repo))}#{b['number']}"
                        for b in ((c.get("blockedBy") or {}).get("nodes") or []) if b.get("state") == "OPEN"]
            parent = c.get("parent") or None
            items.append({
                "pos": len(items) + 1,
                "item": f"{_short(repo)}#{c['number']}",
                "repo": repo, "number": c["number"],
                "kind": "pr" if c.get("__typename") == "PullRequest" else "issue",
                "title": c.get("title", ""), "state": c.get("state", ""),
                "status": ((n.get("status") or {}).get("name") or ""),
                "labels": [l["name"] for l in (c.get("labels") or {}).get("nodes", [])],
                "milestone": ((c.get("milestone") or {}).get("title") or ""),
                "assignees": [a["login"] for a in (c.get("assignees") or {}).get("nodes", [])],
                "url": c.get("url", ""), "draft": bool(c.get("isDraft")),
                "updated": c.get("updatedAt", ""),
                "excerpt": re.sub(r"\s+", " ", c.get("bodyText") or "")[:EXCERPT_CHARS],
                "blocked_by": blockers,
                "parent": (f"{_short((parent.get('repository') or {}).get('nameWithOwner', repo))}"
                           f"#{parent['number']}") if parent else "",
                "sub_issues": (c.get("subIssuesSummary") or {}).get("total", 0),
                "linked_prs": [p["number"] for p in ((c.get("closedByPullRequestsReferences") or {})
                                                     .get("nodes") or [])],
            })
        pi = data["items"]["pageInfo"]
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")
    return items


def _match_repo(item: dict, pattern: str) -> bool:
    p = pattern.lower()
    return any(fnmatch.fnmatch(x, p) for x in (item["repo"].lower(), _short(item["repo"]).lower()))


def score_item(item: dict, ctx: dict, routed: bool = False) -> tuple[float | None, list[str]]:
    """(score, why tokens) for one item against one working context; None = never."""
    never = ctx.get("never") or {}
    if any(_match_repo(item, r) for r in never.get("repos") or []):
        return None, ["never:repo"]
    if set(never.get("labels") or []) & set(item["labels"]):
        return None, ["never:label"]
    aff = ctx.get("affinity") or {}
    score, why = 0.0, []
    for r, w in (aff.get("repos") or {}).items():
        if _match_repo(item, r):
            score += float(w)
            why.append(f"repo:{_short(r)}{w:+g}")
    for l, w in (aff.get("labels") or {}).items():
        if l in item["labels"]:
            score += float(w)
            why.append(f"label:{l}{w:+g}")
    text = f"{item['title']} {item['excerpt']}".lower()
    for k, w in (aff.get("keywords") or {}).items():
        if k.lower() in text:
            score += float(w)
            why.append(f"kw:{k}{w:+g}")
    for g, w in (aff.get("paths") or {}).items():
        prefix = g.split("*", 1)[0].rstrip("/").lower()
        if prefix and prefix in text:
            score += float(w)
            why.append(f"path:{prefix}{w:+g}")
    if routed:
        score += ROUTING_BONUS
        why.append(f"routed{ROUTING_BONUS:+g}")
    if item.get("blocked_by"):
        score -= BLOCKED_PENALTY
        why.append(f"blocked-by:{','.join(item['blocked_by'])}{-BLOCKED_PENALTY:+g}")
    return score, why


def _routing(view) -> dict[str, set[str]]:
    """item key -> roles a decline suggested (`suggest=` on a release)."""
    out: dict[str, set[str]] = {}
    from ledger import item_key
    for e in view.ledger.events():
        if e.get("type") == "release" and e.get("kind") == "decline" and e.get("suggest"):
            out.setdefault(item_key(e.get("item", "")), set()).update(
                s.strip() for s in str(e["suggest"]).split(",") if s.strip())
    return out


def build(spec: HagSpec, view=None, items: list[dict] | None = None) -> dict:
    """The digest document: eligible items, per-role ranked candidates, unowned."""
    from ledger import item_key
    fillers = [r for r in spec.roles if r.fill == "self"]
    src_desc = []
    if items is None:
        items = []
        for src in spec.sources or []:
            if src.get("kind") != "github-project":
                continue
            src_desc.append(f"github-project:{src['owner']}/{src['number']}")
            skip = set(src.get("skip_status") or ["Done"])
            src_repos = {r.lower() for r in (src.get("repos") or [])}
            for it in fetch_board(src["owner"], int(src["number"])):
                if it["state"] not in ("OPEN", "") or it["status"] in skip:
                    continue
                if src_repos and it["repo"].lower() not in src_repos:
                    continue
                items.append(dict(it, source=src_desc[-1]))
    taken = (view.taken() | view.offered_open()) if view is not None else set()
    routing = _routing(view) if view is not None else {}
    declined = {r.slug: set(view.declined(r.slug)) for r in fillers} if view is not None else {}
    n = max(len(items), 1)
    roles: dict[str, list[dict]] = {}
    for r in fillers:
        ctx = r.working_context()
        ranked = []
        for it in items:
            key = item_key(it["item"])
            if key in taken or key in declined.get(r.slug, set()):
                continue
            sc, why = score_item(it, ctx, routed=r.slug in routing.get(key, set()))
            if sc is None:
                continue
            ranked.append({"item": it["item"], "verb": "review" if it["kind"] == "pr" else "fix",
                           "pos": it["pos"], "score": round(sc, 2),
                           "rank": round(sc - it["pos"] / n, 3), "why": why})
        ranked.sort(key=lambda x: -x["rank"])
        roles[r.slug] = ranked[:15]
    unowned = []
    if fillers and view is not None:
        for it in items:
            key = item_key(it["item"])
            if key not in taken and all(key in declined.get(r.slug, set()) for r in fillers):
                unowned.append(it["item"])
    return {"hag": spec.hag, "generated_utc": now_iso(), "sources": src_desc,
            "n_items": len(items), "items": {it["item"]: it for it in items},
            "roles": roles, "unowned": unowned}


def write(spec: HagSpec, doc: dict) -> str:
    path = digest_path(spec.hag)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)
    return path


def load(hag: str, max_age_min: float = DIGEST_MAX_AGE_MIN) -> dict | None:
    """The digest if present and fresh, else None (callers fall back to the board)."""
    path = digest_path(hag)
    try:
        if (time.time() - os.path.getmtime(path)) / 60 > max_age_min:
            return None
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def candidates(hag: str, slug: str, taken: set[str], declined: set[str], limit: int = 5,
               min_score: float = DEFAULT_MIN_SCORE) -> list[dict] | None:
    """Ranked digest candidates for one role with excerpts; None when no fresh digest.
    Items under `min_score` are withheld unless nothing clears it (then the top few
    are shown, so an empty queue still sees the best available work)."""
    doc = load(hag)
    if doc is None:
        return None
    from ledger import item_key
    rows = [r for r in (doc.get("roles") or {}).get(slug, [])
            if item_key(r["item"]) not in taken and item_key(r["item"]) not in declined]
    strong = [r for r in rows if r["score"] >= min_score]
    out = []
    for r in (strong or rows)[:limit]:
        it = (doc.get("items") or {}).get(r["item"], {})
        out.append(dict(it, **r, weak=not strong))
    return out


def fmt_candidate(c: dict, excerpt: int = 220) -> str:
    extra = f" blocked-by={','.join(c['blocked_by'])}" if c.get("blocked_by") else ""
    return (f"BOARD {c['item']}:{c['verb']} pos={c['pos']} score={c['score']:g}"
            f"{' (weak)' if c.get('weak') else ''} [{' '.join(c.get('why') or []) or 'no affinity'}]{extra}"
            f" :: {c.get('title', '')[:80]} :: {c.get('excerpt', '')[:excerpt]}")


# ---- CLI -------------------------------------------------------------------

def _spec_view(a, need_view: bool = True):
    spec = HagSpec.load(find_spec(a.hag, getattr(a, "spec", "")))
    view = None
    if need_view:
        from ledger import Ledger, View
        led = Ledger(spec.hag, root=spec.ledger_dir or None, sync=not getattr(a, "no_sync", False))
        if led.exists():
            if led.sync:
                led.pull()
            view = View(led, spec)
    return spec, view


def cmd_digest(a) -> int:
    spec, view = _spec_view(a)
    if a.refresh:
        if not a.dry_run:
            from watchers import tick
            tick(spec.hag, "digest")  # self-tick: an old tick is a dead timer, never a quiet board
        t0 = time.time()
        doc = build(spec, view)
        dt = time.time() - t0
        summary = (f"digest {spec.hag}: {doc['n_items']} open items, "
                   + ", ".join(f"{k}={len(v)}" for k, v in doc["roles"].items())
                   + f", unowned={len(doc['unowned'])} ({dt:.1f}s)")
        if a.dry_run:
            print("[dry-run] " + summary)
        else:
            print(summary + f" -> {write(spec, doc)}")
            try:
                from hag_telemetry import append_event, build_event, resolve_dir
                append_event(resolve_dir(None, spec.hag), build_event(
                    spec.hag, "digest", "script", "watch_event",
                    ["kind=digest-refresh", f"note={doc['n_items']} items unowned={len(doc['unowned'])}"]))
            except Exception as exc:  # noqa: BLE001
                print(f"(telemetry emit failed: {exc})", file=sys.stderr)
        if not a.slug:
            return 0
        docmem = doc
    else:
        docmem = load(spec.hag, max_age_min=1e9)
        if docmem is None:
            print(f"no digest for {spec.hag}; run `hag.py digest --hag {spec.hag} --refresh`")
            return 1
    if a.slug:
        rows = (docmem.get("roles") or {}).get(a.slug)
        if rows is None:
            print(f"{a.slug} is not a self-filling role in the digest")
            return 1
        print(f"# {a.slug} (digest {docmem['generated_utc']}); context: "
              + json.dumps(spec.role(a.slug).working_context() if spec.role(a.slug) else {}))
        for r in rows[:a.limit]:
            it = dict((docmem.get("items") or {}).get(r["item"], {}), **r)
            print(fmt_candidate(it, excerpt=a.excerpt))
        if docmem.get("unowned"):
            print(f"# unowned (passed by every self-filling role): {', '.join(docmem['unowned'])}")
    return 0


def cmd_triage(a) -> int:
    """Claims one at a time (first-landed-wins), passes in ONE commit."""
    from ledger import LedgerError, item_key, sync_push
    import ledger_cli
    spec, view = _spec_view(a)
    if view is None:
        print(f"ERROR: no ledger for {spec.hag}")
        return 1
    if spec.role(a.slug) is None:
        print(f"ERROR: {a.slug} is not a role in {spec.hag}")
        return 1
    passes = []
    for p in a.pass_ or []:
        parts = p.split(":")
        if len(parts) < 2 or parts[1] not in PASS_CLASSES:
            print(f"ERROR: --pass {p!r}: want ITEM:CLASS[:suggest=ROLE[,ROLE]][:note=TEXT], "
                  f"CLASS one of {PASS_CLASSES}")
            return 1
        rec = {"item": parts[0], "cls": parts[1], "suggest": "", "note": ""}
        for extra in parts[2:]:
            k, _, v = extra.partition("=")
            if k in ("suggest", "note"):
                rec[k] = v
        for s in filter(None, rec["suggest"].split(",")):
            if spec.role(s) is None:
                print(f"ERROR: suggest={s!r} is not a role in {spec.hag}")
                return 1
        passes.append(rec)
    rc = 0
    for item in a.claim or []:
        ns = type("A", (), dict(hag=a.hag, spec=getattr(a, "spec", ""), slug=a.slug, item=item, verb="",
                                base=a.base, src="", dry_run=a.dry_run, ledger_dir="",
                                no_sync=getattr(a, "no_sync", False)))()
        r = ledger_cli.cmd_claim(ns)
        rc = rc or (0 if r in (0,) else r)
    if passes:
        if a.dry_run:
            for p in passes:
                print(f"[dry-run] pass {item_key(p['item'])} class={p['cls']}"
                      + (f" suggest={p['suggest']}" if p["suggest"] else ""))
        else:
            led = view.ledger
            evs = []
            for p in passes:
                evs.append(led.append(a.slug, "release", _commit=False, item=item_key(p["item"]),
                                      kind="decline", cls=p["cls"], suggest=p["suggest"] or None,
                                      reason=p["note"] or p["cls"], retry=a.retry or None))
            try:
                led.commit_own(a.slug, f"{spec.hag} {a.slug} triage: {len(evs)} pass")
            except LedgerError as exc:
                print(f"ERROR: {exc}")
                return 1
            pushed = sync_push(led.root) if led.sync else True
            print(f"passed {len(evs)} item(s) in one commit: "
                  + ", ".join(f"{e['item']}({e['cls']})" for e in evs)
                  + ("" if pushed else " (push pending: `hag.py sync`)"))
    return rc


def cmd_context(a) -> int:
    path = find_spec(a.hag, getattr(a, "spec", ""))
    spec = HagSpec.load(path)
    targets = [spec.role(a.slug)] if a.slug else list(spec.roles)
    if a.slug and targets[0] is None:
        print(f"ERROR: {a.slug} is not a role in {spec.hag}")
        return 1
    if a.action == "show":
        for r in targets:
            src = "context" if r.context else ("scope (derived)" if r.scope else "none")
            print(f"{r.slug} [{src}]: {json.dumps(r.working_context())}")
        return 0
    changed = []
    if a.action == "set":
        if not a.slug or not a.file:
            print("ERROR: context set needs --slug and --file CONTEXT.json")
            return 1
        with open(a.file) as fh:
            ctx = json.load(fh)
        if not isinstance(ctx, dict) or set(ctx) - {"brief", "affinity", "never"}:
            print("ERROR: context keys must be a subset of brief, affinity, never")
            return 1
        targets[0].context, targets[0].scope = ctx, {}
        changed.append(targets[0].slug)
    elif a.action == "migrate":
        for r in targets:
            if r.scope and not r.context:
                r.context = scope_to_context(r.scope)
            if r.scope:
                r.scope = {}
                changed.append(r.slug)
    if not changed:
        print("nothing to change")
        return 0
    if a.dry_run:
        print(f"[dry-run] would update context for {changed} in {path}")
        for s in changed:
            print(f"  {s}: {json.dumps(spec.role(s).context)}")
        return 0
    from hag import persist_spec
    persist_spec(spec, path, note=f"context {a.action} {','.join(changed)}")
    print(f"context {a.action}: {changed}")
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("digest", help="shared board digest: refresh once per HAG, rank per role")
    p.add_argument("--hag", required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--refresh", action="store_true", help="fetch the board once and rewrite the digest")
    p.add_argument("--slug", default="", help="print this role's ranked candidates")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--excerpt", type=int, default=220)
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="build but do not write")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("triage", help="batch claim/pass for one role (passes land as ONE commit)")
    p.add_argument("--hag", required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--slug", required=True)
    p.add_argument("--claim", action="append", help="ITEM[:verb] (repeatable; first-landed-wins each)")
    p.add_argument("--pass", dest="pass_", action="append",
                   help=f"ITEM:CLASS[:suggest=ROLE][:note=TEXT]; CLASS in {', '.join(PASS_CLASSES)}")
    p.add_argument("--base", default="")
    p.add_argument("--retry", default="", help="retry condition recorded on every pass")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("context", help="show / set / migrate role working contexts (replaces scope)")
    p.add_argument("action", choices=["show", "set", "migrate"])
    p.add_argument("--hag", required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--slug", default="")
    p.add_argument("--file", default="", help="context JSON for `set`")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_context)
