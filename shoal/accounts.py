#!/usr/bin/env python3
"""Provider accounts: several ways to run one provider's CLI on a host.

An account is the provider's CLI with an environment overlay, nothing more. Every provider has an implicit default
account (named after the provider, recorded as "") whose overlay is empty, so a host that declares no account behaves
exactly as before. Extra accounts are declared per host in the host config:

  [accounts.claude-2]
  provider = "claude"
  env = { CLAUDE_CONFIG_DIR = "~/.claude-2" }
  badge = "2"          # optional: the mark on its icon (default: the name's trailing digits, else its index)

SHOAL never inspects, syncs or validates what sits behind an account (whether two accounts share settings is the
operator's business), and it does not enforce a provider's limits: a launch on an account that is not signed in fails
with the provider's own message, like any launch failure.

What SHOAL does need is where a provider keeps an account's state, to read jobs, transcripts and the daemon roster:
`HOME_VARS` names the variable and its default per provider. A session lives in exactly one account, so a reader finds
it by looking in each account's directory (`job_account`), and only a creation needs the account chosen for it.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

# provider -> (the variable holding an account's state directory, its default when unset)
HOME_VARS = {"claude": ("CLAUDE_CONFIG_DIR", "~/.claude"), "codex": ("CODEX_HOME", "~/.codex")}
ROW_KEY = "_account"  # the account a listed Claude row came from (absent for the default account)


def declared() -> dict[str, dict]:
    """{name: {"provider", "env", "badge"}} from `[accounts.<name>]` in the host config; {} when none. Scalar keys of
    `[accounts]` (its settings, such as `select`) are not accounts."""
    t = config.file_values().get("accounts")
    if not isinstance(t, dict):
        return {}
    out = {}
    for name, v in t.items():
        if isinstance(v, dict) and v.get("provider"):
            env = v.get("env") if isinstance(v.get("env"), dict) else {}
            out[str(name)] = {"provider": str(v["provider"]),
                              "env": {str(k): os.path.expanduser(str(x)) for k, x in env.items() if x not in (None, "")},
                              "badge": str(v.get("badge") or "")}
    return out


def setting(key: str, default: str = "") -> str:
    """A scalar under `[accounts]` (`select`)."""
    t = config.file_values().get("accounts")
    v = t.get(key) if isinstance(t, dict) else None
    return str(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else default


def of(provider: str) -> list[str]:
    """The provider's accounts on this host, the default ("") first."""
    return [""] + sorted(n for n, v in declared().items() if v["provider"] == provider)


def several(provider: str) -> bool:
    return len(of(provider)) > 1


def name(provider: str, account: str = "") -> str:
    """How an account is shown: its name, or the provider's for the default."""
    return account or provider


def normalize(provider: str, account: str) -> str:
    """The account to record: "" for the provider's default (named or empty), else a declared account of that
    provider; `auto` is the one the selection policy picks now (`choose`). Raises ValueError for an account this host
    does not declare, or one of another provider."""
    account = (account or "").strip()
    if account == "auto":
        return choose(provider)[0]
    if account in ("", provider, "default"):
        return ""
    d = declared().get(account)
    if d is None:
        have = ", ".join(name(provider, a) for a in of(provider))
        raise ValueError(f"no account {account!r} is declared on this host (`[accounts.{account}]` in "
                         f"{config.config_path()}); {provider} has: {have}")
    if d["provider"] != provider:
        raise ValueError(f"account {account!r} is a {d['provider']} account, not {provider}")
    return account


def provider_of(account: str) -> str:
    return (declared().get(account) or {}).get("provider", "")


def badge(provider: str, account: str = "") -> str:
    """The mark drawn on an account's icon: "" for the default, else its declared badge, its name's trailing digits,
    else its position among the provider's accounts (the default is 1)."""
    if not account:
        return ""
    d = declared().get(account) or {}
    if d.get("badge"):
        return d["badge"]
    m = re.search(r"(\d+)$", account)
    if m:
        return m.group(1)
    accts = of(provider)
    return str(accts.index(account) + 1) if account in accts else "?"


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))
    except (OSError, ValueError):
        return a == b


