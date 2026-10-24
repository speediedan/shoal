#!/usr/bin/env python3
"""Wait-target providers (docs/providers.md): what clears a role's wait, resolved without the role.

A wait target is `<kind>:<ref>`. Each kind has one resolver in REGISTRY,

    resolver(ref, slug, sid) -> (True pending | False resolved | None unknown, detail)

and `resolve(target, slug, sid)` is the only entry point (health.target_pending is a thin
alias). Unknown is never resolved: a resolver that cannot tell (tool missing, provider
failed, invalid output, nothing reported) answers None, and the health quorum then covers the
wait with its periodic validity check only. A kind with no resolver answers
(None, "no provider for <kind>").

Kinds shipped here:
  job       the name given to `shoal job run --name` (systemd --user job unit)
  lease     a lease key, via the configured lease provider (`lease_cmd --status --json`)
  gh-run    <owner/repo>:<run id>, via `gh run view`
  gh-pr     <owner/repo>#<n>, via `gh pr checks` (an empty check list is unknown)
  az-build  an Azure Pipelines build, `[<project>/]<id>`, via `az pipelines build show` (optional: needs `az`)
"""
from __future__ import annotations

import json
import os
import re
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


def _repo_num(ref: str) -> tuple[str, str] | None:
    """(owner/repo, number) from `owner/repo#n`, `repo#n`, `owner/repo:n` or `repo:n`; a bare
    repo takes the configured default owner. None when unparseable."""
    m = re.fullmatch(r"(?:([\w.-]+)/)?([\w.-]+)[#:](\d+)", (ref or "").strip())
    if not m:
        return None
    owner = m.group(1) or config.default_owner()
    return (f"{owner}/{m.group(2)}", m.group(3)) if owner else None


def normalize(target: str) -> tuple[str, str]:
    """(canonical target, error). Loose GitHub refs become `<kind>:<owner/repo>#<n>`; an unknown
    kind or an unparseable ref is an error, because a wait nobody can check never resolves and
    never escalates (measured 2026-09-26: `gh-pr:interpretune:637`, a PR merged the evening
    before, held a lane in `remote-ci` for a day while every check read "unknown")."""
    if not target:
        return "", ""
    kind, _, ref = target.partition(":")
    if kind not in REGISTRY:
        return target, f"no provider for {kind} (known: {', '.join(kinds())})"
    if kind in ("gh-pr", "gh-issue"):
        rn = _repo_num(ref)
        if rn is None:
            return target, (f"{kind} target {ref!r} is not <owner/repo>#<n> (or <repo>#<n> with a "
                            f"configured default owner)")
        return f"{kind}:{rn[0]}#{rn[1]}", ""
    if kind == "gh-run":
        repo, _, rid = ref.rpartition(":")
        if not repo or not rid.isdigit():
            return target, f"gh-run target {ref!r} is not <owner/repo>:<run id>"
    if kind == "az-build" and not re.fullmatch(r"(?:[\w.-]+/)?\d+", ref):
        return target, f"az-build target {ref!r} is not [<project>/]<build id>"
    return target, ""


def resolve(target: str, slug: str, sid: str = "", upstream: bool = False) -> Result:
    """(True pending | False resolved | None unknown, detail) for a `<kind>:<ref>` wait target.
    `upstream=True` is the semantics of a dependency outside the shoal (an `upstream` wait or
    blocker): a `gh-pr` target is pending while the PR is open, whatever its checks say. The
    default `gh-pr` answer (pending while checks run) is right for a CI wait and wrong for an
    upstream one: an open PR with finished checks read as resolved, so an upstream wait on it was
    refused as "already resolved" and a blocker on it could not be recorded (measured 2026-09-30:
    two upstream PRs awaiting review, one with a failing check)."""
    if not target:
        return None, "no target"
    if target.partition(":")[0] not in REGISTRY:
        return None, f"no provider for {target.partition(':')[0]}"
    target, err = normalize(target)
    if err:
        return None, err
    kind, _, ref = target.partition(":")
    if upstream and kind == "gh-pr":
        try:
            return pr_open(ref)
        except Exception as exc:  # noqa: BLE001 - a provider failure is unknown, never resolved
            return None, f"check failed: {str(exc)[:80]}"
    fn = REGISTRY.get(kind)
    if fn is None:
        return None, f"no provider for {kind}"
    try:
        return fn(ref, slug, sid)
    except Exception as exc:  # noqa: BLE001 - a provider failure is unknown, never resolved
        return None, f"check failed: {str(exc)[:80]}"


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    if argv and argv[0] in config.login_shell_tools():  # its credential lives in the login profile, not the unit
        argv = ["bash", "-lc", shlex.join(argv)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=config.tool_env(),
                          stdin=subprocess.DEVNULL)


