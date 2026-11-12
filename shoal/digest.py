#!/usr/bin/env python3
"""Shared board digest, batch triage, and working context (architecture doc §4).

One board read per shoal, not one per self-filling watcher: `shoal digest --refresh`
(a systemd timer when the spec opts in with `features.digest`) fetches the priority
table ONCE, with body excerpts, blocking relations and linked PRs, scores every open
item against every self-filling role's working context, and writes
`$SHOAL_DATA_ROOT/<id>/board_digest.json`. A role's ledger-watch then reads that file
instead of GitHub, and a board wake carries enough (excerpt, score, why tokens) to
claim or pass without opening each issue.

Keyword ranking (the fallback when `features.matcher` is off or in shadow mode; soft, never a
gate except `never`):
  score = sum of the role's optional affinity weights matched (repo, labels, keywords in
          title+body, path prefixes in body) + routing bonus (+2 when another role's decline
          suggested this role) - 1 when an open blocker exists
  rank  = score - board_pos / N          (board order breaks ties among equal fits)
A role with no affinity weights ranks by board order alone. An item matching a role's
`never` list is excluded for that role. An item every self-filling role has passed on is
listed under `unowned`.

Semantic mode (`features.matcher`, architecture §4.1; matcher.py): a model scores every
item 0-3 against every lane's brief. Lanes rank by fit, then EFFECTIVE board position:
items blocking an active lane's work are lifted above what they block, and items with an
open blocker sink below every workable item. An `aux` role ranks the free workable items
whose best fit to any other ACTIVE role is lowest (minmax), unblockers first. With
`live: false` (shadow) the live digest is unchanged and the semantic ranking is written to
`board_digest.semantic.json` for `digest --compare`; without `features.matcher` nothing
here changes.

`shoal triage` records a role's decisions in one go: each claim keeps the
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
import config  # noqa: E402

from roles import ShoalSpec, contexts_dir, data_dir, find_spec, scope_to_context  # noqa: E402

PASS_CLASSES = ("out-of-lane", "blocked", "needs-maintainer", "needs-hardware", "duplicate-or-stale")
EXCERPT_CHARS = 600
DIGEST_MAX_AGE_MIN = 30.0
ROUTING_BONUS = 2.0
BLOCKED_PENALTY = 1.0
DEFAULT_MIN_SCORE = 0.5
# a role with fewer ranked candidates than this gets back the items it passed on for these classes, last (`build`)
FALLBACK_BELOW = 6
FALLBACK_CLASSES = ("out-of-lane", "blocked")

QUERY = """
query($login:String!, $number:Int!, $after:String) {
  %s(login:$login) {
    projectV2(number:$number) {
      title
      url
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


BOARD_META: dict[str, dict] = {}  # `<owner>/<number>` -> the title and page a board read reported


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest_path(sid: str) -> str:
    return os.path.join(data_dir(sid), "board_digest.json")


def _short(repo: str) -> str:
    return repo.split("/", 1)[1] if "/" in repo else repo


def fetch_board(owner: str, number: int) -> list[dict]:
    """All board items in board order, with the fields the digest needs."""
    from sources import board_fixture, gh_graphql
    fixture = board_fixture()  # SHOAL_BOARD_FIXTURE (tests): no board read
    if fixture is not None:
        return fixture
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
        BOARD_META[f"{owner}/{number}"] = {"title": data.get("title") or "", "url": data.get("url") or ""}
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


def has_affinity(ctx: dict) -> bool:
    """True when a working context carries any affinity weight (repos, labels, keywords, paths)."""
    aff = (ctx or {}).get("affinity") or {}
    return any(aff.get(k) for k in ("repos", "labels", "keywords", "paths"))


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


def semantic_path(sid: str) -> str:
    return os.path.join(data_dir(sid), "board_digest.semantic.json")


def _never(item: dict, ctx: dict) -> bool:
    never = ctx.get("never") or {}
    return (any(_match_repo(item, r) for r in never.get("repos") or [])
            or bool(set(never.get("labels") or []) & set(item["labels"])))


def active_lanes(spec: ShoalSpec, lanes: list[str]) -> list[str]:
    """Scored roles that hold a session now (a parked role's territory is aux territory)."""
    return [s for s in lanes if (spec.role(s) is not None and spec.role(s).session)]


def effective_positions(spec: ShoalSpec, view, items: list[dict], sem: dict | None) -> tuple[dict, set]:
    """(item -> effective board position, items lifted as unblockers). The board is never
    reordered; this is the order the digest ranks by."""
    from ledger import item_key
    n = max(len(items), 1)
    eff = {it["item"]: float(it["pos"]) for it in items}
    by_key = {item_key(it["item"]): it["item"] for it in items}
    work: set[str] = set()
    if view is not None:
        for r in spec.roles:
            if not r.session:
                continue
            q = view.current_queue(r.slug) or {}
            for w in [q.get("active") or ""] + list(q.get("next") or []) + [c["item"] for c in view.active(r.slug)]:
                if w:
                    work.add(item_key(w))
    unblock: set[str] = set()
    for it in items:
        if item_key(it["item"]) in work:
            for b in it.get("blocked_by") or []:
                bi = by_key.get(item_key(b))
                if bi and item_key(bi) not in work:
                    eff[bi] = min(eff[bi], it["pos"] - 0.5)
                    unblock.add(bi)
    if view is not None:
        for r in spec.roles:
            for b in view.blocked_by(r.slug):  # a peer's session-blocked wait names this item
                bi = by_key.get(item_key(b["item"])) if b.get("item") else None
                if bi:
                    eff[bi] = min(eff[bi], 0.5)
                    unblock.add(bi)
    fits = (sem or {}).get("fits") or {}
    for it in items:
        f = fits.get(it["item"]) or {}
        blocked = (bool(it.get("blocked_by")) or any(l.lower() == "status: blocked" for l in it["labels"])
                   or f.get("class") == "blocked")
        if blocked and it["item"] not in unblock:
            eff[it["item"]] = float(it["pos"] + n)
    return eff, unblock


def semantic_roles(spec: ShoalSpec, view, items: list[dict], sem: dict, eff: dict, unblock: set,
                   taken: set, declined: dict) -> dict[str, list[dict]]:
    """Per self-filling role: lanes by fit then effective position; aux roles by minmax."""
    from ledger import item_key
    n = max(len(items), 1) + 1
    fits = sem.get("fits") or {}
    active = active_lanes(spec, sem.get("lanes") or [])
    out: dict[str, list[dict]] = {}
    for r in [r for r in spec.roles if r.fill == "self"]:
        ctx = r.working_context() or {}
        rows, spare = [], []
        mine = declined.get(r.slug, {})
        for it in items:
            key = item_key(it["item"])
            f = fits.get(it["item"])
            if f is None or key in taken or _never(it, ctx):
                continue
            if key in mine or (not getattr(r, "aux", False) and int((f.get("fits") or {}).get(r.slug, 0)) < 1):
                # passed on as out of lane or blocked, or judged no fit: kept back as a fallback for a short queue
                cls = str(mine[key].get("cls") or "") if key in mine and isinstance(mine, dict) else ""
                if (key not in mine or cls in FALLBACK_CLASSES) and f.get("class") == "workable" \
                        and not getattr(r, "aux", False):
                    e = eff.get(it["item"], it["pos"])
                    spare.append({"item": it["item"], "verb": "review" if it["kind"] == "pr" else "fix", "pos": it["pos"],
                                  "eff_pos": e, "class": f.get("class"), "score": 0.0, "strong": False,
                                  "rank": round(-e / n, 3), **({"declined": cls} if cls else {}),
                                  "why": [f"declined-earlier:{cls}" if cls else "sem:0", f.get("why", "")]})
                continue
            base = {"item": it["item"], "verb": "review" if it["kind"] == "pr" else "fix", "pos": it["pos"],
                    "eff_pos": eff.get(it["item"], it["pos"]), "class": f.get("class")}
            e = eff.get(it["item"], it["pos"])
            if getattr(r, "aux", False):
                if f.get("class") != "workable":
                    continue
                others = {s: int(f["fits"].get(s, 0)) for s in active if s != r.slug}
                mx = max(others.values(), default=0)
                owners = [s for s, v in others.items() if v >= 2]
                if owners and not all(key in declined.get(s, set()) for s in owners):
                    continue
                tier = 0 if it["item"] in unblock else 1
                rows.append(dict(base, score=float(3 - mx), strong=mx <= 1, tier=tier,
                                 rank=round((1 - tier) * 10 + (3 - mx) - e / n, 3),
                                 why=[f"aux:max-fit={mx}"] + (["unblocks"] if tier == 0 else []) + [f.get("why", "")]))
            else:
                fit = int(f["fits"].get(r.slug, 0))
                if fit < 1:
                    continue
                rows.append(dict(base, score=float(fit), strong=fit >= 2, rank=round(fit - e / n, 3),
                                 why=[f"sem:{fit}", f.get("why", "")]
                                 + ([f"class:{f['class']}"] if f.get("class") != "workable" else [])
                                 + (["unblocks"] if it["item"] in unblock else [])))
        rows.sort(key=lambda x: -x["rank"])
        spare.sort(key=lambda x: -x["rank"])
        # capacious (maintainer, 2026-11-12): a lane short of fitting work gets the board's other workable items, last
        # and weak, so a fill=self queue stays full; an imperfect fit beats an idle role
        out[r.slug] = (rows + (spare if len(rows) < FALLBACK_BELOW else []))[:15]
    return out


def build(spec: ShoalSpec, view=None, items: list[dict] | None = None, no_model: bool = False) -> dict:
    """The digest document: eligible items, per-role ranked candidates, unowned. With
    features.matcher in shadow mode the semantic document rides along under `shadow`."""
    from ledger import item_key
    fillers = [r for r in spec.roles if r.fill == "self"]
    src_desc, boards = [], []
    if items is None:
        items = []
        for src in spec.sources or []:
            if src.get("kind") != "github-project":
                continue
            src_desc.append(f"github-project:{src['owner']}/{src['number']}")
            skip = set(src.get("skip_status") or ["Done"])
            src_repos = {r.lower() for r in (src.get("repos") or [])}
            board = {"source": src_desc[-1], "title": "", "url": "", "done": 0}
            for it in fetch_board(src["owner"], int(src["number"])):
                if src_repos and it["repo"].lower() not in src_repos:
                    continue
                if it["state"] not in ("OPEN", "") or it["status"] in skip:
                    board["done"] += 1
                    continue
                items.append(dict(it, source=src_desc[-1]))
            board.update(BOARD_META.get(f"{src['owner']}/{src['number']}") or {})
            boards.append(board)
    taken = (view.taken() | view.offered_open()) if view is not None else set()
    routing = _routing(view) if view is not None else {}
    declined = {r.slug: view.declined(r.slug) for r in spec.roles} if view is not None else {}
    import matcher
    mcfg = matcher.config(spec)
    sem_doc = None
    if mcfg is not None:
        suggest = {it["item"]: routing.get(item_key(it["item"]), set()) for it in items}
        try:
            sem = matcher.score(spec, items, suggest=suggest, no_call=no_model)
        except Exception as exc:  # noqa: BLE001 - a matcher failure never takes the digest down
            sem = {"fits": {}, "lanes": [], "notes": [f"matcher failed: {str(exc)[:200]}"], "live": False,
                   "unscored": [it["item"] for it in items], "calls": 0}
        eff, unblock = effective_positions(spec, view, items, sem)
        s_items = {it["item"]: dict(it, eff_pos=eff[it["item"]], sem=(sem["fits"].get(it["item"]))) for it in items}
        s_roles = semantic_roles(spec, view, items, sem, eff, unblock, taken, declined)
        summary = {k: sem.get(k) for k in ("model", "backend", "calls", "latency_s", "scored_now", "live")}
        summary.update(unscored=len(sem.get("unscored") or []), withheld=len(sem.get("withheld") or []),
                       notes=(sem.get("notes") or [])[-8:], unblockers=sorted(unblock))
        sem_doc = {"shoal": spec.sid, "generated_utc": now_iso(), "sources": src_desc, "ranking": "semantic",
                   "n_items": len(items), "items": s_items, "roles": s_roles, "matcher": summary}
    live = bool(mcfg and mcfg.get("live") and sem_doc is not None and (sem_doc["matcher"].get("unscored", 0)
                                                                      < len(items) or not items))
    n = max(len(items), 1)
    roles: dict[str, list[dict]] = {}
    for r in ([] if live else fillers):
        ctx = r.working_context()
        # affinity weights are optional: a role with none ranks by board order alone, and every
        # item counts as a fit (no weak-candidate withholding for a role that expressed no weights)
        weighted = has_affinity(ctx)
        ranked, passed = [], []
        mine = declined.get(r.slug, {})
        for it in items:
            key = item_key(it["item"])
            if key in taken:
                continue
            sc, why = score_item(it, ctx, routed=r.slug in routing.get(key, set()))
            if sc is None:
                continue
            row = {"item": it["item"], "verb": "review" if it["kind"] == "pr" else "fix",
                   "pos": it["pos"], "score": round(sc, 2),
                   "rank": round(sc - it["pos"] / n, 3), "why": why,
                   **({} if weighted else {"strong": True})}
            if key in mine:
                # an item it passed on comes back only as a fallback, and only for a lane or blocker judgment
                cls = str(mine[key].get("cls") or "")
                if cls in FALLBACK_CLASSES:
                    passed.append(dict(row, strong=False, declined=cls, why=why + [f"declined-earlier:{cls}"]))
                continue
            ranked.append(row)
        ranked.sort(key=lambda x: -x["rank"])
        passed.sort(key=lambda x: -x["rank"])
        # capacious (maintainer, 2026-11-11): a role short of candidates gets back what it passed on as out of lane or
        # blocked, after everything else, so a fill=self queue stays full; a suboptimal match beats an idle role
        roles[r.slug] = (ranked + (passed if len(ranked) < FALLBACK_BELOW else []))[:15]
    unowned = []
    if fillers and view is not None:
        for it in items:
            key = item_key(it["item"])
            if key not in taken and all(key in declined.get(r.slug, set()) for r in fillers):
                unowned.append(it["item"])
    if live:
        doc = dict(sem_doc, unowned=unowned, boards=boards)
        return doc
    doc = {"shoal": spec.sid, "generated_utc": now_iso(), "sources": src_desc, "ranking": "keyword",
           "n_items": len(items), "items": {it["item"]: it for it in items},
           "roles": roles, "unowned": unowned, "boards": boards}
    if sem_doc is not None:
        for r in fillers:  # the keyword ranking means nothing for a lane with no brief
            if getattr(r, "aux", False):
                roles[r.slug] = sem_doc["roles"].get(r.slug, [])
        # shadow: the live ranking is untouched; the semantic one is written beside it. A live
        # matcher with nothing scored also lands here, so roles keep the keyword ranking.
        doc["shadow"] = dict(sem_doc, unowned=unowned)
        doc["matcher"] = dict(sem_doc["matcher"], shadow=True)
    return doc


def _write_json(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)


def write(spec: ShoalSpec, doc: dict) -> str:
    doc = dict(doc)
    shadow = doc.pop("shadow", None)
    if shadow is not None:
        _write_json(semantic_path(spec.sid), shadow)
    path = digest_path(spec.sid)
    _write_json(path, doc)
    try:
        note_history(spec.sid, doc)
    except Exception:  # noqa: BLE001 - the trend is a convenience: a digest is never lost to it
        pass
    return path


HISTORY_EVERY_S = 1800  # one reading per board per half hour is enough for a day's trend
HISTORY_KEEP_S = 90 * 86400


def history_path(sid: str) -> str:
    return os.path.join(data_dir(sid), "board_history.jsonl")


def note_history(sid: str, doc: dict, now: float | None = None) -> None:
    """Append each board's open and done counts (the Board tab's trends: open over time, closes a day), at most every
    HISTORY_EVERY_S per board, keeping HISTORY_KEEP_S of it. The digest itself holds only the open items."""
    now = time.time() if now is None else now
    path = history_path(sid)
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                # line by line: one torn or foreign line is skipped, not the history (a whole-file parse dropped it all,
                # and the next append wrote back only the new rows)
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)):
                    rows.append(r)
    except OSError:
        pass
    last = {}
    for r in rows:
        last[r.get("source")] = r["ts"]
    items = doc.get("items") or {}
    new = []
    for b in doc.get("boards") or []:
        src = b.get("source") or ""
        if now - float(last.get(src) or 0) < HISTORY_EVERY_S:
            continue
        new.append({"ts": int(now), "source": src, "open": sum(1 for it in items.values() if it.get("source") == src),
                    "done": int(b.get("done") or 0)})
    if not new:
        return
    keep = [r for r in rows if now - r["ts"] <= HISTORY_KEEP_S] + new
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"  # two writers on one host never share a temporary file
    with open(tmp, "w") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in keep)
    os.replace(tmp, path)


