#!/usr/bin/env python3
"""Bounded role context (architecture doc §7, "Bounding context"): no model tokens.

Every turn re-reads the session's whole context, so a cold wake costs about the context's size. A
long-lived lane that hands over to a fresh session at a quiet boundary is cheaper on every wake after
it. The handover is the start doc (plan_state): the role refreshes it, marks the boundary with
`queue ... --restart-ready`, and is restarted on it.

    features.health.context_restart_k   300 (default) | 0 (off) | {"*": 300, "<slug>": 800, "<slug>": 0}
    features.health.context_restart     "page" (default: page the operator) | "auto" (the quorum restarts it)

Only Claude sessions report their context today (the transcript's last `usage`); other harnesses read
as unknown and are never advised.
"""
from __future__ import annotations

import json
import os

DEFAULT_K = 300
TAIL_BYTES = 1048576


def _cfg(spec) -> dict:
    h = (getattr(spec, "features", {}) or {}).get("health")
    return h if isinstance(h, dict) else {}


def limit_k(spec, slug: str) -> int:
    """The context size (thousands of tokens) past which `slug` is advised to hand over; 0 is off.
    A dict value names per-role limits, with "*" as the shoal default."""
    v = _cfg(spec).get("context_restart_k", DEFAULT_K)
    if isinstance(v, dict):
        v = v.get(slug, v.get("*", DEFAULT_K))
    try:
        return max(int(v), 0)
    except (TypeError, ValueError):
        return DEFAULT_K


def mode(spec) -> str:
    m = str(_cfg(spec).get("context_restart", "page")).lower()
    return m if m in ("page", "auto") else "page"


def claude_tokens(session: str) -> int | None:
    """The input context of the session's last model call (input + cache read + cache creation), from
    the tail of its newest transcript; None when unknown."""
    from health import claude_transcript
    path = claude_transcript(session)
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(max(os.path.getsize(path) - TAIL_BYTES, 0))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        if '"usage"' not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        u = (d.get("message") or {}).get("usage") if d.get("type") == "assistant" else None
        if isinstance(u, dict):
            return int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
                + int(u.get("cache_creation_input_tokens") or 0)
    return None


def tokens(role) -> int | None:
    """SHOAL_FAKE_CONTEXT (tests only): '{"<slug>": tokens}'."""
    import config
    fake = config.env("FAKE_CONTEXT")
    if fake:
        v = json.loads(fake).get(role.slug)
        return None if v is None else int(v)
    if role.ecosystem == "claude" and role.session:
        return claude_tokens(role.session)
    return None


def over(spec, role) -> tuple[int, int] | None:
    """(context k, limit k) when the role's context is past its limit, else None."""
    lim = limit_k(spec, role.slug)
    if not lim:
        return None
    t = tokens(role)
    if t is None or t < lim * 1000:
        return None
    return t // 1000, lim


def advisory(spec, role) -> str:
    """The line a role reads in `next` and in every wake while its context is past its limit."""
    o = over(spec, role)
    if o is None:
        return ""
    k, lim = o
    who = "the quorum restarts you there" if mode(spec) == "auto" else "the operator restarts you there"
    return (f"CONTEXT {k}k (limit {lim}k): every cold wake re-reads about {k}k tokens. At your next boundary with "
            f"nothing mid-flight, refresh your start doc ({role.doc or 'your plan_state'}) so a fresh session can "
            f"continue from it alone (current state, decisions taken, next steps), then end with `queue --idle "
            f"--restart-ready` (or keep your wait and add `--restart-ready`); {who}. Do not restart yourself.")
