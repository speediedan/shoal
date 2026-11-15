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


# ---- quota balance -------------------------------------------------------------------------------------------------
# An account's paced headroom in one window: its free share divided by the share of the window still to run (never less
# than an hour of it), so 50% free with 1 h of a week left (84) beats 60% free with 6 days left (0.7). The account's score
# is its tightest window's; one with any window under MIN_FREE free, or a reached limit, scores 0 (ineligible); one with
# no fresh reading has no score and is ranked last, never chosen by the policy.
MIN_FREE = 0.02
SELECT = ("balance", "default")


def headroom(usage_row: dict, now: float) -> tuple[float | None, str]:
    """(score, its binding window) of one usage reading (subscription_usage.snapshot's row): None when it has no fresh
    reading."""
    if not usage_row or usage_row.get("stale") or usage_row.get("error"):
        return None, "no fresh reading"
    best: tuple[float, str] | None = None
    for r in usage_row.get("rows") or []:
        if r.get("blocked"):
            return 0.0, f"{r.get('bucket') or '?'}: limit reached"
        for w in r.get("windows") or []:
            try:
                used, minutes, reset = float(w["used_percent"]), float(w.get("minutes") or 0), w.get("resets_at")
            except (KeyError, TypeError, ValueError):
                continue
            if minutes <= 0 or reset is None:
                continue
            span = minutes * 60
            free = max(0.0, 1 - used / 100)
            left = max(float(reset) - now, 0.0)
            h = 0.0 if free < MIN_FREE else free / max(left / span, 3600 / span)
            label = f"{w['name']} {used:g}% used, resets in {_dur(left)}"
            if best is None or h < best[0]:
                best = (h, label)
    return best if best is not None else (None, "no window reported")


def _dur(s: float) -> str:
    return f"{s / 86400:.1f} d" if s >= 86400 else f"{s / 3600:.1f} h" if s >= 3600 else f"{int(s / 60)} min"


def rank(provider: str, now: float | None = None, snap: list | None = None) -> list[dict]:
    """The provider's accounts on this host, best first: [{account, name, score, binding}] from the cached usage
    readings (no read here: `shoal ui usage --refresh` refreshes them); an account with no fresh reading last."""
    import time
    import subscription_usage
    now = time.time() if now is None else now
    keys = [usage_key(provider, a) for a in of(provider)]
    rows = {r.get("provider"): r for r in (subscription_usage.snapshot(keys, now=now)["accounts"] if snap is None else snap)}
    out = []
    for a in of(provider):
        score, why = headroom(rows.get(usage_key(provider, a)) or {}, now)
        out.append({"account": a, "name": name(provider, a), "score": score, "binding": why})
    return sorted(out, key=lambda r: (r["score"] is None, -(r["score"] or 0)))


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
    default) the best-scored eligible account, `default` the provider's default. The default account whenever the
    provider has one account, is excluded from balancing, or no account has a fresh, eligible reading."""
    if provider in excluded():
        return "", "excluded from balancing ([accounts] exclude)"
    if not several(provider):
        return "", "one account"
    if setting("select", "balance") not in ("balance",):
        return "", "select = default"
    ranked = rank(provider, now, snap)
    top = ranked[0] if ranked else None
    if not top or not top["score"]:
        return "", "no account has a fresh reading with room left: the default"
    return top["account"], f"best paced headroom {top['score']:.2g} ({top['binding']})"


def choose_any(providers: list[str] | None = None, now: float | None = None,
               snap: list | None = None) -> tuple[str, str, str]:
    """(provider, account, why) across the balancing pool (`pool`): the account with the most paced headroom of any
    provider in it. ("", "", why) when the pool is empty or no account has a fresh reading with room left: the caller
    then keeps its provider. `--ecosystem auto` on handoff and add-role."""
    if setting("select", "balance") != "balance":
        return "", "", "select = default"
    best = None
    for p in pool(providers):
        for r in rank(p, now, snap):
            if r["score"] and (best is None or r["score"] > best[2]["score"]):
                best = (p, r["account"], r)
    if best is None:
        return "", "", "no provider in the pool has a fresh reading with room left"
    p, a, r = best
    return p, a, f"best paced headroom in the pool ({', '.join(pool(providers))}): {name(p, a)} {r['score']:.2g} ({r['binding']})"


def cmd_accounts(a) -> int:
    """`shoal accounts`: each provider's accounts on this host (declared ones and the defaults beside them)."""
    import json
    rows = describe()
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
        if "signin" in r:
            line += f"  {r['signin']}"
        if r.get("binding"):
            line += f"  headroom {r['score']:.2g} ({r['binding']})" if r["score"] is not None else f"  ({r['binding']})"
        print(line)
    for p in sorted({r["provider"] for r in rows}):
        acct, why = choose(p)
        print(f"{p}: a new session would go to {name(p, acct)} ({why}; [accounts] select = {setting('select', 'balance')})")
    try:
        p, acct, why = choose_any()
        print(f"pool {', '.join(pool()) or '(empty)'}" + (f" (excluded: {', '.join(excluded())})" if excluded() else "")
              + f": `--ecosystem auto` would pick {name(p, acct) if p else 'nothing (the current provider stays)'}"
              + (f" ({why})" if p else f" ({why})"))
    except Exception:  # noqa: BLE001 - no usage cache or settings: the pool line is a convenience
        pass
    return 0