def load(sid: str, max_age_min: float = DIGEST_MAX_AGE_MIN) -> dict | None:
    """The digest if present and fresh, else None (callers fall back to the board)."""
    path = digest_path(sid)
    try:
        if (time.time() - os.path.getmtime(path)) / 60 > max_age_min:
            return None
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def candidates(sid: str, slug: str, taken: set[str], declined: set[str], limit: int = 5,
               min_score: float = DEFAULT_MIN_SCORE) -> list[dict] | None:
    """Ranked digest candidates for one role with excerpts; None when no fresh digest. Strong
    candidates first (`min_score`), then weaker ones to fill `limit`, then the fallback rows the digest
    keeps for a role short of work (items it passed on as out of lane or blocked): capacious, so a
    short queue always sees the best available work (maintainer, 2026-11-11)."""
    doc = load(sid)
    if doc is None:
        return None
    from ledger import item_key
    rows = [r for r in (doc.get("roles") or {}).get(slug, [])
            if item_key(r["item"]) not in taken and (r.get("declined") or item_key(r["item"]) not in declined)]
    strong = [r for r in rows if not r.get("declined") and r.get("strong", r["score"] >= min_score)]
    rest = [r for r in rows if r not in strong]
    out = []
    for r in (strong + rest)[:limit]:
        it = (doc.get("items") or {}).get(r["item"], {})
        out.append(dict(it, **r, weak=r not in strong))
    return out


