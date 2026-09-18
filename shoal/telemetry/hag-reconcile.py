#!/usr/bin/env python3
"""Nightly GitHub reconciliation for HAG telemetry (stdlib + `gh` CLI only).

Emits issue_closed / pr_opened / pr_merged for recently-updated items in the
participating repos, skipping events already in the log (idempotent by type+number).
Ground truth the lane-reported events are checked against.

Usage: hag-reconcile.py --hag ID [--since-hours N] [--dir LOGDIR]
Cron: 7 4 * * * .../hag-reconcile.py --hag it-mvp-2026-09   (daily, 24h window)
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hag_telemetry import emit as _emit, resolve_dir  # noqa: E402  (single-file reuse, no package)

REPOS = ["speediedan/interpretune", "speediedan/it-interp-engine-adapter"]


class Args:
    def __init__(self, d, hag):
        self.dir = d
        self.hag = hag
        self.session = "reconcile"
        self.ecosystem = "script"
        self.field = []


def seen(logdir, hag):
    have = set()
    dirs = [logdir, os.path.join(logdir, hag)]
    for d in dirs:
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
    r = subprocess.run(["gh"] + list(a), capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []


def emit_ev(logdir, hag, typ, fields):
    args = Args(logdir, hag)
    args.type = typ
    args.field = fields + ["reconciled=true"]
    _emit(args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hag", required=True)
    ap.add_argument("--since-hours", type=float, default=24)
    ap.add_argument("--dir", default=None)
    a = ap.parse_args()
    a.dir = resolve_dir(a.dir, a.hag)
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=a.since_hours)
    have = seen(a.dir, a.hag)
    n = 0
    for repo in REPOS:
        prs = gh("pr", "list", "--repo", repo, "--state", "merged", "--limit", "50",
                 "--json", "number,title,mergedAt,updatedAt")
        for pr in prs:
            try:
                up = datetime.datetime.strptime(pr["updatedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            if up >= since and ("pr_merged", str(pr["number"])) not in have:
                emit_ev(a.dir, a.hag, "pr_merged",
                        [f"pr={pr['number']}", f"repo={repo}", "by=reconcile",
                         f"merge_sha=", f"title={pr['title'][:80]}"])
                n += 1
        iss = gh("issue", "list", "--repo", repo, "--state", "closed", "--limit", "50",
                 "--json", "number,title,closedAt,updatedAt")
        for it in iss:
            try:
                up = datetime.datetime.strptime(it["updatedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            if up >= since and ("issue_closed", str(it["number"])) not in have:
                emit_ev(a.dir, a.hag, "issue_closed",
                        [f"issue={it['number']}", f"repo={repo}", "closed_by=reconcile",
                         "via_pr=", "unfixed=", f"title={it['title'][:80]}"])
                n += 1
    print(f"reconciled {n} new events across {len(REPOS)} repos")


if __name__ == "__main__":
    main()
