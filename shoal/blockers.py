"""Verified next-item blockers (spec feature `blockers`; architecture doc §4).

A next item may sit behind a recorded blocker (`queue --blocked ITEM=<blocker>`), and a role may
sleep idle only when every next item has one. Without this module a blocker was free text checked
once and carried forward for as long as the item stayed in `next`, so a role could idle for days
behind a hold that had cleared or never applied (measured 2026-09-29: two lanes idle 2.5 h and
11.5 h behind blockers that were all stale: a peer dependency its owner had declared clear, a design
question already answered, and holds citing an offline mode and a lease that had both ended).

Each blocker now carries metadata in the queue event (`next_blocked_meta`: `since`, the last time the
role recorded it; `first`, when the item first became blocked; `n`, how many times it was re-recorded)
and is checked by kind:

- `<role>:<repo#n>`: resolved once the named item is done; expires after `peer_ttl_min`.
- `upstream:<target>`, `lease:<key>`: resolved when the provider says the target is no longer
  pending (docs/providers.md); expires after `peer_ttl_min`.
- `ask:..` / `user:..`: live only while the queue records an ask (`queue --ask`); a maintainer-shaped
  blocker with no raised ask is a hold nobody was asked about.
- `hold:<why>`: unverifiable, so it expires after `hold_ttl_min` and must be re-verified; one kept
  past `hold_page_min` pages the maintainer.

A stale blocker (resolved or expired) no longer counts toward the idle gate or the health quorum's
parked-work check, and `next` names it. Re-recording it is the re-verification. A role or provider
blocker that cleared (`cleared`) also wakes the role once (blocker-resolved in the health quorum).
"""
from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {"hold_ttl_min": 240.0, "peer_ttl_min": 720.0, "hold_page_min": 720.0}
ASK_KINDS = ("ask", "user")


def cfg(spec) -> dict | None:
    """The feature's settings (defaults filled in), or None when the feature is off."""
    c = (getattr(spec, "features", None) or {}).get("blockers")
    if not c:
        return None
    out = dict(DEFAULTS)
    if isinstance(c, dict):
        out.update({k: float(v) for k, v in c.items() if k in DEFAULTS})
    return out


def kind(blocker: str) -> str:
    """role | ask | upstream | lease | hold (anything unprefixed reads as a hold)."""
    head = (blocker or "").split(":", 1)[0]
    if head in ASK_KINDS:
        return "ask"
    if head in ("upstream", "lease", "hold"):
        return head
    return "role" if ":" in (blocker or "") else "hold"


def takeable(spec, view, blocker: str) -> str:
    """"" when the role a `<role>:<repo#n>` blocker names can take that item, else why not.

    A `fill=assigned` or `fill=none` role works only items offered or handed to it, so blocking on
    one that holds no offer, handoff or claim for the item records a wait nothing can end. Measured
    2026-10-08 (FM-4, third occurrence): a lane told by the maintainer to delegate seven items to an
    assigned reviewer sent the reviewer mail, marked its asks answered and blocked three items on
    it; mail neither wakes the reviewer nor lets it claim, and nothing moved until the operator
    offered the items. A `fill=self` role can claim board items itself, so it is not checked."""
    from ledger import item_key
    who, _, item = (blocker or "").partition(":")
    r = spec.role(who) if who else None
    if r is None or not item or getattr(r, "fill", "") == "self":
        return ""
    k = item_key(item)
    if any(c.get("_live") and c.get("by") == who for c in view.claims.get(k, [])):
        return ""
    if any(o.get("status") in ("open", "claimed") and o.get("to") in (who, "*") for o in view.by_item.get(k, [])):
        return ""
    return (f"{who} is fill={r.fill}: it works only items offered or handed to it, and it holds no offer, "
            f"handoff or claim for {k}, so nothing would ever end this wait. If you hold {k}, hand it over "
            f"(`shoal release --item {k} --kind handoff --to {who}`; claim it first if you have not); if it "
            f"is the maintainer's to give, record an ask for a user offer (`--blocked {k}=ask:..` with "
            f"`--ask`). Block on the item {who} actually holds when that differs (a PR rather than its issue). "
            f"Mail to {who} does neither: a peer msg does not wake it and gives it nothing to claim")


def _now() -> datetime.datetime:
    from health import utcnow  # the shared clock (SHOAL_HEALTH_SHIFT_MIN moves it in tests)
    return utcnow()