@register("job")
def job(ref: str, slug: str, sid: str) -> Result:
    # `job:<name>` is the name given to `job run`; the unit is shoal-<id>-<slug>-<name>.
    # Querying the bare name asked systemd about a unit that never exists, which
    # reads `inactive` and marked every live job wait resolved (2026-09-23, #t401).
    import jobs
    unit = ref if ref.startswith("shoal-") or not sid else jobs.find_unit(sid, slug, ref)
    st = jobs.unit_state(unit)
    active = st.get("ActiveState", "")
    if active in ("active", "activating", "deactivating", "reloading"):
        return True, f"unit {unit} {active}"
    rc_file = jobs.paths(sid, unit)["rc"] if sid else ""
    if rc_file and os.path.exists(rc_file):
        with open(rc_file) as fh:
            return False, f"unit {unit} exited rc={fh.read().strip()}"
    if st.get("LoadState") == "not-found":
        return None, f"unit {unit} not found and no rc file (unknown, not resolved)"
    return False, f"unit {unit} {active or 'gone'}"


@register("lease")
def lease(ref: str, slug: str, sid: str) -> Result:
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
def gh_run(ref: str, slug: str, sid: str) -> Result:
    repo, _, rid = ref.rpartition(":")
    r = _run(["gh", "run", "view", rid, "--repo", repo, "--json", "status,conclusion"])
    d = json.loads(r.stdout or "{}")
    if not d:
        return None, "gh run status unavailable"
    return d.get("status") not in ("completed",), f"run {d.get('status')}/{d.get('conclusion')}"


@register("gh-pr")
def gh_pr(ref: str, slug: str, sid: str) -> Result:
    rn = _repo_num(ref)
    if rn is None:
        return None, f"unparseable gh-pr ref {ref!r}"
    repo, n = rn
    st = json.loads(_run(["gh", "pr", "view", n, "--repo", repo, "--json", "state"]).stdout or "{}").get("state")
    if not st:
        return None, "gh pr state unavailable"
    if st != "OPEN":  # a merged or closed PR has nothing left to wait on
        return False, f"PR {st.lower()}"
    r = _run(["gh", "pr", "checks", n, "--repo", repo, "--json", "bucket"])
    b = [c.get("bucket") for c in json.loads(r.stdout or "[]")]
    if not b:
        return None, "no checks reported (never green on empty)"
    return "pending" in b, f"checks {sorted(set(b))}"


def pr_open(ref: str) -> Result:
    """Pending while the PR is open; resolved once merged or closed (upstream semantics)."""
    rn = _repo_num(ref)
    if rn is None:
        return None, f"unparseable gh-pr ref {ref!r}"
    repo, n = rn
    st = json.loads(_run(["gh", "pr", "view", n, "--repo", repo, "--json", "state"]).stdout or "{}").get("state")
    if not st:
        return None, "gh pr state unavailable"
    return st == "OPEN", f"PR {st.lower()}"


@register("gh-issue")
def gh_issue(ref: str, slug: str, sid: str) -> Result:
    """An upstream or peer issue: pending while open, resolved once closed."""
    rn = _repo_num(ref)
    if rn is None:
        return None, f"unparseable gh-issue ref {ref!r}"
    repo, n = rn
    st = json.loads(_run(["gh", "issue", "view", n, "--repo", repo, "--json", "state"]).stdout or "{}").get("state")
    if not st:
        return None, "gh issue state unavailable"
    return st == "OPEN", f"issue {st.lower()}"


@register("az-build")
def az_build(ref: str, slug: str, sid: str) -> Result:
    """`[<project>/]<id>`: pending while notStarted, inProgress or postponed. A bare id is looked up in each of
    `azure_projects` in turn, because an id from another project reads "not found"."""
    if not shutil.which("az", path=config.tool_env()["PATH"]):
        return None, "az CLI not on the tool PATH"
    proj, _, bid = ref.rpartition("/")
    org = ["--organization", config.azure_org()] if config.azure_org() else []
    missed = []
    for p in ([proj] if proj else config.azure_projects()) or [""]:
        argv = ["az", "pipelines", "build", "show", "--id", bid, *org, *(["--project", p] if p else []),
                "--query", "status", "-o", "tsv"]
        try:
            r = _run(argv, timeout=60)
        except subprocess.TimeoutExpired:
            # az with no Azure DevOps credential waits rather than failing (2026-10-24: 90 s and no output)
            return None, ("az timed out after 60 s: no Azure DevOps credential in the tool environment? "
                          "(a profile-only token needs login_shell_tools = [\"az\"])")
        st = r.stdout.strip()
        if st:
            return st in ("notStarted", "inProgress", "postponed"), f"build {st}" + (f" ({p})" if p and not proj else "")
        if "could not be found" not in r.stderr:
            return None, f"az build status unavailable ({(r.stderr.strip().splitlines() or ['no output'])[-1][:80]})"
        missed.append(p or "default project")
    return None, f"build {bid} not found in {', '.join(missed)} (set azure_projects, or write <project>/<id>)"
