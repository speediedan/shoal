#!/usr/bin/env python3
"""Concurrent writers of one route cache entry, as several routed role cards are (selftest_ui.sh).

    route_cache_stress.py <shoal package dir> <scratch cache dir>

Exits non-zero when a writer raised, or when a temp file is left behind or the entry does not parse."""
import json
import multiprocessing
import os
import sys

sys.path.insert(0, sys.argv[1])
os.environ["SHOAL_ROUTE_CACHE"] = sys.argv[2]
import routes  # noqa: E402

WRITERS, WRITES = 8, 300


def writer(_):
    try:
        for i in range(WRITES):
            routes._store("reach-lane", {"ok": True, "i": i})
        return ""
    except Exception as exc:  # noqa: BLE001 - reported by the parent
        return repr(exc)


if __name__ == "__main__":
    with multiprocessing.Pool(WRITERS) as pool:
        errors = [e for e in pool.map(writer, range(WRITERS)) if e]
    if errors:
        sys.exit(f"{len(errors)} of {WRITERS} writers failed: {errors[0]}")
    names = os.listdir(sys.argv[2])
    if names != ["reach-lane.json"]:
        sys.exit(f"left behind: {names}")
    json.load(open(os.path.join(sys.argv[2], "reach-lane.json")))
    print("route cache stress ok")