def fmt_candidate(c: dict, excerpt: int = 220) -> str:
    extra = f" blocked-by={','.join(c['blocked_by'])}" if c.get("blocked_by") else ""
    return (f"BOARD {c['item']}:{c['verb']} pos={c['pos']} score={c['score']:g}"
            f"{' (weak)' if c.get('weak') else ''} [{' '.join(c.get('why') or []) or 'no affinity'}]{extra}"
            f" :: {c.get('title', '')[:80]} :: {c.get('excerpt', '')[:excerpt]}")


# ---- CLI -------------------------------------------------------------------

def _spec_view(a, need_view: bool = True):
    spec = ShoalSpec.load(find_spec(a.sid, getattr(a, "spec", "")))
    view = None
    if need_view:
        from ledger import Ledger, View
        led = Ledger(spec.sid, root=spec.ledger_dir or None, sync=not getattr(a, "no_sync", False))
        if led.exists():
            if led.sync:
                led.pull()
            view = View(led, spec)
    return spec, view


def cmd_digest(a) -> int:
    spec, view = _spec_view(a)
    if getattr(a, "compare", False):
        return cmd_compare(a, spec, view)
    if a.refresh:
        if not a.dry_run:
            from watchers import tick
            tick(spec.sid, "digest")  # self-tick: an old tick is a dead timer, never a quiet board
        t0 = time.time()
        doc = build(spec, view, no_model=a.no_model)
        dt = time.time() - t0
        m = doc.get("matcher") or {}
        summary = (f"digest {spec.sid}: {doc['n_items']} open items, ranking={doc.get('ranking', 'keyword')}"
                   + (" (semantic in shadow)" if m.get("shadow") else "") + ", "
                   + ", ".join(f"{k}={len(v)}" for k, v in doc["roles"].items())
                   + f", unowned={len(doc['unowned'])} ({dt:.1f}s)"
                   + (f"; matcher model={m.get('model')} calls={m.get('calls')} scored_now={m.get('scored_now')} "
                      f"unscored={m.get('unscored')}" if m else ""))
        for note in m.get("notes") or []:
            print(f"matcher: {note}", file=sys.stderr)
        if a.dry_run:
            print("[dry-run] " + summary)
        else:
            print(summary + f" -> {write(spec, doc)}")
            try:
                from shoal_telemetry import append_event, build_event, resolve_dir
                append_event(resolve_dir(None, spec.sid), build_event(
                    spec.sid, "digest", "script", "watch_event",
                    ["kind=digest-refresh", f"note={doc['n_items']} items unowned={len(doc['unowned'])}"]))
            except Exception as exc:  # noqa: BLE001
                print(f"(telemetry emit failed: {exc})", file=sys.stderr)
        if not a.slug:
            return 0
        docmem = doc
        if a.semantic and doc.get("shadow"):
            docmem = doc["shadow"]
    else:
        docmem = load_semantic(spec.sid) if a.semantic else load(spec.sid, max_age_min=1e9)
        if docmem is None:
            print(f"no {'semantic ' if a.semantic else ''}digest for {spec.sid}; run "
                  f"`shoal digest --shoal {spec.sid} --refresh`")
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


