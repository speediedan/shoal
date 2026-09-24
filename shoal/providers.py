#!/usr/bin/env python3
"""Wait-target providers (docs/providers.md): what clears a role's wait, resolved without the role.

A wait target is `<kind>:<ref>`. Each kind has one resolver in REGISTRY,

    resolver(ref, slug, hag) -> (True pending | False resolved | None unknown, detail)

and `resolve(target, slug, hag)` is the only entry point (health.target_pending is a thin
alias). Unknown is never resolved: a resolver that cannot tell (tool missing, provider
failed, invalid output, nothing reported) answers None, and the health quorum then covers the
wait with its periodic validity check only. A kind with no resolver answers
(None, "no provider for <kind>").

Kinds shipped here:
  job       the name given to `shoal job run --name` (systemd --user job unit)
  lease     a lease key, via the configured lease provider (`lease_cmd --status --json`)
  gh-run    <owner/repo>:<run id>, via `gh run view`
  gh-pr     <owner/repo>#<n>, via `gh pr checks` (an empty check list is unknown)
  az-build  an Azure Pipelines build id, via `az pipelines build show` (optional: needs `az`)
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

Result = tuple  # (bool | None, str)
REGISTRY: dict[str, Callable[[str, str, str], Result]] = {}


def register(kind: str):
    def deco(fn):
        REGISTRY[kind] = fn
        return fn
    return deco


def kinds() -> tuple[str, ...]:
    return tuple(REGISTRY)


def resolve(target: str, slug: str, hag: str = "") -> Result:
    """(True pending | False resolved | None unknown, detail) for a `<kind>:<ref>` wait target."""
    if not target:
        return None, "no target"
    kind, _, ref = target.partition(":")
    fn = REGISTRY.get(kind)
    if fn is None:
        return None, f"no provider for {kind}"
    try:
        return fn(ref, slug, hag)
    except Exception as exc:  # noqa: BLE001 - a provider failure is unknown, never resolved
        return None, f"check failed: {str(exc)[:80]}"


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=config.tool_env())


@register("job")
def job(ref: str, slug: str, hag: str) -> Result:
    # `job:<name>` is the name given to `job run`; the unit is shoal-<hag>-<slug>-<name>
    # (hag-<hag>-<slug>-<name> for a job started before the rename).
    # Querying the bare name asked systemd about a unit that never exists, which
    # reads `inactive` and marked every live job wait resolved (2026-09-23, #t401).
    import jobs
    unit = ref if ref.startswith(("shoal-", "hag-")) or not hag else jobs.find_unit(hag, slug, ref)
    st = jobs.unit_state(unit)
    active = st.get("ActiveState", "")
    if active in ("active", "activating", "deactivating", "reloading"):
        return True, f"unit {unit} {active}"
    rc_file = jobs.paths(hag, unit)["rc"] if hag else ""
    if rc_file and os.path.exists(rc_file):
        with open(rc_file) as fh:
            return False, f"unit {unit} exited rc={fh.read().strip()}"
    if st.get("LoadState") == "not-found":
        return None, f"unit {unit} not found and no rc file (unknown, not resolved)"
    return False, f"unit {unit} {active or 'gone'}"


@register("lease")
def lease(ref: str, slug: str, hag: str) -> Result:
    """Pending while the lease named by the ref is held or has a queue entry."""
    cmd = config.lease_cmd()
    if not cmd:
        return None, "no lease provider configured (lease_cmd)"
    key = ref.strip()
    if not key:
        return None, "lease target names no key"
    try:
        r = _run(shlex.split(cmd) + ["--status", "--json"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"lease provider failed: {str(exc)[:80]}"
    if r.returncode != 0:
        return None, f"lease provider exited {r.returncode}"
    try:
        doc = json.loads(r.stdout)
        leases = doc["leases"]
        queue = doc.get("queue") or []
        if not isinstance(leases, list) or not isinstance(queue, list):
            raise ValueError("leases/queue not lists")
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"lease provider printed invalid JSON ({str(exc)[:60]})"
    held = next((x for x in leases if isinstance(x, dict) and x.get("key") == key), None)
    waiters = [q for q in queue if isinstance(q, dict) and q.get("key") == key]
    if held is None and not waiters:
        return None, f"lease {key} not reported by the provider"
    if held is not None and held.get("held"):
        return True, (f"lease {key} held by {held.get('holder') or '?'} since {held.get('since') or '?'}"
                      + (f"; {len(waiters)} queued" if waiters else ""))
    if waiters:
        return True, f"lease {key} free, {len(waiters)} queued (first: {waiters[0].get('waiter', '?')})"
    return False, f"lease {key} free, none queued"


@register("gh-run")
def gh_run(ref: str, slug: str, hag: str) -> Result:
    repo, _, rid = ref.rpartition(":")
    r = _run(["gh", "run", "view", rid, "--repo", repo, "--json", "status,conclusion"])
    d = json.loads(r.stdout or "{}")
    if not d:
        return None, "gh run status unavailable"
    return d.get("status") not in ("completed",), f"run {d.get('status')}/{d.get('conclusion')}"


@register("gh-pr")
def gh_pr(ref: str, slug: str, hag: str) -> Result:
    repo, _, n = ref.partition("#")
    r = _run(["gh", "pr", "checks", n, "--repo", repo, "--json", "bucket"])
    b = [c.get("bucket") for c in json.loads(r.stdout or "[]")]
    if not b:
        return None, "no checks reported (never green on empty)"
    return "pending" in b, f"checks {sorted(set(b))}"


@register("az-build")
def az_build(ref: str, slug: str, hag: str) -> Result:
    if not shutil.which("az", path=config.tool_env()["PATH"]):
        return None, "az CLI not on the tool PATH"
    r = _run(["az", "pipelines", "build", "show", "--id", ref, "--query", "status", "-o", "tsv"], timeout=60)
    st = r.stdout.strip()
    if not st:
        return None, "az build status unavailable"
    return st in ("notStarted", "inProgress", "postponed"), f"build {st}"
