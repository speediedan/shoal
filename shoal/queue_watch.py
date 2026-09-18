#!/usr/bin/env python3
"""Consumer QUEUE self-watch (port of hag_queue_watch.sh).

Every INTERVAL s: rehash the QUEUE section; exit (which wakes the owning
session) ONLY when an item id is in neither `consumed` nor `declined` of the
sidecar. The sidecar is re-read every tick, so consumed/declined edits take
effect without a restart. A QUEUE edit that only confirms a decline updates
queue_hash and does not wake. Five consecutive read failures exit 2
(QUERY-FAILED) so a broken instrument is reported rather than silently
sleeping.

Sidecar keys: queue_file, queue_hash, consumed (comma ids), declined (comma ids).
"""
from __future__ import annotations

import datetime
import sys
import time

from queue_section import QueueReadError, read_queue


def load_sidecar(path: str) -> dict[str, str]:
    d: dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                d[k.strip()] = v.strip()
    return d


def save_hash(path: str, h: str) -> None:
    lines = open(path).read().splitlines(keepends=True)
    with open(path, "w") as fh:
        for line in lines:
            fh.write(f"queue_hash={h}\n" if line.startswith("queue_hash=") else line)


def cmd_queue_watch(sidecar: str, interval: float) -> int:
    fails = 0
    while True:
        time.sleep(interval)
        try:
            sc = load_sidecar(sidecar)
            queue_file = sc["queue_file"]
        except Exception:
            print(f"QUEUE-WATCH ERROR: sidecar unreadable at {sidecar}")
            return 3
        try:
            h, ids = read_queue(queue_file)
        except QueueReadError as exc:
            fails += 1
            print(f"QUEUE-WATCH QUERY-FAILED ({fails}/5): {exc}")
            if fails >= 5:
                return 2
            continue
        fails = 0
        prev = sc.get("queue_hash", "")
        if h == prev:
            continue
        save_hash(sidecar, h)
        sc = load_sidecar(sidecar)  # re-read: consumed/declined may have moved
        known = set(filter(None, (sc.get("consumed", "") + ","
                                  + sc.get("declined", "")).split(",")))
        new = [i for i in ids if i not in known]
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%SZ")
        if new:
            print(f"QUEUE-CHANGED NEW-ITEMS at {ts}: [{h} {len(ids)} items] "
                  + " | ".join(new))
            return 0
        print(f"QUEUE-CHANGED no unseen items at {ts}: [{prev} -> {h}, "
              f"{len(ids)} items] (decline confirmation or reorder)")
    return 0  # unreachable


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", required=True)
    ap.add_argument("--interval", required=True, type=float)
    a = ap.parse_args()
    sys.exit(cmd_queue_watch(a.sidecar, a.interval))