def _age_min(ts: str) -> float | None:
    try:
        t = datetime.datetime.strptime((ts or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return (_now() - t).total_seconds() / 60


def provider_target(blocker: str) -> str:
    """The provider target a checkable blocker names ('' when none): `lease:<key>` as is,
    `upstream:<kind>:<ref>` as `<kind>:<ref>`, and a loose `upstream:<repo#n>` as a gh-issue."""
    k = kind(blocker)
    if k == "lease":
        return blocker
    if k == "upstream":
        rest = blocker.split(":", 1)[1] if ":" in blocker else ""
        import providers
        return rest if rest.split(":", 1)[0] in providers.kinds() else (f"gh-issue:{rest}" if rest else "")
    return ""


def check(spec, view, slug: str, q: dict, item_k: str, blocker: str, meta: dict | None,
          live: bool = True) -> tuple[str, str]:
    """(live | resolved | expired | unknown, why) for one recorded blocker. `live=False` skips
    provider calls (watchers), so a checkable blocker is judged by its age alone."""
    c = cfg(spec) or DEFAULTS
    k = kind(blocker)
    since = (meta or {}).get("since") or (q or {}).get("ts", "")
    age = _age_min(since)
    if k == "ask":
        if (q or {}).get("asks"):
            return "live", "ask recorded"
        return "resolved", "no ask recorded: raise it (`queue --ask`) or the item is free"
    if k == "role":
        from ledger import item_key
        other = blocker.split(":", 1)[1] if ":" in blocker else ""
        if other and item_key(other) in view.done:
            d = view.done[item_key(other)]
            return "resolved", f"{item_key(other)} done by {d['by']} {d['ts']}"
        # a carried-forward blocker naming a role that cannot take the item parks nothing: `queue`
        # refuses it when recorded, and one carried forward is stale, so the writer is woken
        # (parked-work), not the named role
        why_t = takeable(spec, view, blocker)
        if why_t:
            return "expired", f"untakeable: {why_t.split(', so nothing', 1)[0]}"
        ttl = c["peer_ttl_min"]
    elif k in ("upstream", "lease"):
        target = provider_target(blocker)
        if live and target:
            import providers
            pending, why = providers.resolve(target, slug, getattr(spec, "sid", ""), upstream=(k == "upstream"))
            if pending is False:
                return "resolved", why
        ttl = c["peer_ttl_min"]
    else:
        ttl = c["hold_ttl_min"]
    if age is not None and age >= ttl:
        return "expired", f"recorded {since} ({age:.0f}m ago, re-verify every {ttl:.0f}m)"
    return "live", ""


def stale(spec, view, slug: str, q: dict | None = None, live: bool = True) -> dict[str, str]:
    """{item key: why} for the role's recorded next-item blockers that no longer hold. Empty when
    the feature is off."""
    if cfg(spec) is None:
        return {}
    q = q if q is not None else (view.current_queue(slug) or {})
    meta = q.get("next_blocked_meta") or {}
    out = {}
    for k, b in (q.get("next_blocked") or {}).items():
        st, why = check(spec, view, slug, q, k, b, meta.get(k), live=live)
        if st in ("resolved", "expired"):
            out[k] = f"{st}: {why}"
    return out


def cleared(stale_nb: dict[str, str], q: dict) -> dict[str, str]:
    """The part of `stale` that is news: {item key: why} for role and provider blockers whose target really cleared.
    An expired blocker only needs re-checking, and an `ask:` with no ask recorded is the maintainer's or the role's own
    doing, so neither says that anything changed."""
    nb = q.get("next_blocked") or {}
    return {k: why for k, why in stale_nb.items()
            if why.startswith("resolved:") and kind(nb.get(k, "")) in ("role", "upstream", "lease")}


def long_holds(spec, q: dict | None) -> list[tuple[str, str, str, float, int]]:
    """[(item key, blocker, first, age min, renewals)] for holds kept past `hold_page_min`."""
    c = cfg(spec)
    if c is None or not q:
        return []
    meta = q.get("next_blocked_meta") or {}
    out = []
    for k, b in (q.get("next_blocked") or {}).items():
        if kind(b) != "hold":
            continue
        m = meta.get(k) or {}
        age = _age_min(m.get("first", ""))
        if age is not None and age >= c["hold_page_min"]:
            out.append((k, b, m["first"], age, int(m.get("n", 0))))
    return out


def next_meta(prev_q: dict, nb: dict[str, str], recorded: set[str], now: str) -> dict[str, dict]:
    """The metadata for the blockers a queue write carries: a blocker recorded in this write is
    re-verified now (`since`), keeps its `first` while the item stays blocked, and counts a
    renewal when it replaces an earlier one; a carried blocker keeps its metadata."""
    prev_nb = prev_q.get("next_blocked") or {}
    prev_meta = prev_q.get("next_blocked_meta") or {}
    out = {}
    for k in nb:
        pm = prev_meta.get(k) or {}
        if k in recorded:
            was = k in prev_nb
            out[k] = {"since": now, "first": (pm.get("first") or prev_q.get("ts") or now) if was else now,
                      "n": int(pm.get("n", 0)) + 1 if was else 0}
        else:
            out[k] = {"since": pm.get("since") or now, "first": pm.get("first") or pm.get("since") or now,
                      "n": int(pm.get("n", 0))}
    return out
