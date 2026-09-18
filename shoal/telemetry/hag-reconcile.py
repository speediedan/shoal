#!/usr/bin/env python3
"""Nightly GitHub reconciliation for HAG telemetry (stdlib + `gh` CLI only).

Emits issue_closed / pr_opened / pr_merged for recently-updated items in the
participating repos, skipping events already in the log (idempotent by
type+number), then one `reconcile` marker event with the count. Ground truth
the lane-reported events are checked against.

Repos come from the HAG spec (`repos` in hag.json, default list when empty);
--repo overrides. --dry-run prints what would be emitted and writes nothing.
Each run touches /tmp/<hag>/tick-reconcile so `hag.py status` / `doctor` can
tell a reconcile that never ran from one that ran and found nothing (the v0
IndentationError sat undetected for three nights for lack of exactly that).

Usage: hag-reconcile.py --hag ID [--spec hag.json] [--repo R ...]
                        [--since-hours N] [--dir LOGDIR] [--dry-run]
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from hag_telemetry import append_event, build_event, resolve_dir, legacy_dirs  # noqa: E402

DEFAULT_REPOS = ["speediedan/interpretune", "speediedan/it-interp-engine-adapter"]
GH_ENV = dict(os.environ, PATH="/usr/bin:/bin:/usr/local/bin:" + os.environ.get("PATH", ""))


def seen(logdir, hag):
    have = set()
    for d in [logdir] + legacy_dirs(hag) + [os.path.join(logdir, hag)]:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not (fn.startswith("events-") and fn.endswith(".jsonl")):
                continue
            with open(os.path.join(d, fn)) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    key = e.get("type"), str(e.get("issue") or e.get("pr") or "")
                    if key[1]:
                        have.add(key)
    return have


def gh(*a):
    r = subprocess.run(["gh"] + list(a), capture_output=True, text=True, timeout=60,
                       env=GH_ENV)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(a[:3])} failed: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout) if r.stdout.strip() else []


def parse_gh_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def repos_for(a):
    if a.repo:
        return list(a.repo)
    spec_path = a.spec or os.path.join(
        os.environ.get("HAG_ROOT", "/home/speediedan/repos/distributed-insight/.agents/hags"),
        a.hag, "hag.json")
    try:
        with open(spec_path) as fh:
            repos = json.load(fh).get("repos") or []
        if repos:
            return repos
    except OSError:
        pass
    return list(DEFAULT_REPOS)


def tick(hag):
    d = os.path.join("/tmp", hag)
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "tick-reconcile")
        with open(p, "a"):
            os.utime(p, None)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hag", required=True)
    ap.add_argument("--spec", default="")
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--since-hours", type=float, default=24)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    a.dir = resolve_dir(a.dir, a.hag)
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=a.since_hours)
    have = seen(a.dir, a.hag)
    repos = repos_for(a)
    pending = []
    for repo in repos:
        try:
            prs = gh("pr", "list", "--repo", repo, "--state", "merged", "--limit", "50",
                     "--json", "number,title,mergedAt,updatedAt,mergeCommit")
            iss = gh("issue", "list", "--repo", repo, "--state", "closed", "--limit", "50",
                     "--json", "number,title,closedAt,updatedAt")
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            return 2
        for pr in prs:
            try:
                up = parse_gh_ts(pr["updatedAt"])
            except Exception:
                continue
            if up >= since and ("pr_merged", str(pr["number"])) not in have:
                sha = (pr.get("mergeCommit") or {}).get("oid", "") or ""
                pending.append(("pr_merged",
                                [f"pr={pr['number']}", f"repo={repo}", "by=reconcile",
                                 f"merge_sha={sha[:12]}", f"merged_at={pr.get('mergedAt','')}",
                                 f"title={pr['title'][:80]}"]))
        for it in iss:
            try:
                up = parse_gh_ts(it["updatedAt"])
            except Exception:
                continue
            if up >= since and ("issue_closed", str(it["number"])) not in have:
                pending.append(("issue_closed",
                                [f"issue={it['number']}", f"repo={repo}", "closed_by=reconcile",
                                 f"closed_at={it.get('closedAt','')}", "via_pr=", "unfixed=",
                                 f"title={it['title'][:80]}"]))
    for typ, fields in pending:
        ev = build_event(a.hag, "reconcile", "script", typ, fields + ["reconciled=true"])
        if a.dry_run:
            print("[dry-run]", json.dumps(ev))
        else:
            append_event(a.dir, ev)
    marker = build_event(a.hag, "reconcile", "script", "reconcile",
                         [f"new_events={len(pending)}", f"repos={','.join(repos)}",
                          f"since_hours={a.since_hours:g}"])
    if a.dry_run:
        print("[dry-run]", json.dumps(marker))
    else:
        append_event(a.dir, marker)
        tick(a.hag)
    print(f"reconciled {len(pending)} new events across {len(repos)} repos"
          f"{' (dry-run, nothing written)' if a.dry_run else ''} -> {a.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
