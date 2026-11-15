#!/usr/bin/env python3
"""Hermetic proof of provider accounts (shoal/accounts.py): with none declared every path is the one it was; with a
second Claude account declared, listings, job records, transcripts, the daemon roster and usage are read from both
accounts' directories, every call on an existing job runs in the account that holds it, a new job starts in the account
it was made for, and the default account is reached even from inside the second account's session.

`claude` is a fake script first on PATH that answers from `$CLAUDE_CONFIG_DIR/fake-agents.json` and logs the account
each call ran in; HOME is a scratch directory, so the default account is `$HOME/.claude`. Synthetic ids only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

T = tempfile.mkdtemp(prefix="shoal-accounts-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
HOME = os.path.join(T, "home")
D1, D2 = os.path.join(HOME, ".claude"), os.path.join(HOME, ".claude-2")
CX1, CX2 = os.path.join(HOME, ".codex"), os.path.join(HOME, ".codex-2")
BIN, LOG = os.path.join(T, "bin"), os.path.join(T, "calls.log")
for d in (D1, D2, CX1, CX2, BIN, os.path.join(T, "xdg")):
    os.makedirs(d, exist_ok=True)

NONE_CFG = os.path.join(T, "none.toml")
TWO_CFG = os.path.join(T, "two.toml")
open(NONE_CFG, "w").write('server = "testhost"\n')
open(TWO_CFG, "w").write('server = "testhost"\n\n[accounts]\nselect = "balance"\n\n'
                         '[accounts.claude-2]\nprovider = "claude"\nenv = { CLAUDE_CONFIG_DIR = "~/.claude-2" }\n\n'
                         '[accounts.codex-b]\nprovider = "codex"\nenv = { CODEX_HOME = "~/.codex-2" }\nbadge = "b"\n')

with open(os.path.join(BIN, "claude"), "w") as fh:
    fh.write(f"""#!/bin/bash
echo "${{CLAUDE_CONFIG_DIR:-default}}|$*" >> {LOG}
d="${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
case "$1" in
  agents) cat "$d/fake-agents.json" ;;
  --bg) echo "backgrounded · ${{FAKE_NEW_ID:-feedf00d}}" ;;
  stop|rm|respawn|logs) echo "ok $1 $2" ;;
