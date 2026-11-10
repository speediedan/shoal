#!/usr/bin/env python3
"""Priority-table sources for self-filling roles (readme 5.k, 2026-09-20).

A source is where a self-filling role looks when its queue is empty. The
only kind today is a GitHub Projects (v2) board, read in board order —
item order IS the priority (board convention: "the sessions pop
from the top"). Reading uses `gh api graphql` so the item's open/closed
state and repository come back in one call (`gh project item-list` omits
state, and a closed issue left on the board is the measured hygiene gap).

Spec shape (shoal.json `sources`):
  [{"kind": "github-project", "owner": "<owner>", "number": <project number>,
    "repos": ["<owner>/<repo>", ...],               # optional filter
    "skip_status": ["Done"]}]                        # optional

Per-role `scope` narrows further: {"repos": [...], "labels_any": [...],
"labels_none": [...]}. Candidates are returned in board order with their
position; the caller subtracts what the ledger already shows as taken.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import config  # sibling module: the package dir is on sys.path wherever this is imported

GH_ENV = dict(os.environ, PATH="/usr/bin:/bin:/usr/local/bin:" + os.environ.get("PATH", ""))
_CACHE: dict[str, tuple[float, list[dict]]] = {}

QUERY = """
query($login:String!, $number:Int!, $after:String) {
  %s(login:$login) {
    projectV2(number:$number) {
      title
      items(first:100, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          status: fieldValueByName(name:"Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            __typename
            ... on Issue { number title state url
              repository { nameWithOwner }
              labels(first:20) { nodes { name } }
              milestone { title }
              assignees(first:5) { nodes { login } } }
            ... on PullRequest { number title state isDraft url
              repository { nameWithOwner }
              labels(first:20) { nodes { name } } }
          }
        }
      }
    }
  }
}
"""


def gh_graphql(query: str, **vars) -> dict:
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in vars.items():
        argv += (["-F", f"{k}={v}"] if isinstance(v, int) else ["-f", f"{k}={v}"])
    r = subprocess.run(argv, capture_output=True, text=True, timeout=90, env=GH_ENV)
    if r.returncode != 0:
        raise RuntimeError(f"gh api graphql failed: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout)


def board_fixture() -> list[dict] | None:
    """Test hook: `SHOAL_BOARD_FIXTURE=<path to a JSON list of items>` replaces every board read
    (this module and digest.fetch_board) with those items, in file order. Each item needs at
    least `repo` and `number`; the other board fields get neutral defaults. None when unset."""
    path = config.env("BOARD_FIXTURE")
    if not path:
        return None
    with open(path) as fh:
        raw = json.load(fh)
    items = []
    for d in raw:
        repo = d.get("repo", "")
        short = repo.split("/", 1)[1] if "/" in repo else repo
        it = {"item": f"{short}#{d['number']}", "repo": repo, "number": d["number"], "kind": "issue",
              "title": "", "state": "OPEN", "status": "", "labels": [], "milestone": "", "assignees": [],
              "url": "", "draft": False, "project_item_id": "", "updated": "", "excerpt": "",
              "blocked_by": [], "parent": "", "sub_issues": 0, "linked_prs": []}
        it.update(d)
        it["pos"] = len(items) + 1
        items.append(it)
    return items


def github_project_items(owner: str, number: int, ttl_s: float = 120) -> list[dict]:
    """Board items in board order: [{pos, item, repo, number, kind, title,
    state, status, labels, url, draft}]. Cached per process for ttl_s."""
    fixture = board_fixture()
    if fixture is not None:
        return fixture
    ck = f"{owner}/{number}"
    now = time.time()
    if ck in _CACHE and now - _CACHE[ck][0] < ttl_s:
        return _CACHE[ck][1]
    items: list[dict] = []
    after = None
    scope = None
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
                continue  # draft notes
            repo = (c.get("repository") or {}).get("nameWithOwner", "")
            short = repo.split("/", 1)[1] if "/" in repo else repo
            items.append({
                "pos": len(items) + 1,
                "item": f"{short}#{c['number']}",
                "repo": repo, "number": c["number"],
                "kind": "pr" if c.get("__typename") == "PullRequest" else "issue",
                "title": c.get("title", ""), "state": c.get("state", ""),
                "status": ((n.get("status") or {}).get("name") or ""),
                "labels": [l["name"] for l in (c.get("labels") or {}).get("nodes", [])],
                "milestone": ((c.get("milestone") or {}).get("title") or ""),
                "assignees": [a["login"] for a in (c.get("assignees") or {}).get("nodes", [])],
                "url": c.get("url", ""), "draft": bool(c.get("isDraft")),
                "project_item_id": n.get("id", ""),
            })
        pi = data["items"]["pageInfo"]
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")
    _CACHE[ck] = (now, items)
    return items


def candidates(sources: list[dict], scope: dict | None, taken: set[str],
               declined: set[str], limit: int = 5, ttl_s: float = 120) -> list[dict]:
    """Board-ordered eligible items for one role: open, not skip-status, in
    scope, not taken by anyone in the ledger, not declined by this role."""
    scope = scope or {}
    want_repos = {r.lower() for r in (scope.get("repos") or [])}
    any_l = set(scope.get("labels_any") or [])
    none_l = set(scope.get("labels_none") or [])
    out: list[dict] = []
    for src in sources or []:
        if src.get("kind") != "github-project":
            continue
        src_repos = {r.lower() for r in (src.get("repos") or [])}
        skip = set(src.get("skip_status") or ["Done"])
        for it in github_project_items(src["owner"], int(src["number"]), ttl_s=ttl_s):
            if it["state"] not in ("OPEN", ""):
                continue
            if it["status"] in skip:
                continue
            if src_repos and it["repo"].lower() not in src_repos:
                continue
            if want_repos and not ({it["repo"].lower(), it["item"].split("#")[0].lower()} & want_repos):
                continue
            labels = set(it["labels"])
            if any_l and not (labels & any_l):
                continue
            if none_l and (labels & none_l):
                continue
            key = it["item"].lower()
            if key in taken or key in declined:
                continue
            out.append(dict(it, verb="review" if it["kind"] == "pr" else "fix",
                            source=f"github-project:{src['owner']}/{src['number']}"))
            if len(out) >= limit:
                return out
    return out


def project_url(src: dict) -> str:
    """A user project's board page. A digest records the page GitHub reports, which also covers an organization's."""
    return f"https://github.com/users/{src['owner']}/projects/{src['number']}"


def parse_source(spec: str) -> dict:
    """CLI form: github-project:OWNER/NUMBER[:repo1|repo2]"""
    parts = spec.split(":")
    if len(parts) < 2 or parts[0] != "github-project" or "/" not in parts[1]:
        raise ValueError(f"bad --source {spec!r} (want github-project:OWNER/NUMBER[:repo|repo])")
    owner, number = parts[1].split("/", 1)
    src = {"kind": "github-project", "owner": owner, "number": int(number)}
    if len(parts) > 2 and parts[2]:
        src["repos"] = parts[2].split("|")
    return src


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: sources.py OWNER PROJECT_NUMBER")
        sys.exit(2)
    owner, number = sys.argv[1:3]
    for it in github_project_items(owner, int(number)):
        print(f"{it['pos']:3d} {it['item']:<28} {it['state']:<6} {it['status']:<10} {it['title'][:60]}")