def env(provider: str, account: str = "", base: dict | None = None) -> dict:
    """The environment a CLI call for this account runs with: `config.tool_env(base)` with the account's overlay. The
    default account of a provider that has others drops any variable naming one of them, so a command run from inside
    another account's session (its CLAUDE_CONFIG_DIR exported) still reaches the default account's sessions."""
    return apply(provider, account, config.tool_env(base))


def apply(provider: str, account: str, e: dict) -> dict:
    """`e` (changed in place and returned) with an account's overlay; for the default account, without the variables
    of the provider's other accounts that name theirs. `e` unchanged when the provider has no declared account."""
    if not several(provider):
        return e
    decl = declared()
    if account:
        e.update((decl.get(account) or {}).get("env") or {})
        return e
    for a in of(provider)[1:]:
        for k, v in (decl[a].get("env") or {}).items():
            if e.get(k) and _same_path(e[k], v):
                e.pop(k)
    return e


def home(provider: str, account: str = "", e: dict | None = None) -> str:
    """The account's state directory for a provider SHOAL reads state from (HOME_VARS), "" for other providers."""
    if provider not in HOME_VARS:
        return ""
    var, dflt = HOME_VARS[provider]
    e = env(provider, account) if e is None else e
    return e.get(var) or os.path.expanduser(dflt)


def homes(provider: str) -> list[tuple[str, str]]:
    """[(account, state directory)] for every account of the provider, the default first; two accounts that resolve to
    one directory are listed once (the first)."""
    out, seen = [], set()
    for a in of(provider):
        h = home(provider, a)
        k = os.path.realpath(h) if h else ""
        if h and k not in seen:
            seen.add(k)
            out.append((a, h))
    return out


def find_job(provider: str, job: str, sub: str = "jobs") -> str | None:
    """The account whose state directory holds `<home>/<sub>/<job>` ("" the default's), None when none does."""
    if not job:
        return None
    for a, h in homes(provider):
        if os.path.exists(os.path.join(h, sub, job)):
            return a
    return None


def job_account(provider: str, job: str, sub: str = "jobs") -> str:
    """The account whose state directory holds `<home>/<sub>/<job>` ("" when it is the default's, or none does)."""
    if not job or not several(provider):
        return ""
    return find_job(provider, job, sub) or ""


def env_account(provider: str, e: dict | None = None) -> str:
    """The account an environment selects (the current process's by default): the declared account whose overlay it
    carries, else ""."""
    e = os.environ if e is None else e
    for a in of(provider)[1:]:
        ov = (declared()[a].get("env") or {})
        if ov and all(e.get(k) and _same_path(e[k], v) for k, v in ov.items()):
            return a
    return ""


def describe() -> list[dict]:
    """Every account on this host, per provider: [{provider, account, name, home, exists, badge}] (the default of each
    provider that has a declared account, then the declared ones)."""
    out = []
    provs = sorted({v["provider"] for v in declared().values()})
    for p in provs:
        for a in of(p):
            h = home(p, a)
            out.append({"provider": p, "account": a, "name": name(p, a), "home": h,
                        "exists": bool(h) and os.path.isdir(h), "badge": badge(p, a)})
    return out


# the provider's own sign-in report, where the harness has one (relayed, never enforced)
SIGNIN = {"claude": ["claude", "auth", "status"], "codex": ["codex", "login", "status"]}


def signin(provider: str, account: str = "") -> str:
    """What the provider's CLI says about an account's sign-in, in one line ("" when the provider has no such report)."""
    import json
    import shutil
    import subprocess
    argv = SIGNIN.get(provider)
    if not argv:
        return ""
    e = env(provider, account)
    exe = shutil.which(argv[0], path=e.get("PATH", ""))
    if not exe:
        return f"{argv[0]} is not on this host's tool path"
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=30, env=e)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"no answer ({exc.__class__.__name__})"
    out = (r.stdout + r.stderr).strip()
    try:
        d = json.loads(r.stdout)
        if isinstance(d, dict) and "loggedIn" in d:
            return (f"signed in as {d.get('email') or '?'} ({d.get('subscriptionType') or d.get('authMethod') or '?'})"
                    if d.get("loggedIn") else "not signed in")
    except ValueError:
        pass
    return (out.splitlines() or [f"rc={r.returncode}"])[-1][:160]