def load_semantic(sid: str) -> dict | None:
    """The live digest when it is semantic, else the shadow file."""
    doc = load(sid, max_age_min=1e9)
    if doc is not None and doc.get("ranking") == "semantic":
        return doc
    try:
        with open(semantic_path(sid)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def compare(spec: ShoalSpec, view, k: int = 10, n_claims: int = 80) -> dict:
    """Switch bar (H8 proposal §2.5): per self-filling lane, over the whole board (no taken
    filter), how often the lane's recent claims sat in its top k, and how often its recorded
    out-of-lane declines did, semantic vs keyword. Items since closed are skipped."""
    from ledger import item_key
    semd = load_semantic(spec.sid)
    if semd is None:
        raise RuntimeError("no semantic digest: enable features.matcher and refresh first")
    items = list((semd.get("items") or {}).values())
    lanes = [r for r in spec.roles if r.fill == "self" and not getattr(r, "aux", False)]
    top: dict[str, dict[str, set]] = {}
    for r in lanes:
        ctx = r.working_context() or {}
        kw = []
        for it in items:
            sc, _ = score_item(it, ctx)
            if sc is not None:
                kw.append((sc, -it["pos"], item_key(it["item"])))
        sm = []
        for it in items:
            f = it.get("sem") or {}
            fit = int((f.get("fits") or {}).get(r.slug, 0))
            if fit >= 1 and not _never(it, ctx):
                sm.append((fit, -float(it.get("eff_pos", it["pos"])), item_key(it["item"])))
        top[r.slug] = {"keyword": {x[2] for x in sorted(kw, reverse=True)[:k]},
                       "semantic": {x[2] for x in sorted(sm, reverse=True)[:k]}}
    on_board = {item_key(it["item"]) for it in items}
    slugs = {r.slug for r in lanes}
    claims = [e for e in view.ledger.events() if e.get("type") == "claim" and e.get("by") in slugs]
    claims = [e for e in claims if item_key(e.get("item", "")) in on_board][-n_claims:]
    declines = [e for e in view.ledger.events() if e.get("type") == "release" and e.get("kind") == "decline"
                and e.get("by") in slugs and e.get("cls", "out-of-lane") == "out-of-lane"
                and item_key(e.get("item", "")) in on_board]
    res = {"k": k, "claims": len(claims), "declines": len(declines), "per_lane": {}}
    for mode in ("keyword", "semantic"):
        hit = sum(1 for e in claims if item_key(e["item"]) in top[e["by"]][mode])
        leak = sum(1 for e in declines if item_key(e["item"]) in top[e["by"]][mode])
        res[mode] = {"claim_recall": round(hit / len(claims), 3) if claims else None,
                     "decline_leakage": round(leak / len(declines), 3) if declines else None}
    for r in lanes:
        res["per_lane"][r.slug] = {
            m: [sum(1 for e in claims if e["by"] == r.slug and item_key(e["item"]) in top[r.slug][m]),
                sum(1 for e in declines if e["by"] == r.slug and item_key(e["item"]) in top[r.slug][m])]
            for m in ("keyword", "semantic")}
    kw, sm = res["keyword"], res["semantic"]
    recall_ok = (sm["claim_recall"] or 0) >= (kw["claim_recall"] or 0)
    leak_ok = (sm["decline_leakage"] or 0) <= (kw["decline_leakage"] or 0)
    res["bar_met"] = bool(claims) and recall_ok and leak_ok
    res["matcher"] = semd.get("matcher") or {}
    return res


def cmd_compare(a, spec: ShoalSpec, view) -> int:
    try:
        r = compare(spec, view, k=a.k)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    m = r["matcher"]
    print(f"# digest compare {spec.sid}: top-{r['k']} over the whole board; {r['claims']} recent claims and "
          f"{r['declines']} out-of-lane declines on items still open; matcher model={m.get('model')} "
          f"calls={m.get('calls')} latency={m.get('latency_s')}s unscored={m.get('unscored')}")
    for mode in ("keyword", "semantic"):
        print(f"{mode:9s} claim recall={r[mode]['claim_recall']}  decline leakage={r[mode]['decline_leakage']}")
    for slug, d in r["per_lane"].items():
        print(f"  {slug}: claims-in-top keyword={d['keyword'][0]} semantic={d['semantic'][0]}; "
              f"declines-in-top keyword={d['keyword'][1]} semantic={d['semantic'][1]}")
    print("SWITCH BAR: " + ("MET (semantic recall >= keyword and leakage <= keyword): "
                            "`shoal upgrade --spec <spec> --enable matcher.live`" if r["bar_met"] else
                            "NOT MET (keep shadow; see the per-lane lines)"))
    return 0 if r["bar_met"] else 3


def cmd_triage(a) -> int:
    """Claims one at a time (first-landed-wins), passes in ONE commit."""
    from ledger import LedgerError, item_key, sync_push
    import ledger_cli
    spec, view = _spec_view(a)
    if view is None:
        print(f"ERROR: no ledger for {spec.sid}")
        return 1
    if spec.role(a.slug) is None:
        print(f"ERROR: {a.slug} is not a role in {spec.sid}")
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
                print(f"ERROR: suggest={s!r} is not a role in {spec.sid}")
                return 1
        passes.append(rec)
    rc = 0
    for item in a.claim or []:
        ns = type("A", (), dict(sid=a.sid, spec=getattr(a, "spec", ""), slug=a.slug, item=item, verb="",
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
                led.commit_own(a.slug, f"{spec.sid} {a.slug} triage: {len(evs)} pass")
            except LedgerError as exc:
                print(f"ERROR: {exc}")
                return 1
            pushed = sync_push(led.root) if led.sync else True
            print(f"passed {len(evs)} item(s) in one commit: "
                  + ", ".join(f"{e['item']}({e['cls']})" for e in evs)
                  + ("" if pushed else " (push pending: `shoal sync`)"))
    return rc


def cmd_context(a) -> int:
    path = find_spec(a.sid, getattr(a, "spec", ""))
    spec = ShoalSpec.load(path)
    targets = [spec.role(a.slug)] if a.slug else list(spec.roles)
    if a.slug and targets[0] is None:
        print(f"ERROR: {a.slug} is not a role in {spec.sid}")
        return 1
    cdir = contexts_dir(spec.sid)
    if a.action == "show":
        for r in targets:
            src = "context" if r.context else ("scope (derived)" if r.scope else "none")
            f = os.path.join(cdir, f"{r.slug}.json")
            print(f"{r.slug} [{src}; file {'present' if os.path.exists(f) else 'absent'}: {f}]: "
                  f"{json.dumps(r.working_context())}")
        return 0
    if a.action == "export":
        os.makedirs(cdir, exist_ok=True)
        for r in targets:
            f = os.path.join(cdir, f"{r.slug}.json")
            if os.path.exists(f) and not a.force:
                print(f"kept {f} (exists; --force to overwrite)")
                continue
            if a.dry_run:
                print(f"[dry-run] would write {f}")
                continue
            with open(f, "w") as fh:
                json.dump(r.working_context() or {"brief": "", "affinity": {}, "never": {}}, fh, indent=1)
            print(f"wrote {f}")
        return 0
    changed = []

    def _read(path: str) -> dict | None:
        with open(path) as fh:
            ctx = json.load(fh)
        if not isinstance(ctx, dict) or set(ctx) - {"brief", "affinity", "never"}:
            print(f"ERROR: {path}: context keys must be a subset of brief, affinity, never")
            return None
        return ctx

    if a.action == "set":
        if not a.slug:
            print("ERROR: context set needs --slug (and --file, default <data-root>/<id>/contexts/<slug>.json)")
            return 1
        ctx = _read(a.file or os.path.join(cdir, f"{a.slug}.json"))
        if ctx is None:
            return 1
        targets[0].context, targets[0].scope = ctx, {}
        changed.append(targets[0].slug)
    elif a.action == "load":
        for r in targets:
            f = os.path.join(cdir, f"{r.slug}.json")
            if not os.path.exists(f):
                continue
            ctx = _read(f)
            if ctx is None:
                return 1
            if ctx != r.context or r.scope:
                r.context, r.scope = ctx, {}
                changed.append(r.slug)
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
    from cli import persist_spec
    persist_spec(spec, path, note=f"context {a.action} {','.join(changed)}")
    print(f"context {a.action}: {changed}")
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("digest", help="shared board digest: refresh once per shoal, rank per role")
    config.add_id_arg(p, required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--refresh", action="store_true", help="fetch the board once and rewrite the digest")
    p.add_argument("--slug", default="", help="print this role's ranked candidates")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--excerpt", type=int, default=220)
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="build but do not write")
    p.add_argument("--no-model", action="store_true",
                   help="semantic mode: use cached fits only, make no model call")
    p.add_argument("--semantic", action="store_true",
                   help="with --slug: show the semantic ranking (the shadow file while matcher.live is off)")
    p.add_argument("--compare", action="store_true",
                   help="switch bar: claim recall and decline leakage, semantic vs keyword (exit 0 = met)")
    p.add_argument("--k", type=int, default=10, help="--compare: top-k window")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("triage", help="batch claim/pass for one role (passes land as ONE commit)")
    config.add_id_arg(p, required=True)
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

    p = sub.add_parser("context", help="show / set / load / export / migrate role working contexts (replaces scope)")
    p.add_argument("action", choices=["show", "set", "load", "export", "migrate"],
                   help="load: every <data-root>/<id>/contexts/<slug>.json into the spec; "
                        "export: the spec's (or scope-derived) contexts to those files")
    config.add_id_arg(p, required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--slug", default="")
    p.add_argument("--file", default="", help="context JSON for `set` (default <data-root>/<id>/contexts/<slug>.json)")
    p.add_argument("--force", action="store_true", help="export: overwrite existing files")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_context)
