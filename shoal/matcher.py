#!/usr/bin/env python3
"""Semantic matcher for the board digest (architecture doc §4.1; H8 proposal §2).

A model scores every open board item 0-3 against every lane's declared brief, and
classifies it (workable | needs-maintainer | blocked | parked), in place of affinity
weights. Opt-in per HAG with `features.matcher` (absent: the digest behaves exactly as
before). `live: false` is shadow mode: the semantic ranking is written beside the live
digest for `hag.py digest --compare`; `live: true` makes it the ranking roles read.

Transport, measured 2026-09-23:
  - Zen free models refuse direct API calls (`FreeTierError: OpenCode's free tier can
    only be used from within OpenCode`), so free models run through `opencode run`,
    which uses opencode's own login and needs no key;
  - a long prompt passed as the message argument hung 600 s without creating a session,
    so the prompt is always an attached file (`-f <absolute path>`);
  - every run's session is deleted afterwards, so the store does not fill up.
The direct OpenAI-compatible API is only the optional paid fallback, keyed by
`HAG_MATCHER_API_KEY` in `hag/.env` (gitignored; created by the maintainer).

Models rotate. Resolution is tiered, cheapest first:
  free  the model that last worked, the configured model and fallbacks, then free models
        discovered from `opencode models opencode` cross-checked against the Zen pricing table;
  go    the OpenCode Go subscription (`go_fallbacks`, opencode's stored login): the first
        PAID tier, a flat monthly fee with usage caps;
  zen   the Zen balance (`paid_fallback`, pay per token, key in hag/.env).
While a paid tier is active the free tier is re-probed every `free_recheck_min`. A switch is
warned on stderr, recorded as a ledger `system` event, and paged at most once a day.

Results are cached per (item content, lane brief): a quiet board costs no model call.
Private-repository items are sent (maintainer exemption for issue routing, 2026-09-23)
unless the spec sets `private_repos: "withhold"`.
"""
from __future__ import annotations

import datetime
import hashlib
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from roles import HagSpec, data_dir  # noqa: E402

ENV_FILE = os.path.join(HERE, ".env")
PRICING_URL = "https://opencode.ai/docs/zen/"
PROBE_PROMPT = "Reply with the single word ok and nothing else. Do not use any tools."
INSTRUCTION = ("Follow the instructions in the attached file exactly. Do not use any tools. "
               "Reply with the JSON object only.")
CLASSES = ("workable", "needs-maintainer", "blocked", "parked")
DEFAULTS = {
    "backend": "opencode-run",
    "model": "opencode/muse-spark-1.3-contributor-free",
    "fallbacks": ["opencode/space-bunny-free", "opencode/nemotron-3.5-lightning-free"],
    "discover_free": True,
    # paid tiers, tried only after every free route: the OpenCode Go subscription first (flat
    # monthly fee with usage caps; opencode's stored `opencode-go` login, no key file), then the
    # Zen balance (pay per token; `paid_fallback`, keyed from hag/.env)
    # measured 2026-09-23: both answer in ~5 s and neither trains on prompts (Go privacy table);
    # opencode-go/deepseek-v4-flash needs the workspace's Global-regions setting and, through
    # `opencode run`, hangs silently instead of erroring, so it is not a default
    "go_fallbacks": ["opencode-go/glm-5.3-flash", "opencode-go/kimi-k2.6"],
    "paid_fallback": None,  # {"backend": "openai-compat", "base_url", "model", "key_env"}
    "free_recheck_min": 60,  # while a paid tier is active, re-probe the free tier this often
    "private_repos": "send",  # send | withhold
    "chunk": 40,
    "timeout_s": 600,
    "probe_timeout_s": 60,  # a healthy probe answers in ~5 s; a refused route can hang silently
    "stale_hours": 24,
    "live": False,
}
EXCERPT = 300


class Unavailable(RuntimeError):
    """The model cannot serve this route (rotated out, free tier refused, no funds, auth)."""