# ---- keeping a sign-in fresh ---------------------------------------------------------------------------------------
# Claude's sign-in is a short access token (about 8 h) and a long refresh token. Only the CLI refreshes the access token,
# and only when it runs; the usage meter reads the stored token as it is. An account no session runs on (every role moved
# to another account, or a laptop that only views) then reads "HTTP 401: login rejected" within hours although its sign-in
# is fine (2026-11-17: the default account after the claude-2 reseat, and both of a laptop's; a one-line headless call
# brought each back with no login). The keepalive is that call, made by the CLI itself, so the refresh follows the CLI's
# own handling of a token several processes share; refreshing from here would race it and could sign the account out.
KEEPALIVE_ARGV = ["-p", "Reply with exactly: OK", "--model", "claude-haiku-5-5", "--no-session-persistence",
                  "--output-format", "text"]
# the CLI refreshes only a token about to expire (measured 2026-11-17: with 2.5 h left a call used it as it was; expired
# two days, a call refreshed it), so the keepalive acts then: expired, or within minutes. The meter polls every 5 min.
KEEPALIVE_MIN_H = 0.1


def token_hours(provider: str, account: str = "") -> float | None:
    """Hours until an account's stored access token expires (negative: expired); None where it keeps none in its state
    directory (another provider, a keychain, never signed in)."""
    import json
    import time
    if provider != "claude":
        return None
    try:
        with open(os.path.join(home(provider, account), ".credentials.json")) as fh:
            exp = ((json.load(fh) or {}).get("claudeAiOauth") or {}).get("expiresAt")
        return (float(exp) / 1000 - time.time()) / 3600 if exp else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def keepalive_on() -> bool:
    """`[accounts] keepalive = true`: let the usage meter refresh a Claude account's sign-in when it is about to lapse
    (opt-in: it costs one tiny model call per account about every 8 hours)."""
    t = config.file_values().get("accounts")
    return isinstance(t, dict) and t.get("keepalive") is True


def rebalance_on() -> bool:
    """`[accounts] rebalance = true`: the health quorum's automatic restart of a role (past its context limit, at a quiet
    boundary) starts the new session on the account the selection policy picks now, within the role's provider, rather
    than the one it had. Every other restart and handoff keeps the account unless one is named."""
    t = config.file_values().get("accounts")
    return isinstance(t, dict) and t.get("rebalance") is True


def quota_handoff_on() -> bool:
    """`[accounts] quota_handoff = true`: the health quorum hands a role at a quiet boundary to another account or
    provider when its own nears a 5 h or weekly limit, or carries more than its share (`quota_pick`)."""
    t = config.file_values().get("accounts")
    return isinstance(t, dict) and t.get("quota_handoff") is True


def pinned(slug: str) -> bool:
    """`[accounts] pin`: roles balancing keeps on their own provider (it may still change their account)."""
    t = config.file_values().get("accounts")
    v = t.get("pin") if isinstance(t, dict) else None
    return isinstance(v, list) and slug in [str(x) for x in v]


QUOTA_COOLDOWN_MIN = 60  # a role moved for quota is not moved again sooner (`[accounts] quota_cooldown_min`)


def quota_cooldown_min() -> float:
    try:
        return float(setting("quota_cooldown_min", "") or QUOTA_COOLDOWN_MIN)
    except ValueError:
        return float(QUOTA_COOLDOWN_MIN)