esac
""")
os.chmod(os.path.join(BIN, "claude"), 0o755)

os.environ.update(HOME=HOME, PATH=BIN + ":" + os.environ.get("PATH", ""), XDG_CONFIG_HOME=os.path.join(T, "xdg"),
                  XDG_RUNTIME_DIR=os.path.join(T, "run"), SHOAL_TOOL_PATH=BIN, SHOAL_CONFIG=NONE_CFG)
for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "SHOAL_CODEX_HOME", "SHOAL_FAKE_SESSIONS", "SHOAL_FAKE_ROSTER",
          "SHOAL_USAGE_CLAUDE_OAUTH_TOKEN"):
    os.environ.pop(k, None)
sys.path[:0] = [SHOALD, os.path.join(SHOALD, "ui")]

import accounts  # noqa: E402
import backends  # noqa: E402
import codex_harness  # noqa: E402
import config  # noqa: E402
import health  # noqa: E402
import subscription_usage  # noqa: E402
import ui_app  # noqa: E402
import ui_status  # noqa: E402
import ui_view  # noqa: E402
from roles import Role, ShoalSpec  # noqa: E402


def fail(msg: str) -> None:
    print(f"test_accounts FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check(cond, msg: str) -> None:
    if not cond:
        fail(msg)


def use(cfg: str) -> None:
    os.environ["SHOAL_CONFIG"] = cfg


def calls() -> list[str]:
    try:
        return [ln.rstrip("\n") for ln in open(LOG)]
    except OSError:
        return []


def job(base: str, jid: str, name: str) -> str:
    conv = f"{jid}-0000-4000-8000-000000000000"
    os.makedirs(os.path.join(base, "jobs", jid), exist_ok=True)
    json.dump({"sessionId": conv, "name": name, "respawnFlags": ["-n", name, "--model", "m-test"]},
              open(os.path.join(base, "jobs", jid, "state.json"), "w"))
    os.makedirs(os.path.join(base, "projects", "-w"), exist_ok=True)
    open(os.path.join(base, "projects", "-w", f"{conv}.jsonl"), "w").write('{"type":"user"}\n')
    return conv


A1 = job(D1, "aaaa1111", "role-a")
B2 = job(D2, "bbbb2222", "role-b")
json.dump([{"id": "aaaa1111", "sessionId": A1, "name": "role-a", "pid": 11, "state": "working", "cwd": T}],
          open(os.path.join(D1, "fake-agents.json"), "w"))
json.dump([{"id": "bbbb2222", "sessionId": B2, "name": "role-b", "pid": 22, "state": "working", "cwd": T}],
          open(os.path.join(D2, "fake-agents.json"), "w"))
for d, jid in ((D1, "aaaa1111"), (D2, "bbbb2222")):
    os.makedirs(os.path.join(d, "daemon"), exist_ok=True)
# a flat map (older services), and the nested shape the service writes now ({proto, ..., workers: {job: ...}})
json.dump({"aaaa1111": {"pid": 1, "cliVersion": "9.9.9"}}, open(os.path.join(D1, "daemon", "roster.json"), "w"))
json.dump({"proto": 1, "supervisorPid": 5, "updatedAt": 1, "workers": {"bbbb2222": {"pid": 2, "cliVersion": "9.9.9"}}},
          open(os.path.join(D2, "daemon", "roster.json"), "w"))

# ---- 1. nothing declared: one account, the old paths and environment ---------------------------------------------
use(NONE_CFG)
check(accounts.of("claude") == [""] and not accounts.several("claude"), "no account declared reads as one")
check(backends.claude_env("") is backends._ENV, "with no account declared, a claude call gets the very same environment")
check(backends.claude_bases() == [D1] and backends.job_base("bbbb2222") == D1, "one Claude home, the default")
rows = backends.claude_agents()
check([r["id"] for r in rows] == ["aaaa1111"] and accounts.ROW_KEY not in rows[0], "the default listing, unlabeled")
check(calls() == ["default|agents --json"], f"one listing, in the default account: {calls()}")
check(backends.ClaudeBackend.view_argv(backends.ClaudeBackend(), A1)[1] == {"CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0"},
      "the attach environment is unchanged")
check(subscription_usage.account_key("claude-2") is None and not subscription_usage.known("claude-2"),
      "an undeclared account is not a usage provider")
try:
    accounts.normalize("claude", "claude-2")
    fail("an undeclared account was accepted")
except ValueError as exc:
    check("no account 'claude-2' is declared" in str(exc), f"the refusal names the account: {exc}")
check(accounts.normalize("claude", "claude") == "" and accounts.normalize("claude", "") == "", "the default is ''")

# ---- 2. claude-2 declared ------------------------------------------------------------------------------------------
use(TWO_CFG)
open(LOG, "w").close()
check(accounts.of("claude") == ["", "claude-2"] and accounts.several("claude"), "claude has two accounts")
check(accounts.normalize("claude", "claude-2") == "claude-2", "a declared account is accepted")
try:
    accounts.normalize("codex", "claude-2")
    fail("an account of another provider was accepted")
except ValueError as exc:
    check("is a claude account" in str(exc), f"refused as another provider's: {exc}")
check(accounts.badge("claude", "claude-2") == "2" and accounts.badge("codex", "codex-b") == "b", "badges")
check(accounts.setting("select") == "balance", "a scalar of [accounts] is a setting, not an account")
check([r["name"] for r in accounts.describe()] == ["claude", "claude-2", "codex", "codex-b"], "describe")

# the default account from inside the second account's session: its variable is dropped, not inherited
os.environ["CLAUDE_CONFIG_DIR"] = D2
check("CLAUDE_CONFIG_DIR" not in accounts.env("claude", ""), "the default drops another account's variable")
check(accounts.env("claude", "claude-2")["CLAUDE_CONFIG_DIR"] == D2, "the second account sets its own")
check(accounts.home("claude", "") == D1 and accounts.home("claude", "claude-2") == D2, "each account's home")
check(accounts.env_account("claude") == "claude-2", "the process's own account is recognized")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(T, "elsewhere")  # an operator's own default directory is kept
check(accounts.env("claude", "")["CLAUDE_CONFIG_DIR"] == os.path.join(T, "elsewhere"), "a non-account variable stays")
os.environ["CLAUDE_CONFIG_DIR"] = D2

rows = backends.claude_agents(include_stopped=True)
check({r["id"]: r.get(accounts.ROW_KEY, "") for r in rows} == {"aaaa1111": "", "bbbb2222": "claude-2"},
      f"both accounts listed, the second's rows labeled: {rows}")
check(calls() == ["default|agents --json --all", f"{D2}|agents --json --all"], f"one listing per account: {calls()}")
check(backends.job_base("bbbb2222") == D2 and backends.job_base("aaaa1111") == D1, "a job's record is found in its account")
check(backends._state_of("bbbb2222").get("name") == "role-b", "the second account's job record reads")
check(backends.claude_original("bbbb2222") == B2, "its original conversation reads")
check(health.claude_transcript(B2).startswith(D2) and health.claude_transcript(A1).startswith(D1),
      "transcripts are found in their own account")
check(set(health.claude_roster()) == {"aaaa1111", "bbbb2222"}, "the rosters of both services merge")
busy = health.claude_busy()
check(B2 in busy and A1 in busy, f"health sees both accounts' sessions: {sorted(busy)}")
check(ui_status.claude_says({"id": "bbbb2222"}, B2) == "", "a seat's needs read from its account (none here)")
check(ui_view._job_state(ui_view.claude_jobs_dir(), "bbbb2222").get("name") == "role-b",
      "the viewer reads a second account's job from the host's jobs directory")

open(LOG, "w").close()
backends.ClaudeBackend.stop("bbbb2222")
backends.ClaudeBackend.stop("aaaa1111")
check(calls() == [f"{D2}|stop bbbb2222", "default|stop aaaa1111"], f"each stop runs in the job's account: {calls()}")

open(LOG, "w").close()
os.environ["FAKE_NEW_ID"] = "c0ffee22"
new = backends.get("claude", "claude-2").create("t", T, brief="hello", name="role-c")
check(new == "c0ffee22" and calls() == [f"{D2}|--bg -n role-c hello"], f"a new job starts in its account: {calls()}")
open(LOG, "w").close()
backends.get("claude").create("t", T, brief="hi", name="role-d")
check(calls() == ["default|--bg -n role-d hi"], f"the default account's new job drops the variable: {calls()}")

argv, extra = backends.ClaudeBackend().view_argv("bbbb2222")
check(argv[-1] == "bbbb2222" and extra.get("CLAUDE_CONFIG_DIR") == D2, f"an attach runs in the job's account: {extra}")
argv, extra = backends.ClaudeBackend().view_argv("aaaa1111")
check("CLAUDE_CONFIG_DIR" in extra and extra["CLAUDE_CONFIG_DIR"] is None, "a default attach unsets the variable")
check("CLAUDE_CONFIG_DIR" not in ui_view.clean_env(extra), "the viewer's environment drops an unset variable")

# workspace trust is per account: a handoff refuses before it stops anything when the new account has not trusted it
REPO = os.path.join(T, "repo")
os.makedirs(os.path.join(REPO, "sub"), exist_ok=True)
json.dump({"projects": {REPO: {"hasTrustDialogAccepted": True}}}, open(os.path.join(HOME, ".claude.json"), "w"))
json.dump({"projects": {}}, open(os.path.join(D2, ".claude.json"), "w"))
check(backends.claude_untrusted("", os.path.join(REPO, "sub")) == "", "the default trusts it (a parent covers its subfolders)")
why = backends.claude_untrusted("claude-2", REPO)
check("claude-2 account has not trusted" in why and f"CLAUDE_CONFIG_DIR={D2} claude" in why, f"claude-2 has not: {why}")
use(NONE_CFG)
check(backends.claude_untrusted("", os.path.join(T, "elsewhere")) == "", "with one account nothing is checked")
use(TWO_CFG)

sess = {s["job"]: s.get("account", "") for s in backends.ClaudeBackend().sessions()}
check(sess == {"aaaa1111": "", "bbbb2222": "claude-2"}, f"discovery labels each session's account: {sess}")

# ---- 3. Codex: a thread runs in the account whose store holds it --------------------------------------------------
os.makedirs(os.path.join(CX2, "sessions", "2026", "11", "14"), exist_ok=True)
TID = "019a0000-0000-7000-8000-00000000c0de"
open(os.path.join(CX2, "sessions", "2026", "11", "14", f"rollout-2026-11-14T00-00-00-{TID}.jsonl"), "w").write("{}\n")
check(codex_harness.thread_account(TID) == "codex-b", "a thread is found in its account's store")
check(codex_harness.turn_env(session=TID).get("CODEX_HOME") == CX2, "its turns run with that CODEX_HOME")
check("CODEX_HOME" not in codex_harness.turn_env(account=""), "a new thread on the default account sets none")
check(codex_harness.turn_env(account="codex-b")["CODEX_HOME"] == CX2, "a new thread on an account sets its home")
check(codex_harness.rollouts(TID), "rollouts are read across accounts")

# ---- 4. usage: one row per account, each read with its own sign-in -------------------------------------------------
json.dump({"claudeAiOauth": {"accessToken": "tok-one"}}, open(os.path.join(D1, ".credentials.json"), "w"))
json.dump({"claudeAiOauth": {"accessToken": "tok-two"}}, open(os.path.join(D2, ".credentials.json"), "w"))
seen = []
subscription_usage.http_json = lambda url, token, **kw: seen.append(token) or {}
os.environ["SHOAL_USAGE_CLAUDE_OAUTH_TOKEN"] = "tok-override"
subscription_usage.fetch("claude-2")
subscription_usage.fetch("claude")
check(seen == ["tok-two", "tok-override"], f"the override is the default account's only: {seen}")
os.environ.pop("SHOAL_USAGE_CLAUDE_OAUTH_TOKEN")
data = {"five_hour": {"utilization": 10, "resets_at": "2026-11-15T00:00:00Z"}}
rows = subscription_usage.normalize_key("claude-2", data)
check(rows and rows[0]["bucket"] == "claude-2", f"an account's bucket is named after it: {rows}")
check(subscription_usage.compact_account("claude-2", True) == "cl2", "the compact status-bar name")
os.makedirs(os.path.dirname(ui_status.ui_config_path()), exist_ok=True)
json.dump({"usage_providers": ["codex", "claude"]}, open(ui_status.ui_config_path(), "w"))
check(ui_status.usage_providers() == ["codex", "codex-b", "claude", "claude-2"],
      f"each declared account follows its provider: {ui_status.usage_providers()}")

# ---- 5. quota balance: paced headroom, the policy's pick, `auto` -----------------------------------------------------
NOW = 1_800_000_000.0
WEEK = 10080


def reading(used: float, left_h: float, minutes: int = WEEK, **kw) -> dict:
    return {"rows": [{"bucket": "b", "windows": [{"name": "week", "used_percent": used, "resets_at": NOW + left_h * 3600,
                                                  "minutes": minutes}], **kw}], "stale": False, "error": ""}


h_a, _ = accounts.headroom(reading(50, 1), NOW)
h_b, why = accounts.headroom(reading(40, 144), NOW)
check(round(h_a) == 84 and round(h_b, 1) == 0.7 and h_a > h_b, f"the maintainer's example: {h_a} vs {h_b}")
check("week 40% used" in why, f"the binding window is named: {why}")
check(accounts.headroom(reading(99, 1), NOW)[0] == 0.0, "under 2% free is ineligible")
check(accounts.headroom(reading(10, 1, blocked=True), NOW)[0] == 0.0, "a reached limit is ineligible")
check(accounts.headroom({"stale": True}, NOW)[0] is None, "a stale reading has no score")
two = {"rows": [{"bucket": "b", "windows": [{"name": "5h", "used_percent": 90, "resets_at": NOW + 3600, "minutes": 300},
                                             {"name": "week", "used_percent": 10, "resets_at": NOW + 3600, "minutes": WEEK}]}]}
check(accounts.headroom(two, NOW)[1].startswith("5h"), "the tightest window binds")
snap = [dict(reading(94, 46), provider="claude"), dict(reading(1, 160), provider="claude-2")]
check(accounts.choose("claude", NOW, snap)[0] == "claude-2", "balance picks the account with more paced headroom")
snap = [dict(reading(10, 46), provider="claude"), {"provider": "claude-2", "stale": True}]
check(accounts.choose("claude", NOW, snap)[0] == "", "an unread account is never chosen")
check(accounts.rank("claude", NOW, snap)[-1]["account"] == "claude-2", "and is ranked last")
check(accounts.choose("codex", NOW, [])[0] == "", "no reading at all: the default")

# the pool: every provider in use less `[accounts] exclude`; `auto` picks the best account of any of them
snap = [dict(reading(94, 46), provider="claude"), dict(reading(50, 160), provider="claude-2"),
        dict(reading(0, 1), provider="opencode-go"), dict(reading(20, 100), provider="codex")]
p, a, why = accounts.choose_any(["claude", "codex", "opencode"], NOW, snap)
check((p, a) == ("opencode", ""), f"with nothing excluded the freshest headroom wins: {p} {a}")
EXCL_CFG = os.path.join(T, "excl.toml")
open(EXCL_CFG, "w").write(open(TWO_CFG).read().replace('select = "balance"', 'select = "balance"\nexclude = ["opencode"]'))
use(EXCL_CFG)
check(accounts.excluded() == ["opencode"] and accounts.pool(["claude", "codex", "opencode"]) == ["claude", "codex"],
      "exclude keeps a provider out of the pool")
p, a, why = accounts.choose_any(["claude", "codex", "opencode"], NOW, snap)
check((p, a) == ("codex", ""), f"an excluded provider is never picked: {p} {a} ({why})")
check(accounts.choose("opencode", NOW, snap)[1].startswith("excluded"), "nor balanced within")
check(list(accounts.declared()) == ["claude-2", "codex-b"], "a list under [accounts] is a setting, not an account")
use(TWO_CFG)

# a usage limit is one account's: it holds only that account's roles
import datetime as _dt  # noqa: E402
soon = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
lim = {"reset": soon, "accounts": {"claude": soon}}
check(health.limit_until(lim, "") == soon and health.limit_until(lim, "claude-2") == "", "a limit holds its own account only")
check(health.limit_until(lim) == soon, "without an account, the shoal-wide reset as before")
check(health.limit_until({"reset": soon}, "claude-2") == soon, "a reading from before accounts still holds everyone")

# ---- 6. records and icons ------------------------------------------------------------------------------------------
spec = ShoalSpec.from_dict({"sid": "acct-test", "roles": [{"slug": "x", "ecosystem": "claude", "account": "claude-2"}]}) \
    if hasattr(ShoalSpec, "from_dict") else None
r = spec.roles[0] if spec else Role(slug="x", ecosystem="claude", account="claude-2")
check(r.account == "claude-2", "a role records its account")
check(spec is None or spec.to_dict()["roles"][0]["account"] == "claude-2", "and saves it")
from cli import parse_role  # noqa: E402
check(parse_role("x,claude,X,/d.md,account=claude-2").account == "claude-2", "--role takes account=")
check(ui_app.program_key("claude", "claude-2") == "claude#2" and ui_app.program_key("claude", "") == "claude",
      "a plan names a second account's card with its badge")
svg = '<svg viewBox="0 0 24 24"><path d="M0 0"/></svg>'
b = ui_app.badged_svg(svg, "2")
check(b.endswith("</svg>") and "<circle" in b and b.count("M") == 1 + 5, "a badge: a disc and the digit's five strokes")
check(ui_app.badged_svg(svg, "x").count("<path") == 1, "a non-digit badge is a plain disc")

print("test_accounts: ok")