class Transient(RuntimeError):
    """A timeout or an unclassified failure: skip this run, keep the model."""


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config(spec: HagSpec) -> dict | None:
    """The merged matcher config, or None when the HAG has not opted in."""
    m = (spec.features or {}).get("matcher")
    if not m:
        return None
    return dict(DEFAULTS, **(m if isinstance(m, dict) else {}))


def mdir(hag: str) -> str:
    return os.path.join(data_dir(hag), "matcher")


def load_env(path: str = ENV_FILE) -> dict[str, str]:
    """KEY=VALUE lines from the gitignored hag/.env; os.environ wins over the file."""
    out: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    out.update({k: v for k, v in os.environ.items() if k.startswith("HAG_")})
    return out


def opencode_bin() -> str:
    # systemd timer units carry a bare PATH, so fall back to the installer's location
    return shutil.which("opencode") or os.path.expanduser("~/.opencode/bin/opencode")


def _h(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def item_hash(it: dict) -> str:
    return _h(it.get("title", ""), ",".join(sorted(it.get("labels") or [])), it.get("excerpt", "")[:EXCERPT],
              ",".join(it.get("blocked_by") or []), it.get("status", ""))


def lanes(spec: HagSpec) -> dict[str, str]:
    """Roles scored by the matcher: workers with a declared brief, never the aux lane."""
    out = {}
    for r in spec.roles:
        if r.fill in ("self", "assigned") and not getattr(r, "aux", False):
            brief = (r.working_context() or {}).get("brief", "").strip()
            if brief:
                out[r.slug] = brief
    return out


# ---- transports ----------------------------------------------------------------

def _classify(text: str) -> type[RuntimeError]:
    t = text.lower()
    if any(k in t for k in ("timed out", "timeout")):
        return Transient
    return Unavailable


def _delete_sessions(ids: set[str], title: str) -> None:
    exe = opencode_bin()
    try:
        r = subprocess.run([exe, "session", "list", "--format", "json", "-n", "50"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        for s in json.loads(r.stdout or "[]"):
            if s.get("title") == title:
                ids.add(s.get("id", ""))
    except Exception:  # noqa: BLE001
        pass
    for sid in filter(None, ids):
        subprocess.run([exe, "session", "delete", sid], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)


def call_opencode(model: str, prompt: str, workdir: str, timeout: float) -> str:
    """One `opencode run` with the prompt attached; returns the reply text."""
    os.makedirs(workdir, exist_ok=True)
    pf = os.path.abspath(os.path.join(workdir, "prompt.md"))
    with open(pf, "w") as fh:
        fh.write(prompt)
    title = f"hag-matcher-{int(time.time() * 1000)}"
    argv = [opencode_bin(), "run", "--pure", "--dir", os.path.abspath(workdir), "-m", model, "--format", "json",
            "--title", title, "-f", pf, "--", INSTRUCTION]
    ids: set[str] = set()
    try:
        try:
            # own process group: a hung run is killed whole, so a leftover child cannot hold
            # opencode's store and stall the next run (measured: a hung Go run did)
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=workdir,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            raise Unavailable(f"opencode not runnable: {exc}") from exc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.communicate()
            raise Transient(f"{model}: timed out after {timeout:g}s") from exc
        p = subprocess.CompletedProcess(argv, proc.returncode, out, err)
        text, errs = [], []
        for line in p.stdout.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ids.add(e.get("sessionID", ""))
            if e.get("type") == "text":
                text.append((e.get("part") or {}).get("text", ""))
            elif e.get("type") == "error":
                d = (e.get("error") or {}).get("data") or {}
                errs.append(f"{(e.get('error') or {}).get('name', 'error')}: {d.get('message', '')}"[:200])
        if errs:
            raise _classify(" ".join(errs))(f"{model}: {errs[0]}")
        if p.returncode != 0 and not text:
            raise _classify(p.stderr)(f"{model}: rc={p.returncode} {p.stderr.strip()[-200:]}")
        return "".join(text)
    finally:
        _delete_sessions(ids, title)


def call_openai(base_url: str, model: str, key: str, prompt: str, timeout: float) -> str:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "hag-matcher/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode("utf-8", "replace")[:200]
        raise (Transient if exc.code >= 500 and "upstream request failed" not in msg.lower() else Unavailable)(
            f"{model}: HTTP {exc.code} {msg}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Transient(f"{model}: {exc}") from exc
    if d.get("error"):
        raise Unavailable(f"{model}: {str(d['error'])[:200]}")
    return d["choices"][0]["message"]["content"]


# ---- model resolution ------------------------------------------------------------

def _state_path(hag: str) -> str:
    return os.path.join(mdir(hag), "state.json")


def load_state(hag: str) -> dict:
    try:
        with open(_state_path(hag)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(hag: str, st: dict) -> None:
    os.makedirs(mdir(hag), exist_ok=True)
    tmp = _state_path(hag) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, _state_path(hag))


def pricing_free_names(url: str = PRICING_URL, timeout: float = 30) -> set[str]:
    """Normalized names of the Zen pricing rows whose Input column reads `Free`."""
    req = urllib.request.Request(url, headers={"User-Agent": "hag-matcher/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read().decode("utf-8", "replace")
    i = t.lower().find('id="pricing"')
    names = set()
    for row in re.findall(r"<tr>(.*?)</tr>", t[i if i >= 0 else 0:], re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) >= 2 and cells[1].lower() == "free":
            names.add(re.sub(r"\s+", "-", cells[0].lower()))
    return names


def discover_free(warn) -> list[str]:
    """Free Zen models reachable through opencode: `opencode models opencode` ids that the
    pricing table lists as free (ids ending -free alone when the page cannot be read)."""
    try:
        r = subprocess.run([opencode_bin(), "models", "opencode"], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        ids = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("opencode/")]
    except Exception as exc:  # noqa: BLE001
        warn(f"model discovery failed: `opencode models` ({exc})")
        return []
    try:
        free = pricing_free_names()
    except Exception as exc:  # noqa: BLE001
        warn(f"pricing page unreadable ({exc}); discovery falls back to ids ending in -free")
        return [m for m in ids if m.endswith("-free")]
    return [m for m in ids if m.split("/", 1)[1] in free]


TIERS = ("free", "go", "zen")


def tier_of(route: dict) -> str:
    """free: a Zen free model through opencode; go: the Go subscription; zen: the Zen balance."""
    if route.get("tier"):
        return route["tier"]
    if route.get("backend") == "openai-compat":
        return "zen"
    return "go" if str(route.get("model", "")).startswith("opencode-go/") else "free"


def _age_min(ts: str) -> float:
    try:
        t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 60


def candidates(c: dict, st: dict, env: dict, warn, stage: str, recheck: bool = False) -> list[dict]:
    """Routes for one resolution stage, in order:
      first       the route that last answered, unless it is a paid tier and the periodic
                  free-tier recheck is due (then the free tier gets its chance first);
      free        the configured model and fallbacks;
      discovered  free models from `opencode models opencode` x the Zen pricing table;
      go          the Go subscription (`go_fallbacks`);
      zen         the Zen balance (`paid_fallback`, when its key is present)."""
    out: list[dict] = []

    def add(route: dict) -> None:
        if route.get("model") and route not in out:
            out.append(route)

    if stage == "first":
        if st.get("active") and not recheck:
            add(st["active"])
    elif stage == "free":
        add({"backend": c["backend"], "model": c["model"]})
        for m in c.get("fallbacks") or []:  # "opencode/<id>" or {"backend", "model", ...}
            add(dict(m) if isinstance(m, dict) else {"backend": "opencode-run", "model": m})
    elif stage == "discovered" and c.get("discover_free"):
        for m in discover_free(warn):
            add({"backend": "opencode-run", "model": m})
    elif stage == "go":
        for m in c.get("go_fallbacks") or []:
            add(dict(m, tier="go") if isinstance(m, dict) else {"backend": "opencode-run", "model": m, "tier": "go"})
    elif stage == "zen":
        pf = c.get("paid_fallback") or None
        if pf and env.get(pf.get("key_env", "HAG_MATCHER_API_KEY")):
            add({"backend": pf.get("backend", "openai-compat"), "model": pf["model"], "tier": "zen",
                 "base_url": pf.get("base_url", "https://opencode.ai/zen/v1"),
                 "key_env": pf.get("key_env", "HAG_MATCHER_API_KEY")})
    return out


def call(route: dict, prompt: str, hag: str, timeout: float, env: dict) -> str:
    if route["backend"] == "opencode-run":
        return call_opencode(route["model"], prompt, os.path.join(mdir(hag), "run"), timeout)
    if route["backend"] == "openai-compat":
        key = env.get(route.get("key_env", "HAG_MATCHER_API_KEY"), "")
        if not key:
            raise Unavailable(f"{route['model']}: no {route.get('key_env')} in {ENV_FILE}")
        return call_openai(route["base_url"], route["model"], key, prompt, timeout)
    if route["backend"] == "command":  # tests: a local executable reads the prompt on stdin
        p = subprocess.run(route["model"], shell=True, input=prompt, capture_output=True, text=True,
                           timeout=timeout)
        if p.returncode != 0:
            raise _classify(p.stderr)(f"{route['model']}: rc={p.returncode} {p.stderr.strip()[-200:]}")
        return p.stdout
    raise Unavailable(f"unknown matcher backend {route['backend']!r}")


def resolve(spec: HagSpec, c: dict, st: dict, env: dict, notes: list[str]) -> dict | None:
    """The first route that answers the probe, cheapest tier first; records a switch. None
    when nothing answers."""
    tried: set[str] = set()
    act = st.get("active")
    recheck = bool(act) and tier_of(act) != "free" and _age_min(st.get("free_checked", "")) >= c["free_recheck_min"]
    if recheck:
        st["free_checked"] = now_iso()
        notes.append(f"free-tier recheck (active route is the {tier_of(act)} tier)")
    for stage in ("first", "free", "discovered", "go", "zen"):
        for route in candidates(c, st, env, notes.append, stage, recheck):
            key = f"{route['backend']}:{route['model']}"
            if key in tried:
                continue
            tried.add(key)
            try:
                if "ok" not in call(route, PROBE_PROMPT, spec.hag, c["probe_timeout_s"], env).lower():
                    raise Unavailable(f"{route['model']}: probe reply was not `ok`")
            except (Unavailable, Transient) as exc:
                notes.append(f"skip {key}: {str(exc)[:160]}")
                continue
            prev = st.get("active")
            if prev and (prev.get("backend"), prev.get("model")) != (route.get("backend"), route.get("model")):
                announce_switch(spec, prev, route, notes, st)
            if tier_of(route) != "free" and not st.get("free_checked"):
                st["free_checked"] = now_iso()
            st["active"], st["last_ok"] = route, now_iso()
            return route
    return None


def announce_switch(spec: HagSpec, prev: dict, new: dict, notes: list[str], st: dict) -> None:
    paid = {"go": " (PAID: the Go subscription's monthly usage)", "zen": " (PAID: the Zen balance)"}
    text = (f"matcher model switched: {prev.get('model')} ({tier_of(prev)} tier) -> {new['model']} "
            f"({tier_of(new)} tier){paid.get(tier_of(new), '')}. Free models are re-probed every "
            f"free_recheck_min while a paid tier is active; set features.matcher.model to pin a model.")
    print(f"WARNING: {text}", file=sys.stderr)
    notes.append(text)
    try:
        from ledger import Ledger
        Ledger(spec.hag, root=spec.ledger_dir).append("hag.py", "system", kind="matcher-model",
                                                      old=prev.get("model"), new=new["model"], text=text)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"(ledger system event failed: {exc})")
    day = now_iso()[:10]
    if st.get("last_page_day") != day:  # the caller saves st
        try:
            import health
            health.page_user(spec, f"[matcher] {text}", {})
            st["last_page_day"] = day
        except Exception as exc:  # noqa: BLE001
            notes.append(f"(page failed: {exc})")


# ---- prompt, parse, cache ----------------------------------------------------------

def build_prompt(briefs: dict[str, str], items: list[dict], suggest: dict[str, set[str]]) -> str:
    lanes_txt = "\n".join(f"- {slug}: {b}" for slug, b in briefs.items())
    rows = []
    for it in items:
        extra = []
        if it.get("blocked_by"):
            extra.append("open blockers: " + ",".join(it["blocked_by"]))
        if suggest.get(it["item"]):
            extra.append("a lane that declined it suggested: " + ",".join(sorted(suggest[it["item"]])))
        rows.append(f"{it['item']} | pos {it['pos']} | {it['title'][:120]} | labels: {', '.join(it['labels'])}"
                    + (f" | {'; '.join(extra)}" if extra else "")
                    + f" | {(it.get('excerpt') or '')[:EXCERPT]}")
    slugs = ", ".join(f'"{s}": <0-3>' for s in briefs)
    return f"""You route GitHub project-board items to worker lanes. Each lane has a declared brief:
{lanes_txt}

Board items (item | board position, lower is higher priority | title | labels | notes | body excerpt):
{chr(10).join(rows)}

For EVERY item give:
- "fits": a fit score for EVERY lane: 3 = squarely inside the lane's brief, 2 = clearly in scope, 1 = marginal,
  0 = outside it. Judge by the brief's meaning, not by shared words.
- "class": one of {', '.join(CLASSES)}. needs-maintainer = an open design question or a decision only the
  maintainer can make; blocked = the text says it waits on something unfinished; parked = explicitly parked
  or deferred; workable = none of those.
- "blocker": the blocker the text names (an issue reference or a short phrase), or "".
- "why": at most 12 words.

Output ONLY this JSON object, one entry per item, nothing else:
{{"items": [{{"item": "<item>", "fits": {{{slugs}}}, "class": "<class>", "blocker": "<text>", "why": "<words>"}}]}}"""


def parse(text: str, briefs: dict[str, str], wanted: set[str]) -> dict[str, dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise Transient("reply held no JSON object")
    try:
        d = json.loads(m.group(0))
    except ValueError as exc:
        raise Transient(f"reply JSON unparseable: {exc}") from exc
    out = {}
    for rec in d.get("items") or []:
        item = str(rec.get("item", "")).strip()
        if item not in wanted:
            continue
        fits = {}
        for s in briefs:
            try:
                fits[s] = max(0, min(3, int(round(float((rec.get("fits") or {}).get(s, 0))))))
            except (TypeError, ValueError):
                fits[s] = 0
        cls = rec.get("class") if rec.get("class") in CLASSES else "workable"
        out[item] = {"fits": fits, "class": cls, "blocker": str(rec.get("blocker") or "")[:120],
                     "why": str(rec.get("why") or "")[:100]}
    return out


def _cache_path(hag: str) -> str:
    return os.path.join(mdir(hag), "cache.json")


def load_cache(hag: str) -> dict:
    try:
        with open(_cache_path(hag)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(hag: str, cache: dict) -> None:
    os.makedirs(mdir(hag), exist_ok=True)
    tmp = _cache_path(hag) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cache, fh)
    os.replace(tmp, _cache_path(hag))


def repo_private(repo: str, cache: dict) -> bool:
    vis = cache.setdefault("_visibility", {})
    if repo not in vis:
        try:
            r = subprocess.run(["gh", "repo", "view", repo, "--json", "visibility", "-q", ".visibility"],
                               capture_output=True, text=True, timeout=30)
            vis[repo] = r.stdout.strip() or "UNKNOWN"
        except Exception:  # noqa: BLE001
            vis[repo] = "UNKNOWN"
    return vis[repo] != "PUBLIC"


def score(spec: HagSpec, items: list[dict], suggest: dict[str, set[str]] | None = None,
          no_call: bool = False) -> dict:
    """Fit matrix for the board: {item: {fits, class, blocker, why, model, ts, stale}} plus a
    run summary. Only new or changed items (or items missing a lane's current brief) cost a
    model call; when no model answers, entries younger than stale_hours are reused."""
    c = config(spec) or dict(DEFAULTS)
    briefs = lanes(spec)
    bh = {s: _h(b) for s, b in briefs.items()}
    cache = load_cache(spec.hag)
    st = load_state(spec.hag)
    env = load_env()
    notes: list[str] = []
    suggest = suggest or {}
    withheld = []
    todo = []
    for it in items:
        if c.get("private_repos") == "withhold" and repo_private(it["repo"], cache):
            withheld.append(it["item"])
            continue
        e = cache.get(it["item"])
        if e is None or e.get("h") != item_hash(it) or any(e.get("bh", {}).get(s) != h for s, h in bh.items()):
            todo.append(it)
    route, calls, t0, failed = None, 0, time.time(), []
    if todo and briefs and not no_call:
        route = resolve(spec, c, st, env, notes)
        if route is None:
            notes.append("NO MODEL ANSWERED: every candidate route failed its probe")
        chunk = max(1, int(c.get("chunk") or 40))
        for i in range(0, len(todo), chunk):
            if route is None:
                failed.extend(x["item"] for x in todo[i:])
                break
            part = todo[i:i + chunk]
            try:
                got = parse(call(route, build_prompt(briefs, part, suggest), spec.hag, c["timeout_s"], env),
                            briefs, {x["item"] for x in part})
                calls += 1
            except Unavailable as exc:
                notes.append(f"chunk {i // chunk}: {str(exc)[:160]}; re-resolving")
                st.pop("active", None)
                route = resolve(spec, c, st, env, notes)
                failed.extend(x["item"] for x in part)
                continue
            except Transient as exc:
                notes.append(f"chunk {i // chunk}: {str(exc)[:160]}")
                failed.extend(x["item"] for x in part)
                continue
            ts = now_iso()
            for x in part:
                if x["item"] in got:
                    cache[x["item"]] = dict(got[x["item"]], h=item_hash(x), bh=dict(bh), model=route["model"], ts=ts)
                else:
                    failed.append(x["item"])
        save_state(spec.hag, st)
    elif todo:
        failed = [x["item"] for x in todo]
    save_cache(spec.hag, cache)
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=float(c.get("stale_hours") or 24))).strftime("%Y-%m-%dT%H:%M:%SZ")
    fits, unscored = {}, []
    todo_keys = {x["item"] for x in todo}
    for it in items:
        e = cache.get(it["item"])
        if it["item"] in withheld or e is None:
            unscored.append(it["item"])
            continue
        stale = it["item"] in todo_keys and it["item"] in failed
        if stale and e.get("ts", "") < cutoff:
            unscored.append(it["item"])
            continue
        fits[it["item"]] = {"fits": {s: int(e.get("fits", {}).get(s, 0)) for s in briefs}, "class": e.get("class"),
                            "blocker": e.get("blocker", ""), "why": e.get("why", ""), "model": e.get("model"),
                            "stale": stale}
    return {"fits": fits, "lanes": list(briefs), "model": (route or st.get("active") or {}).get("model"),
            "backend": (route or st.get("active") or {}).get("backend"),
            "tier": tier_of(route or st.get("active") or {"model": ""}) if (route or st.get("active")) else None,
            "calls": calls,
            "latency_s": round(time.time() - t0, 1), "scored_now": len(todo) - len(set(failed) & todo_keys),
            "unscored": unscored, "withheld": withheld, "notes": notes, "live": bool(c.get("live"))}