def keepalive(provider: str, account: str = "", min_hours: float = KEEPALIVE_MIN_H) -> str:
    """Refresh an account's sign-in through its CLI when its token has under `min_hours` left: one headless call on the
    smallest model, saving no session. Says what it found and did."""
    import shutil
    import subprocess
    import tempfile
    if provider != "claude":
        return "no keepalive for this provider"
    before = token_hours(provider, account)
    if before is None:
        return "no stored sign-in to keep"
    if before >= min_hours:
        return f"fresh ({before:.1f} h left)"
    e = env(provider, account)
    exe = shutil.which("claude", path=e.get("PATH", ""))
    if not exe:
        return "claude is not on this host's tool path"
    try:
        with tempfile.TemporaryDirectory(prefix="shoal-keepalive-") as d:
            r = subprocess.run([exe, *KEEPALIVE_ARGV], capture_output=True, text=True, timeout=120, env=e, cwd=d,
                               stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"keepalive failed ({exc.__class__.__name__})"
    after = token_hours(provider, account)
    if after is not None and after > max(before, 0) + 0.01:
        return f"refreshed: {before:.1f} -> {after:.1f} h"
    said = (r.stdout + r.stderr).strip().splitlines()
    if r.returncode == 0 and after is not None and after > 0:
        return f"still valid ({after:.1f} h left): the CLI refreshes only a token about to expire"
    return f"not refreshed ({said[-1][:160] if said else f'rc={r.returncode}'}): sign in again"


# ---- quota balance -------------------------------------------------------------------------------------------------
# Spend down (the maintainer, 2026-11-20): as a window nears its limit, use what is left rather than avoid it, and leave
# only at the reserve, a share small enough to restart on another account (`[accounts] reserve_pct`, default 2). An
# account's headroom in one window is its free share above the reserve over the share of the window still to run (at
# least an hour of it, at most all of it): the rate it would have to be used at for that quota not to expire unused at
# the reset. So 30% free with 10 h of a week left (4.7) beats 60% free with 6 days left (0.68): the first loses its
# quota soonest. Its tightest window is its score, so a nearly spent week binds however fresh the 5 h window is. A
# window not started yet (no reset time) is a whole unused one (1 less the reserve). An account with a window at or
# within the reserve, or a reached limit, scores 0 (no new session); one with no fresh reading has no score and is
# ranked last, never chosen. A window that resets within RESET_GRACE_MIN counts as the fresh window it is about to
# become, so a 5 h window close to its reset neither sheds roles nor holds a score down. Until 2026-11-20 the score was
# paced (the free share left at the reset at the rate used so far) and an account "on course to run out" scored 0:
# early heavy use of a 5 h window then made an account ineligible, and on 2026-11-19 both Claude accounts read "no
# room" for hours while claude-2 held 18% of a week that reset 15.8 h later.
RESERVE_PCT = 2.0
RESET_GRACE_MIN = 30
LONG_WINDOW_MIN = 1440  # roles already placed are shared by their windows of a day or more (`headroom(held=True)`)
SELECT = ("balance", "default")


def _num_setting(key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(setting(key, "") or default)
    except ValueError:
        v = default
    return min(max(v, lo), hi)


def reserve_pct() -> float:
    """`[accounts] reserve_pct`: the percent of a window kept back so a role can restart on another account (default
    2). An account with a window at or within it takes no new session, and its roles move off (`near_limit`)."""
    return _num_setting("reserve_pct", RESERVE_PCT, 0.0, 50.0)


def reset_grace_min() -> float:
    """`[accounts] reset_grace_min`: a window that resets within this many minutes counts as the fresh window it is
    about to become for the roles already on it: they are not moved off it, and it does not hold its score down
    (default 30). A new session still avoids an account whose window is within the reserve now."""
    return _num_setting("reset_grace_min", RESET_GRACE_MIN, 0.0, 300.0)


def _windows(usage_row: dict, now: float):
    """Each window of a usage reading: (name, minutes, percent used, free share, seconds to its reset or None)."""
    for r in usage_row.get("rows") or []:
        for w in r.get("windows") or []:
            try:
                used, minutes = float(w["used_percent"]), float(w.get("minutes") or 0)
                left = None if w.get("resets_at") is None else max(float(w["resets_at"]) - now, 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            yield str(w.get("name") or "?"), minutes, used, max(0.0, 1 - used / 100), left


def headroom(usage_row: dict, now: float, held: bool = False) -> tuple[float | None, str]:
    """(score, its binding window) of one usage reading (subscription_usage.snapshot's row): None when it has no fresh
    reading. `held`: the score that shares out roles already running (`shares`): over the windows of a day or more
    (every window when it reports none), and a window inside its reset grace counts fresh even within the reserve, since
    its roles ride it out. Without it, the score a new session is placed by: every window binds."""
    if not usage_row or usage_row.get("stale") or usage_row.get("error"):
        return None, "no fresh reading"
    for r in usage_row.get("rows") or []:
        if r.get("blocked"):
            return 0.0, f"{r.get('bucket') or '?'}: limit reached"
    res, grace = reserve_pct() / 100, reset_grace_min() * 60
    ws = [w for w in _windows(usage_row, now) if w[1] > 0]
    if held and any(w[1] >= LONG_WINDOW_MIN for w in ws):
        ws = [w for w in ws if w[1] >= LONG_WINDOW_MIN]
    best: tuple[float, str] | None = None
    for wname, minutes, used, free, left in ws:
        span = minutes * 60
        if left is None:  # not started yet: a whole unused window
            if used > 0:
                continue
            h, label = 1 - res, f"{wname} not started"
        else:
            h = max(free - res, 0.0) / max(min(left / span, 1.0), 3600 / span)
            label = f"{wname} {used:g}% used, resets in {_dur(left)}"
            if left <= grace and (held or free > res or left <= 0):
                h, label = max(h, 1 - res), label + " (counted fresh)"
            elif free <= res:
                h, label = 0.0, label + f", within the {reserve_pct():g}% reserve"
        if best is None or h < best[0]:
            best = (h, label)
    return best if best is not None else (None, "no window reported")


def _dur(s: float) -> str:
    return f"{s / 86400:.1f} d" if s >= 86400 else f"{s / 3600:.1f} h" if s >= 3600 else f"{int(s / 60)} min"


def rank(provider: str, now: float | None = None, snap: list | None = None) -> list[dict]:
    """The provider's accounts on this host, best first: [{account, name, score, binding, units, plan, weighted, held,
    near}] from the cached usage readings (no read here: `shoal ui usage --refresh` refreshes them); an account with no
    fresh reading last. `weighted` is the score times units over BASE_UNITS, `held` that of `headroom(held=True)`."""
    import time
    import subscription_usage
    now = time.time() if now is None else now
    keys = [usage_key(provider, a) for a in of(provider)]
    rows = {r.get("provider"): r for r in (subscription_usage.snapshot(keys, now=now)["accounts"] if snap is None else snap)}
    out = []
    for a in of(provider):
        row = rows.get(usage_key(provider, a)) or {}
        score, why = headroom(row, now)
        held = headroom(row, now, held=True)[0]
        units, plan = capacity(provider, a)
        out.append({"account": a, "name": name(provider, a), "score": score, "binding": why, "units": units,
                    "plan": plan, "weighted": None if score is None else score * units / BASE_UNITS,
                    "held": None if held is None else held * units / BASE_UNITS, "near": near_limit(row, now)})
    # ties (two fresh 5 h windows bind alike) go to the account whose longer windows lose the most quota unused
    return sorted(out, key=lambda r: (r["weighted"] is None, -(r["weighted"] or 0), -(r["held"] or 0)))


USAGE_KEYS = {"opencode": "opencode-go"}  # a provider whose usage reading has another name


def usage_key(provider: str, account: str = "") -> str:
    """The usage reading of an account (subscription_usage): a declared account by its name, a default by its provider's."""
    return account or USAGE_KEYS.get(provider, provider)


def excluded() -> list[str]:
    """`[accounts] exclude`: providers kept out of balancing (never suggested by `auto`, their default account kept)."""
    t = config.file_values().get("accounts")
    v = t.get("exclude") if isinstance(t, dict) else None
    return [str(x) for x in v] if isinstance(v, list) else []


def pool(providers: list[str] | None = None) -> list[str]:
    """The providers balancing may choose among: those given (else the ones in use on this host, `shoal ui set
    ecosystems`), less `[accounts] exclude`."""
    if providers is None:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui"))
        import ui_status
        providers = ui_status.active_ecosystems()
    out = excluded()
    return [p for p in providers if p not in out]


def choose(provider: str, now: float | None = None, snap: list | None = None) -> tuple[str, str]:
    """(account, why) the selection policy picks for a new session on `provider`: `[accounts] select = "balance"` (the
    default) the best-scored eligible account (a score above 0 and not near a limit), `default` the provider's default.
    The default account whenever the provider has one account, is excluded from balancing, or no account has a fresh,
    eligible reading."""
    if provider in excluded():
        return "", "excluded from balancing ([accounts] exclude)"
    if not several(provider):
        return "", "one account"
    if setting("select", "balance") not in ("balance",):
        return "", "select = default"
    top = next((r for r in rank(provider, now, snap) if r["score"] and not r["near"]), None)
    if top is None:
        return "", "no account has a fresh reading with room left: the default"
    return top["account"], f"most quota to use before its reset {top['score']:.2g} x {top['units']} units " \
                           f"({top['binding']})"


def choose_any(providers: list[str] | None = None, now: float | None = None,
               snap: list | None = None) -> tuple[str, str, str]:
    """(provider, account, why) across the balancing pool (`pool`): the account with the most weighted headroom of any
    provider in it (ties to the one whose longer windows lose the most). ("", "", why) when the pool is empty or no
    account has a fresh reading with room left: the caller then keeps its provider. `--ecosystem auto` on handoff and
    add-role."""
    if setting("select", "balance") != "balance":
        return "", "", "select = default"
    best = None
    for p in pool(providers):
        for r in rank(p, now, snap):
            if r["score"] and not r["near"] and (best is None or (r["weighted"], r["held"] or 0) >
                                                 (best[2]["weighted"], best[2]["held"] or 0)):
                best = (p, r["account"], r)
    if best is None:
        return "", "", "no provider in the pool has a fresh reading with room left"
    p, a, r = best
    return p, a, (f"most capacity-weighted quota to use in the pool ({', '.join(pool(providers))}): {name(p, a)} "
                  f"{r['score']:.2g} x {r['units']} units ({r['binding']})")


# ---- capacity and shares -------------------------------------------------------------------------------------------
# What an account's plan buys, in units where one Claude Max plan is 100: headroom is a share of an account's own
# windows, so comparing a Codex Plus account's with a Claude Max account's needs what each window holds (the
# maintainer's estimates, 2026-11-18: Codex Plus 0.15 to 0.2 of a Claude Max plan per 5 h and per week, Codex Pro about
# 1.25). `[accounts.capacity]` names each account's plan or units, a default account by its provider's name:
#
#   [accounts.capacity]
#   claude = "max"
#   claude-2 = "max"
#   codex = "plus"              # or { plan = "plus", units = 20 }, or a bare number of units
#
# An account with none is BASE_UNITS, so a host that declares nothing compares raw headroom as before.
BASE_UNITS = 100
PLAN_UNITS = {"claude": {"max": 100}, "codex": {"plus": 18, "pro": 125}}


def capacity(provider: str, account: str = "") -> tuple[int, str]:
    """(units, plan) of an account from `[accounts.capacity]`: a plan name (PLAN_UNITS), a number of units, or
    {plan, units}; (BASE_UNITS, "") when it names none, or a plan this provider does not have."""
    t = config.file_values().get("accounts")
    cap = t.get("capacity") if isinstance(t, dict) else None
    v = cap.get(name(provider, account)) if isinstance(cap, dict) else None
    plan, units = "", None
    if isinstance(v, dict):
        plan, units = str(v.get("plan") or ""), v.get("units")
    elif isinstance(v, str):
        plan = v
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        units = v
    if not isinstance(units, (int, float)) or isinstance(units, bool) or units <= 0:
        units = PLAN_UNITS.get(provider, {}).get(plan.lower())
    return (int(round(units)), plan) if units else (BASE_UNITS, plan)


def near_threshold(window: str) -> float:
    """The percent used at which `window` counts as near its limit: 100 less `[accounts] reserve_pct` (98 by default),
    or `[accounts] near_<window>` (`near_5h`, `near_week`) where a config sets one: an explicit override, kept so a
    config written for the old defaults (70 and 85) works as it did."""
    try:
        v = setting(f"near_{window}", "")
        return float(v) if v else 100.0 - reserve_pct()
    except ValueError:
        return 100.0 - reserve_pct()


def near_limit(usage_row: dict, now: float | None = None) -> str:
    """The window of a usage reading that is near its limit ("week 98.5% used"), a reached limit, or "" for none. A
    window that resets within the grace (`reset_grace_min`) is not near: its roles ride it out."""
    import time
    now = time.time() if now is None else now
    for r in (usage_row or {}).get("rows") or []:
        if r.get("blocked"):
            return f"{r.get('bucket') or '?'}: limit reached"
    grace = reset_grace_min() * 60
    for wname, _minutes, used, _free, left in _windows(usage_row or {}, now):
        if used >= near_threshold(wname) and not (left is not None and left <= grace):
            return f"{wname} {used:g}% used"
    return ""


def shares(load: dict, providers: list[str] | None = None, now: float | None = None,
           snap: list | None = None) -> dict[tuple[str, str], dict]:
    """{(provider, account): {load, share, target, units, weighted, near, open, binding}} over the balancing pool: each
    account's share of the roles is its capacity-weighted held headroom (`weighted`: `headroom(held=True)`, over its
    windows of a day or more, so a 5 h window moves roles only at its reserve; at most its units, so an account near its
    reset with room to spare does not draw every role) over the pool's total, and `target` that share of the roles
    running on the pool now (`load`, from the specs). `open`: a new session may start there now (its score above 0). An
    account with no fresh reading takes no share and keeps its load."""
    rows = []
    for p in pool(providers):
        for r in rank(p, now, snap):
            w = 0.0 if r["held"] is None or r["near"] else min(r["held"], float(r["units"]))
            rows.append(((p, r["account"]), w, r))
    total_w = sum(w for _, w, _ in rows)
    n = sum(load.get(k, 0) for k, _, _ in rows)
    out = {}
    for k, w, r in rows:
        share = w / total_w if total_w else 0.0
        out[k] = {"load": load.get(k, 0), "share": share, "target": share * n, "units": r["units"],
                  "weighted": w, "near": r["near"], "open": bool(r["score"]), "binding": r["binding"]}
    return out


def role_load(specs: list | None = None) -> dict[tuple[str, str], int]:
    """{(provider, account): roles running on it} over every live shoal the viewer shows (`ui_status.discover`): a role
    with a session counts on its recorded account, whichever host it runs on (an account is named alike on every host)."""
    if specs is None:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui"))
        import ui_status
        from roles import ShoalSpec
        specs = []
        for path in ui_status.discover(config.ledger_dir()):
            try:
                specs.append(ShoalSpec.load(path))
            except (OSError, ValueError):
                continue
    out: dict[tuple[str, str], int] = {}
    for spec in specs:
        for r in spec.roles:
            if r.session and r.ecosystem:
                k = (r.ecosystem, r.account or "")
                out[k] = out.get(k, 0) + 1
    return out


def preferred(provider: str) -> list[str] | None:
    """`[accounts.prefer] <provider> = [<role slugs>]`: the roles balancing may move onto that provider (shorter items
    suit a small plan: a long one would outlast its window); None when the provider names none (any role)."""
    t = config.file_values().get("accounts")
    v = (t.get("prefer") if isinstance(t, dict) else None) or {}
    roles = v.get(provider) if isinstance(v, dict) else None
    return [str(x) for x in roles] if isinstance(roles, list) else None


def quota_pick(provider: str, account: str, load: dict, pinned: bool = False, providers: list[str] | None = None,
               now: float | None = None, snap: list | None = None, slug: str = "") -> tuple[str, str, str] | None:
    """(provider, account, why) a role on `provider`/`account` should move to now, or None to stay: off an account near
    a limit (within the reserve) to the account furthest under its share that is not and can take a new session; else
    off an account carrying at least one role over its share to one at least one role under (shares rounded to whole
    roles, so an account whose share is most of a role gets one, and a move never just swaps which side is over).
    `pinned`: its own provider's accounts only; `slug`: a provider with `[accounts.prefer]` takes only the roles it
    names. None when this account has no fresh reading."""
    plan = shares(load, [provider] if pinned else providers, now, snap)
    here = plan.get((provider, account or ""))
    if here is None or (not here["near"] and here["weighted"] == 0 and here["binding"] == "no fresh reading"):
        return None
    room = [(k, v) for k, v in plan.items() if k != (provider, account or "") and v["weighted"] > 0 and v["open"]
            and not v["near"]
            and (k[0] == provider or preferred(k[0]) is None or slug in preferred(k[0]))]
    if not room:
        return None
    (p, a), v = max(room, key=lambda kv: (round(kv[1]["target"]) - kv[1]["load"], kv[1]["weighted"]))
    under = round(v["target"]) - v["load"]
    if here["near"]:
        return p, a, f"{name(provider, account)} is near its limit ({here['near']}); {name(p, a)} has room " \
                     f"({v['load']} of a {v['target']:.1f}-role share, {v['units']} units)"
    over = here["load"] - round(here["target"])
    if over >= 1 and under >= 1:
        return p, a, f"{name(provider, account)} carries {here['load']} roles against a {here['target']:.1f}-role " \
                     f"share; {name(p, a)} {v['load']} against {v['target']:.1f}"
    return None


def cmd_accounts(a) -> int:
    """`shoal accounts`: each provider's accounts on this host (declared ones and the defaults beside them)."""
    import json
    rows = describe()
    if not any(r["provider"] == "claude" for r in rows):  # the default Claude account has a sign-in to show and keep too
        rows = [{"provider": "claude", "account": "", "name": "claude", "home": home("claude"),
                 "exists": os.path.isdir(home("claude")), "badge": ""}] + rows
    try:
        balanced = pool()
    except Exception:  # noqa: BLE001 - no viewer settings: the pool rows are a convenience
        balanced = []
    for p in balanced:  # every pool provider's default account, which balancing chooses among too
        if not any(r["provider"] == p for r in rows):
            rows.append({"provider": p, "account": "", "name": p, "home": home(p) if p in HOME_VARS else "",
                         "exists": p in HOME_VARS and os.path.isdir(home(p)), "badge": ""})
    if getattr(a, "keepalive", False):
        for r in rows:
            if r["provider"] == "claude":
                print(f"{r['name']}: {keepalive('claude', r['account'], float(getattr(a, 'min_hours', KEEPALIVE_MIN_H)))}")
        return 0
    for r in rows:
        h = token_hours(r["provider"], r["account"])
        r["token_hours"] = round(h, 2) if h is not None else None
    if getattr(a, "check", False):
        for r in rows:
            r["signin"] = signin(r["provider"], r["account"])
    scores = {}
    for p in {r["provider"] for r in rows}:
        try:
            for x in rank(p):
                scores[(p, x["account"])] = x
        except Exception:  # noqa: BLE001 - no usage cache: scores are a convenience
            pass
    for r in rows:
        s = scores.get((r["provider"], r["account"])) or {}
        r["score"], r["binding"] = s.get("score"), s.get("binding", "")
    if getattr(a, "json", False):
        print(json.dumps({"accounts": rows, "select": setting("select", "balance"), "config": config.config_path()}))
        return 0
    if not rows:
        print(f"no accounts declared: each provider runs on its default account ([accounts.<name>] in "
              f"{config.config_path()} adds one)")
        return 0
    for r in rows:
        line = f"{r['provider']:<8} {r['name']:<14} {r['home'] or '-'}" + ("" if r["exists"] or not r["home"] else "  (missing)")
        if r.get("token_hours") is not None:
            line += f"  token {'expired ' + format(-r['token_hours'], '.1f') + ' h ago' if r['token_hours'] < 0 else format(r['token_hours'], '.1f') + ' h left'}"
        if "signin" in r:
            line += f"  {r['signin']}"
        units, plan = capacity(r["provider"], r["account"])
        line += f"  {units} units" + (f" ({plan})" if plan else "")
        if r.get("binding"):
            line += f"  headroom {r['score']:.2g} ({r['binding']})" if r["score"] is not None else f"  ({r['binding']})"
        print(line)
    for p in sorted({r["provider"] for r in rows}):
        acct, why = choose(p)
        print(f"{p}: a new session would go to {name(p, acct)} ({why}; [accounts] select = "
              f"{setting('select', 'balance')}, reserve_pct = {reserve_pct():g})")
    try:
        p, acct, why = choose_any()
        print(f"pool {', '.join(pool()) or '(empty)'}" + (f" (excluded: {', '.join(excluded())})" if excluded() else "")
              + f": `--ecosystem auto` would pick {name(p, acct) if p else 'nothing (the current provider stays)'}"
              + (f" ({why})" if p else f" ({why})"))
    except Exception:  # noqa: BLE001 - no usage cache or settings: the pool line is a convenience
        pass
    try:
        for (p, acct), v in sorted(shares(role_load()).items()):
            print(f"  share {name(p, acct)}: {v['load']} role(s) running, {v['target']:.1f} its share"
                  + (f" (near its limit: {v['near']})" if v["near"] else ""))
    except Exception:  # noqa: BLE001 - no specs or usage cache: the share lines are a convenience
        pass
    return 0
