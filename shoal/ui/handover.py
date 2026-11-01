#!/usr/bin/env python3
"""Context handover for standalone sessions (docs/handover.md): the same protocol shoal roles follow, for a session in
the viewer registry that is in no shoal.

    shoal ui handover <label|job> [--doc PATH] [--mode off|advise|auto] [--k N]     # set, and show
    shoal ui handover --list                                                       # every saved session

Opt-in per session (registry fields `doc`, `handover`, `handover_k`). The feeder checks each opted-in session about
every 5 minutes, with no model calls:
  1. **advise**: past its limit and idle at its prompt, the session gets ONE message (a turn): refresh the start doc
     and its `Last updated` stamp, then end the turn.
  2. **verify**: the start doc meets the contract (`shoal doc-check`) and its stamp is at or after the advice.
  3. **reset** (`auto` only): `Backend.reset` starts a new conversation in the same seat whose first message is the
     start doc to resume from, retires the old one, and the registry follows the new session.
`advise` stops after step 1 (the maintainer resets by hand). A stale doc after STALL_H hours is shown as stalled.
Opting in while past the limit starts step 1 at once.
"""
from __future__ import annotations

import datetime
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
for _p in (PKG, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MODES = ("off", "advise", "auto")
DEFAULT_K = 500
STALL_H = 3
ADVISE = ("HANDOVER (context {k}k, limit {lim}k): every cold turn re-reads about {k}k tokens. Refresh your start doc "
          "{doc} now so a fresh session can continue from it alone (current state, decisions taken, next steps) and "
          "update its `Last updated: <UTC>` line; `shoal doc-check {doc}` shows what is missing. Then end your turn "
          "with nothing mid-flight. {after}")
RESUME = ("Resume from your start doc {doc}: read it first, then continue from its current state and next steps. You "
          "are a fresh session after a context handover; nothing else carried over.")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_h(ts: str) -> float:
    try:
        t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600


def find(entries: list[dict], key: str) -> dict | None:
    """A local registry entry by label, job id or session id (prefix)."""
    for e in entries:
        if e.get("host"):
            continue
        if key in (e.get("label"), e.get("job")) or str(e.get("session", "")).startswith(key):
            return e
    return None


def context_k(e: dict) -> int | None:
    """The entry's current context in thousands of tokens (Claude only today), None when unknown."""
    import config
    fake = config.env("FAKE_CONTEXT")  # tests: '{"<label>": tokens}'
    if fake:
        import json as _json
        v = _json.loads(fake).get(e.get("label") or "")
        return None if v is None else int(v) // 1000
    if e.get("harness") != "claude":
        return None
    import context as _ctx
    from backends import claude_current
    t = _ctx.claude_tokens(claude_current(e["session"], e.get("job", "")))
    return None if t is None else t // 1000


def idle_row(e: dict) -> dict | None:
    """The harness row when the session is idle at its prompt with nothing in flight, else None."""
    if e.get("harness") != "claude":
        return None
    import health as _h
    import ui_status
    r = ui_status.claude_session(ui_status.claude_rows(), e["session"], e.get("job", ""))
    if r is None:
        return None
    st = _h.claude_job_state(r)
    fl = st.get("inFlight") or {}
    busy = isinstance(fl, dict) and (fl.get("tasks") or fl.get("kinds"))
    if _h.claude_liveness(r) != "idle" or busy or _h.claude_prompt_open(r, str(r.get("sessionId") or e["session"])):
        return None
    return r


def _pseudo(e: dict):
    """The minimum of a spec and a role the direct-wake delivery reads, for a session in no shoal: deliver directly
    (stop and resume an idle session)."""
    spec = types.SimpleNamespace(features={"health": {"direct_wake": "idle"}}, sid="standalone")
    role = types.SimpleNamespace(tag=e.get("label") or e["session"][:8], slug=e.get("label") or e["session"][:8])
    return spec, role


def follow(e: dict) -> bool:
    """After a delivery the harness may have moved the conversation to another job (a stopped and resumed Claude job
    whose conversation was cleared becomes a separate job, measured 2026-10-22): point the entry at the job that holds
    its current conversation."""
    if e.get("harness") != "claude":
        return False
    import ui_status
    from backends import claude_current, claude_holders
    cur = claude_current(e["session"], e.get("job", ""))
    # the live copy first: a stop+resume can leave the stopped job listed beside the one now holding the conversation
    r = next(iter(claude_holders(ui_status.claude_rows(), cur)), None)
    if r is not None and r.get("id") and r["id"] != e.get("job"):
        e["job"], e["session"] = str(r["id"]), cur
        return True
    return False


def step(e: dict, force: bool = False, dry_run: bool = False) -> str:
    """Advance one opted-in entry by at most one step; returns what happened ('' when nothing). Mutates `e`."""
    mode = e.get("handover") or "off"
    if mode not in ("advise", "auto") or not e.get("doc"):
        return ""
    lim = int(e.get("handover_k") or DEFAULT_K)
    st = e.setdefault("handover_state", {})
    k = context_k(e)
    label = e.get("label") or e["session"][:8]
    if st.get("step") == "reset":
        if k is not None and k < lim:
            e.pop("handover_state", None)  # the new session is under its limit: the handover is complete
            return f"{label}: handover complete ({k}k < {lim}k)"
        return ""
    if not st.get("step"):
        if k is None or k < lim:
            return ""
        if idle_row(e) is None and not force:
            return ""
        after = ("Shoal then hands you over to a fresh session on that doc." if mode == "auto"
                 else "The maintainer resets you on that doc.")
        text = ADVISE.format(k=k, lim=lim, doc=e["doc"], after=after)
        if dry_run:
            return f"{label}: would advise ({k}k past {lim}k)"
        from backends import get as get_backend
        try:
            be = get_backend(e["harness"])
            spec, role = _pseudo(e)
            ch = be.deliver(spec, role, e["session"], text) if hasattr(be, "deliver") else "unsupported"
        except Exception as exc:  # noqa: BLE001
            ch = f"FAILED ({str(exc)[:80]})"
        if ch.startswith(("deferred", "FAILED", "no-adapter", "unsupported")):
            return f"{label}: advice not delivered yet ({ch})"
        follow(e)
        e["handover_state"] = {"step": "advised", "at": now_iso(), "k": k}
        return f"{label}: advised ({k}k past {lim}k) via {ch}"
    if st.get("step") == "advised":
        if k is not None and k < lim:
            e.pop("handover_state", None)  # back under the limit (reset by hand, or compacted): nothing left to do
            return f"{label}: under its limit again ({k}k < {lim}k)"
        if mode != "auto":
            return ""
        import startdoc as _sd
        r = _sd.check(e["doc"], _sd.parse_ts(st.get("at", "")))
        if not r["ok"]:
            if _age_h(st.get("at", "")) >= STALL_H and not st.get("stalled"):
                st["stalled"] = now_iso()
                return f"{label}: handover STALLED: the start doc was not refreshed: " + "; ".join(r["problems"])
            return ""
        if idle_row(e) is None:
            return ""  # still finishing its turn: reset at a quiet moment
        if dry_run:
            return f"{label}: would reset onto {e['doc']}"
        from backends import UnsupportedError, get as get_backend
        try:
            out = get_backend(e["harness"]).reset(e["session"], RESUME.format(doc=e["doc"]), label=label)
        except UnsupportedError as exc:
            st["stalled"] = now_iso()
            return f"{label}: reset unsupported ({exc}); reset it by hand on {e['doc']}"
        except Exception as exc:  # noqa: BLE001
            return f"{label}: reset FAILED ({str(exc)[:120]}); retried later"
        old = e.get("job") or e["session"][:8]
        e["session"], e["job"] = out["session"], out.get("job") or ""
        e["handover_state"] = {"step": "reset", "at": now_iso(), "from": old}
        if out.get("still_live"):
            e["handover_state"]["still_live"] = out["still_live"]  # shown by --list until someone stops them
        return f"{label}: handed over: {out['did']}"
    return ""


def tick(dry_run: bool = False) -> list[str]:
    """One pass over the registry (the feeder calls it): advance every opted-in local entry; save what changed."""
    import ui_herdr
    entries = ui_herdr.load_standalone(raw=True)
    notes = []
    changed = False
    for e in entries:
        if e.get("host") or (e.get("handover") or "off") == "off":
            continue
        before = repr(e)
        try:
            n = step(e, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - one entry never stops the pass
            n = f"{e.get('label')}: handover check failed ({str(exc)[:80]})"
        if n:
            notes.append(n)
        changed = changed or repr(e) != before
    if changed and not dry_run:
        ui_herdr.save_standalone(entries)
    return notes


def cmd_handover(a) -> int:
    import ui_herdr
    entries = ui_herdr.load_standalone(raw=True)
    if a.list or not a.target:
        for x in entries:
            if x.get("host"):
                continue
            k = context_k(x)
            st = x.get("handover_state") or {}
            print(f"{(x.get('label') or x['session'][:12]):<32} {x.get('harness'):<8} handover {x.get('handover') or 'off':<6} "
                  f"limit {int(x.get('handover_k') or DEFAULT_K)}k context {k if k is not None else '?'}k "
                  f"step {st.get('step') or '-'}{' STALLED' if st.get('stalled') else ''}"
                  f"{' OLD JOB STILL LIVE: ' + ', '.join(st['still_live']) if st.get('still_live') else ''}  doc {x.get('doc') or '-'}")
        return 0
    e = find(entries, a.target)
    if e is None:
        print(f"ui handover: no saved local session named {a.target!r} (`shoal ui pick --save` first)")
        return 2
    if a.doc:
        e["doc"] = os.path.abspath(os.path.expanduser(a.doc))
    if a.mode:
        e["handover"] = a.mode
        if a.mode == "off":
            e.pop("handover_state", None)
    if a.k:
        e["handover_k"] = int(a.k)
    if (e.get("handover") or "off") != "off" and not e.get("doc"):
        print("ui handover: a handover needs the session's start doc (--doc PATH)")
        return 2
    ui_herdr.save_standalone(entries)
    k = context_k(e)
    lim = int(e.get("handover_k") or DEFAULT_K)
    print(f"{e.get('label')}: handover {e.get('handover') or 'off'}, limit {lim}k, context {k if k is not None else '?'}k, "
          f"doc {e.get('doc') or '-'}")
    if e.get("doc"):
        import startdoc as _sd
        r = _sd.check(e["doc"])
        print(f"  doc-check: {'ok' if r['ok'] else 'FAIL: ' + '; '.join(r['problems'])}")
    if (e.get("handover") or "off") != "off" and k is not None and k >= lim:
        # opting in above the limit starts the refresh at once (maintainer, 2026-10-22)
        n = step(e, force=True, dry_run=a.dry_run)
        ui_herdr.save_standalone(entries)
        print(f"  {n or 'nothing to do yet'}")
    return 0
