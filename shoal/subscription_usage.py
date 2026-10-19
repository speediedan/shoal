"""Account quota observations, shared by every shoal and UI pane on this host.

Only account reads are permitted here. No thread/session/turn is created, no
inference endpoint is called, and no credential or raw response is cached.
The UI reads the cache; its feeder refreshes in a background thread every 5 min.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import sqlite3
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request

import config

PROVIDERS = ("codex", "claude", "muse", "opencode-go")
POLL_S = 300
STALE_S = 900
TIMEOUT_S = 15
MAX_BYTES = 1024 * 1024
CREDENTIAL_KEYS = {
    "claude": "SHOAL_USAGE_CLAUDE_OAUTH_TOKEN",
    "muse": "SHOAL_USAGE_MUSE_DEVICE_TOKEN",
    "opencode-go": "SHOAL_USAGE_OPENCODE_GO_API_KEY",
}
_RUNNING = False
_GUARD = threading.Lock()


class UsageError(Exception):
    """A safe diagnostic: never wrap a credential, server body or argv."""


def number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return None
    return v


def stamp(v):
    if isinstance(v, str):
        try:
            d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d.timestamp() if d.tzinfo else None
        except (ValueError, OverflowError):
            return None
    n = number(v)
    return n if n and n < 253402300800 else None


def text(v, cap=80):
    return "".join(c for c in str(v or "") if c.isprintable())[:cap]


def window(name, used, reset=None, minutes=None):
    n = number(used)
    if n is None:
        raise UsageError("invalid quota percentage")
    return {"name": text(name), "used_percent": n, "resets_at": stamp(reset),
            "minutes": number(minutes)}


def period(minutes):
    return {300: "5h", 10080: "week", 43200: "month"}.get(minutes, f"{minutes}m" if minutes else "window")


def normalize(provider, data):
    """Whitelist provider payloads into one shape. Missing data never becomes zero."""
    rows = []
    if provider == "codex":
        buckets = data.get("rateLimitsByLimitId") or {"codex": data.get("rateLimits") or {}}
        for key, bucket in buckets.items():
            windows = []
            for field in ("primary", "secondary"):
                w = bucket.get(field)
                if isinstance(w, dict):
                    minutes = number(w.get("windowDurationMins"))
                    windows.append(window(period(minutes), w.get("usedPercent"), w.get("resetsAt"), minutes))
            denied = data.get("ordinaryUsageAllowed") is False or bool(bucket.get("rateLimitReachedType")) \
                or bucket.get("spendControlReached") is True
            rows.append({"bucket": text(key), "plan": text(bucket.get("planType")),
                         "windows": windows, "blocked": denied})
    elif provider == "claude":
        windows = []
        for key, name, minutes in (("five_hour", "5h", 300), ("seven_day", "week", 10080)):
            w = data.get(key)
            if isinstance(w, dict):
                windows.append(window(name, w.get("utilization"), w.get("resets_at"), minutes))
        extra = data.get("extra_usage") or {}
        if extra.get("is_enabled") is True and extra.get("utilization") is not None:
            windows.append(window("extra month", extra["utilization"], extra.get("resets_at")))
        rows.append({"bucket": "claude", "plan": "subscription", "windows": windows})
        for key, w in data.items():
            if key.startswith("seven_day_") and isinstance(w, dict) and "utilization" in w:
                rows.append({"bucket": text(key[10:]), "plan": "weekly model limit",
                             "windows": [window("week", w["utilization"], w.get("resets_at"), 10080)]})
    elif provider == "muse":
        if data.get("is_subs_active") is not True:
            raise UsageError("no active subscription on this login")
        usage = data.get("subs_usage")
        if not isinstance(usage, dict):
            raise UsageError("quota omitted by login API; not a measured zero")
        windows = []
        for key, name in (("window", "5h"), ("weekly", "week")):
            w = usage.get(key)
            if not isinstance(w, dict):
                raise UsageError("incomplete subscription quota")
            minutes = w.get("window_duration_mins") if key == "window" else 10080
            windows.append(window(period(minutes) if key == "window" else name,
                                  w.get("used_percent"), w.get("resets_at"), minutes))
        rows.append({"bucket": "muse", "plan": text(data.get("subs_tier_name")), "windows": windows})
    elif provider == "opencode-go":
        usage = data.get("usage") or {}
        windows = []
        for key, name, minutes in (("rolling", "5h", 300), ("weekly", "week", 10080), ("monthly", "month", None)):
            w = usage.get(key)
            if not isinstance(w, dict):
                continue
            used = w.get("usagePercent", w.get("usedPercent", w.get("percent")))
            if used is None:
                amount, limit = number(w.get("used")), number(w.get("limit"))
                used = amount / limit * 100 if amount is not None and limit else None
            reset = w.get("resetAt", w.get("resetsAt"))
            if reset is None and number(w.get("resetInSec")) is not None:
                reset = time.time() + w["resetInSec"]
            windows.append(window(name, used, reset, minutes))
        rows.append({"bucket": "opencode-go", "plan": "Go", "windows": windows})
    if not rows or not any(r["windows"] or r.get("blocked") for r in rows):
        raise UsageError("no quota windows reported")
    return rows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward an account credential to another origin


def http_json(url, token, *, body=None, headers=None):
    if not token:
        raise UsageError("login missing; reconnect this provider")
    h = {"Authorization": "Bearer " + token, "User-Agent": "shoal-usage/0.1", "Accept": "application/json"}
    h.update(headers or {})
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=TIMEOUT_S) as r:
            raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise UsageError("quota response too large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise UsageError("invalid quota response")
        return data
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UsageError(f"HTTP {e.code}: login rejected; reconnect this provider") from None
        raise UsageError(f"HTTP {e.code}: quota read unavailable") from None
    except (OSError, ValueError):
        raise UsageError("quota read failed (network or JSON)") from None


def read_json(path):
    with open(path) as f:
        return json.load(f)


def codex_read():
    """A short-lived account RPC, using Codex's own credential renewal. No thread/start."""
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    p = subprocess.Popen([config.env("CODEX_BIN") or "codex", "app-server"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    replies = queue.Queue()

    def reader():
        for line in p.stdout:
            if len(line) > MAX_BYTES:
                continue
            try:
                replies.put(json.loads(line))
            except ValueError:
                pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    def send(d):
        p.stdin.write((json.dumps(d) + "\n").encode())
        p.stdin.flush()

    def call(i, method, params):
        send({"id": i, "method": method, "params": params})
        end = time.monotonic() + TIMEOUT_S
        while time.monotonic() < end:
            try:
                d = replies.get(timeout=min(.2, max(.01, end - time.monotonic())))
            except queue.Empty:
                if p.poll() is not None:
                    break
                continue
            if d.get("id") == i:
                if "error" in d:
                    raise UsageError("Codex account quota RPC failed")
                return d.get("result") or {}
        raise UsageError("Codex account quota RPC timed out")

    try:
        call(1, "initialize", {"clientInfo": {"name": "shoal_usage", "version": "0.1"}})
        send({"method": "initialized", "params": {}})
        return call(2, "account/rateLimits/read", {})
    finally:
        if p.poll() is None:
            p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        p.stdin.close()
        p.stdout.close()
        t.join(timeout=1)


def go_key():
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode"
    db = root / "opencode.db"
    if db.exists():
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            row = c.execute("SELECT value FROM credential WHERE integration_id=? "
                            "AND (active IS NULL OR active=1) ORDER BY time_updated DESC LIMIT 1",
                            ("opencode-go",)).fetchone()
        if row:
            return json.loads(row[0]).get("key", "")
    return (read_json(root / "auth.json").get("opencode-go") or {}).get("key", "")


def credential(provider):
    """Optional, private credential overrides. Parse data, never source a shell file."""
    key = CREDENTIAL_KEYS[provider]
    if os.environ.get(key):
        return os.environ[key]
    path = Path(config.env("USAGE_ENV_FILE") or Path(config.REPO_DIR) / "config/usage.env")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ""
    except OSError:
        raise UsageError("usage credential file unavailable") from None
    with os.fdopen(fd) as f:
        meta = os.fstat(f.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise UsageError("usage credential file must be owner-only (chmod 600)")
        raw = f.read(65537)
    if len(raw) > 65536:
        raise UsageError("usage credential file too large")
    values = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or name.strip() not in CREDENTIAL_KEYS.values():
            raise UsageError("invalid usage credential setting")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise UsageError("invalid usage credential quoting")
            value = value[1:-1]
        values[name.strip()] = value  # no expansion, escapes, execution or environment mutation
    return values.get(key, "")


def fetch(provider):
    if provider == "codex":
        return codex_read()
    if provider == "claude":
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        token = credential(provider) or (read_json(home / ".credentials.json").get("claudeAiOauth") or {}).get("accessToken", "")
        return http_json("https://api.anthropic.com/api/oauth/usage", token,
                         headers={"anthropic-beta": "oauth-2025-04-20"})
    if provider == "muse":
        home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        token = credential(provider) or (read_json(home / "muse/auth.json").get("providers", {}).get("meta") or {}).get("access_token", "")
        return http_json("https://api.meta.ai/muse-code/key", token, body=b'{"onboard":false}',
                         headers={"x-api-version": "1.0.0"})
    if provider == "opencode-go":
        return http_json("https://opencode.ai/zen/go/v1/usage", credential(provider) or go_key())
    raise UsageError("unsupported subscription provider")


def cache_path():
    root = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    return root / f"shoal-ui-{os.getuid()}" / "subscription-usage.json"


def load_cache():
    try:
        d = read_json(cache_path())
        return d if d.get("v") == 1 and isinstance(d.get("accounts"), dict) else {"v": 1, "accounts": {}}
    except (OSError, ValueError, AttributeError):
        return {"v": 1, "accounts": {}}


def read_bridge(bridge, targets):
    """Optional operator-owned fallback reader. Never execute a shell string."""
    if not isinstance(bridge, dict) or not isinstance(bridge.get("command"), list):
        return {}
    try:
        r = subprocess.run(bridge["command"], input=json.dumps({"providers": targets}).encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=30)
        if r.returncode or len(r.stdout) > MAX_BYTES:
            raise ValueError
        data = json.loads(r.stdout)
        return {p: data.get(p) if isinstance(data, dict) else None for p in targets}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {p: None for p in targets}


def collect(providers, *, force=False, fetcher=None, now=None, bridge=None):
    """One host-wide lock and per-account TTL, including failures. Cache normalized data only."""
    now = time.time() if now is None else now
    path = cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return load_cache()
        doc = load_cache()
        fetcher = fetcher or fetch
        fallback = []
        for provider in dict.fromkeys(providers):
            if provider not in PROVIDERS:
                continue
            prev = doc["accounts"].get(provider) or {}
            if not force and now < prev.get("next_poll_at", 0):
                continue
            row = dict(prev, provider=provider, attempted_at=now)
            try:
                row.update(rows=normalize(provider, fetcher(provider)), observed_at=now,
                           source="account API",
                           error="", failures=0, next_poll_at=now + POLL_S)
            except Exception as e:  # credentials, HTTP, CLI and parser failures never crash the feeder
                failures = min(int(prev.get("failures", 0)) + 1, 5)
                row.update(error=str(e) if isinstance(e, UsageError) else "quota read unavailable; check provider login",
                           failures=failures, next_poll_at=now + min(POLL_S * 2 ** (failures - 1), 3600))
                if provider in (bridge or {}).get("providers", []):
                    fallback.append(provider)
            doc["accounts"][provider] = row
        # Direct account credentials always win. One optional batch covers failed accounts only.
        if fallback:
            bridged = read_bridge(bridge, fallback)
            for provider in fallback:
                row = doc["accounts"][provider]
                try:
                    data = bridged.get(provider)
                    if not isinstance(data, dict) or data.get("error"):
                        raise UsageError("fallback quota reader unavailable")
                    observed = stamp(data.get("observed_at")) if "observed_at" in data else now
                    if observed is None or observed > now + 60:
                        raise UsageError("invalid fallback observation time")
                    row.update(rows=normalize(provider, data), observed_at=observed, source="fallback reader",
                               error="", failures=0, next_poll_at=now + POLL_S)
                except Exception:
                    row["error"] += "; fallback quota reader unavailable"
        tmp = str(path) + f".{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
        return doc


def refresh_async(providers, bridge=None):
    global _RUNNING
    if not providers:
        return
    with _GUARD:
        if _RUNNING:
            return
        _RUNNING = True

    def work():
        global _RUNNING
        try:
            collect(providers, bridge=bridge)
        except OSError:
            pass  # an unavailable runtime directory leaves the old, visibly stale reading
        finally:
            with _GUARD:
                _RUNNING = False

    threading.Thread(target=work, daemon=True, name="subscription-usage").start()


def snapshot(providers, *, now=None):
    now = time.time() if now is None else now
    accounts = load_cache()["accounts"]
    out = []
    for provider in dict.fromkeys(providers):
        if provider not in PROVIDERS:
            continue
        row = dict(accounts.get(provider) or {}, provider=provider)
        observed = row.get("observed_at")
        row["age_s"] = max(0, now - observed) if observed is not None else None
        row["stale"] = bool(row.get("error")) or observed is None or now - observed > STALE_S
        row.setdefault("error", "not collected yet")
        out.append(row)
    return {"accounts": out}


def severity(account, row):
    if row.get("blocked") or any(w["used_percent"] >= 90 for w in row.get("windows", [])):
        return "critical"
    if account.get("stale") or any(w["used_percent"] >= 70 or
                                   (w.get("resets_at") is not None and w["resets_at"] <= time.time())
                                   for w in row.get("windows", [])):
        return "warning"
    return "ok"


def reset_text(value):
    if value is None:
        return "reset ?"
    zone = config.tzinfo() or dt.timezone.utc
    d = dt.datetime.fromtimestamp(math.ceil(value / 60) * 60, zone)
    return d.strftime("%b %d %H:%M")


def render_lines(doc, *, color=False):
    """One text line per provider bucket: the form for a terminal too narrow for the table."""
    accounts = doc.get("accounts") or []
    if not accounts:
        return ""
    colors = {"ok": "\x1b[32m", "warning": "\x1b[33m", "critical": "\x1b[31m"}
    zone = config.timezone() if config.tzinfo() else "UTC"
    lines = [f"Subscription usage (used %, resets in {zone}; -- = not reported; ~ = stale)"]
    for a in accounts:
        rows = a.get("rows") or [{"bucket": a["provider"], "windows": []}]
        for r in rows:
            cells = []
            for w in r["windows"]:
                due = w.get("resets_at") is not None and w["resets_at"] <= time.time()
                mark = "~" if a.get("stale") or due else ""
                cells.append(f"{w['name']} {mark}{w['used_percent']:g}% ({reset_text(w.get('resets_at'))}"
                             + (", refresh due)" if due else ")"))
            label = a["provider"] + ("/" + r["bucket"] if r["bucket"] != a["provider"] else "")
            state = severity(a, r)
            body = f"  {label}: " + ("  ".join(cells) if cells else "? unavailable")
            missing = [n for n in ("5h", "week", "month") if n not in {w["name"] for w in r["windows"]}]
            if cells and missing:
                body += "  " + "/".join(missing) + " --"
            if r.get("blocked"):
                body += "  LIMIT REACHED"
            if a.get("error"):
                body += "  [" + text(a["error"], 100) + "]"
            if a.get("age_s") is not None:
                body += f"  as of {int(a['age_s']) // 60}m ago"
            if a.get("source") == "fallback reader":
                body += "  [fallback]"
            lines.append(colors[state] + body + "\x1b[0m" if color else body)
    return "\n".join(lines)


WINDOWS = ("5h", "week", "month")
BAR_STEPS = (8, 5, 3)  # bar cells, widest first: the table takes the widest set that fits the terminal
DIM, RESET_CODE = "\x1b[90m", "\x1b[0m"
SEVERITY_CODES = {"ok": "\x1b[32m", "warning": "\x1b[33m", "critical": "\x1b[31m"}


def _bar_glyphs():
    """Block glyphs when the output encoding can carry them, plain ASCII otherwise."""
    enc = getattr(__import__("sys").stdout, "encoding", None) or "ascii"
    try:
        "\u2588\u2591".encode(enc)
        return "\u2588", "\u2591"
    except (UnicodeEncodeError, LookupError):
        return "#", "-"


def bar(percent, cells, glyphs=None):
    """(filled, empty) cell strings for a used percentage: a nonzero reading always shows one filled
    cell and anything under 100 leaves one empty, so the bar never reads as 0 or as a full limit."""
    full, empty = glyphs or _bar_glyphs()
    n = max(1, cells)
    f = int(round(max(0.0, min(100.0, percent)) / 100 * n))
    if percent > 0 and f == 0:
        f = 1
    if percent < 100 and f >= n:
        f = n - 1
    return full * f, empty * (n - f)


def _window_code(pct):
    return SEVERITY_CODES["critical" if pct >= 90 else "warning" if pct >= 70 else "ok"]


def _cell(w, stale, cells, glyphs):
    """Segments (text, colour code) of one window cell: bar, percent, reset time."""
    due = w.get("resets_at") is not None and w["resets_at"] <= time.time()
    mark = "~" if stale or due else ""
    pct = w["used_percent"]
    filled, empty = bar(pct, cells, glyphs)
    code = SEVERITY_CODES["warning"] if (stale or due) and pct < 70 else _window_code(pct)
    return [(filled, code), (empty, DIM), (" ", ""), (f"{mark}{pct:g}%".rjust(5), code),
            (" " + reset_text(w.get("resets_at")), "")]


def _age_text(age_s):
    if age_s is None:
        return "?"
    m = int(age_s) // 60
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago"


def _plain(cell):
    return "".join(t for t, _ in cell)


def render_table(doc, *, color=False, width=0, glyphs=None):
    """The overview block: one boxed row per provider bucket with 5h, week and month side by side,
    each a bar, the used percent and the reset time, then when the reading was taken. Returns ""
    when `width` is too narrow for even the shortest bar (use render_lines then)."""
    accounts = doc.get("accounts") or []
    if not accounts:
        return ""
    glyphs = glyphs or _bar_glyphs()
    heads = ["provider", *WINDOWS, "as of"]
    for cells in BAR_STEPS:
        body = []
        for a in accounts:
            for r in a.get("rows") or [{"bucket": a["provider"], "windows": []}]:
                by = {w["name"]: w for w in r["windows"]}
                row = [[(a["provider"] + ("/" + r["bucket"] if r["bucket"] != a["provider"] else ""),
                         SEVERITY_CODES[severity(a, r)])]]
                for i, name in enumerate(WINDOWS):
                    if name in by:
                        row.append(_cell(by[name], a.get("stale"), cells, glyphs))
                    else:
                        row.append([("? unavailable" if i == 0 and not by else "--", "")])
                row.append([(_age_text(a.get("age_s")), "")])
                body.append(row)
        w = [max([len(h)] + [len(_plain(r[i])) for r in body]) for i, h in enumerate(heads)]
        if not width or sum(w) + 3 * len(w) + 1 <= width:
            break
    else:
        return ""
    zone = config.timezone() if config.tzinfo() else "UTC"
    rule = "+" + "+".join("-" * (x + 2) for x in w) + "+"

    def line(cells_, head=False):
        out = []
        for i, c in enumerate(cells_):
            pad = " " * (w[i] - len(_plain(c)))
            if color and not head:
                out.append("".join(f"{code}{t}{RESET_CODE}" if code and t else t for t, code in c) + pad)
            else:
                out.append(_plain(c) + pad)
        return "| " + " | ".join(out) + " |"

    lines = [f"Subscription usage (used %, resets in {zone}; ~ = stale or refresh due; -- = not reported)", rule,
             line([[(h, "")] for h in heads], head=True), rule, *[line(r) for r in body], rule]
    for a in accounts:  # what a table cell cannot carry: a reached limit, a fallback source, a read error
        for r in a.get("rows") or []:
            if r.get("blocked"):
                lines.append((SEVERITY_CODES["critical"] + f"{a['provider']}: LIMIT REACHED" + RESET_CODE) if color
                             else f"{a['provider']}: LIMIT REACHED")
        extra = ([f"fallback reader"] if a.get("source") == "fallback reader" else []) + \
                ([text(a["error"], 100)] if a.get("error") else [])
        if extra:
            lines.append(f"{a['provider']}: " + "; ".join(extra))
    return "\n".join(lines)


def render(doc, *, color=False, width=0):
    """Compact, always-visible overview block, using the same cache in every pane: the table when it
    fits `width` (0 = unconstrained), else one line per provider bucket."""
    return render_table(doc, color=color, width=width) or render_lines(doc, color=color)


def summary(doc):
    parts = []
    for a in doc.get("accounts") or []:
        windows = [(w, r) for r in a.get("rows", []) for w in r.get("windows", [])]
        if not windows:
            denied = any(r.get("blocked") for r in a.get("rows", []))
            parts.append(f"{a['provider']} " + ("! LIMIT REACHED" if denied else "?"))
            continue
        w, row = max(windows, key=lambda pair: pair[0]["used_percent"])
        expired = w.get("resets_at") is not None and w["resets_at"] <= time.time()
        level = "critical" if any(r.get("blocked") for r in a.get("rows", [])) else severity(a, row)
        flag = ("~" if a.get("stale") or expired else "") + ("!" if level == "critical" else "^" if level == "warning" else "")
        reset = w.get("resets_at")
        date = dt.datetime.fromtimestamp(math.ceil(reset / 60) * 60, config.tzinfo() or dt.timezone.utc) if reset else None
        short_reset = date.strftime("%H:%M" if w["name"] == "5h" else "%a%H:%M") if date else "?"
        parts.append(f"{a['provider']} {flag}{w['used_percent']:g}%/{w['name']}@{short_reset}")
    return " | ".join(parts)


def cmd_usage(a):
    import ui_status
    providers = a.providers.split(",") if a.providers else ui_status.ui_config().get("usage_providers", [])
    if any(p not in PROVIDERS for p in providers):
        print("Supported subscription providers: " + ",".join(PROVIDERS))
        return 1
    if a.refresh:
        collect(providers, force=True, bridge=ui_status.ui_config().get("usage_bridge"))
    doc = snapshot(providers)
    print(json.dumps(doc) if a.json else render(doc, color=ui_status.use_color(), width=ui_status.term_width()) or
          "No subscription providers configured; set usage_providers in the UI settings.")
    return 0
