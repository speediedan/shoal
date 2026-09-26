#!/usr/bin/env python3
"""Host configuration: the one place a host-specific value is resolved.

Every setting resolves in the same order: an environment variable, then the host
config file, then a generic default that works on any host. The host config file is
`$SHOAL_CONFIG` when set, else `<repository root>/config/host.toml` (gitignored; see
`config/host.example.toml`). TOML is read with `tomllib` (Python 3.11+) or `tomli`;
when neither is importable the file is skipped with one warning on stderr.

  key            env var                                     generic default
  data_root      SHOAL_DATA_ROOT                             $XDG_DATA_HOME/shoal (else ~/.local/share/shoal)
  ledger_dir     SHOAL_LEDGER_DIR                            <data_root>/ledger
  ledger_repo    SHOAL_LEDGER_REPO                           "" (start / ledger-init then need --ledger-repo)
  tool_path      SHOAL_TOOL_PATH                             this process's PATH
  lease_cmd      SHOAL_LEASE_CMD, GPU_LEASE_CMD              "" (no lease provider: lease waits read unknown)
  default_repos  (none)                                      [] (a spec's own `repos` still wins)
  cli            SHOAL_CLI                                   `shoal` when installed, else python3 <this package>/cli.py
  psi_out        SHOAL_PSI_OUT                               <data_root>/_instruments/psi_events.jsonl
  server         SHOAL_SERVER                                this host's name
  timezone       SHOAL_TZ                                    $TZ or /etc/timezone when it is an IANA name, else UTC
  default_owner  SHOAL_DEFAULT_OWNER                         "" (item keys keep every owner)
  azure_org      SHOAL_AZURE_ORG                             "" (Azure pipeline sensors and GPU share need it)
  azure_project  SHOAL_AZURE_PROJECT                         ""

Every `SHOAL_*` variable also honours its legacy `HAG_*` name as a fallback (the SHOAL name
wins when both are set), and `lease_cmd` falls back further to `GPU_LEASE_CMD`.

An empty environment variable counts as unset. Values are read at call time, so a test
that exports a variable before importing a module sees it.
"""
from __future__ import annotations

import os
import shlex
import shutil
import sys

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PKG_DIR)
_CACHE: dict[str, dict] = {}
_WARNED: set[str] = set()


ENV_PREFIXES = ("SHOAL_", "HAG_")  # current name first, then the legacy one


def names(suffix: str) -> tuple[str, ...]:
    """The environment variable names for one setting: `SHOAL_<suffix>`, then `HAG_<suffix>`."""
    return tuple(p + suffix for p in ENV_PREFIXES)


def env(suffix: str, default: str = "") -> str:
    """`$SHOAL_<suffix>`, else the legacy `$HAG_<suffix>`, else `default` (empty counts as unset)."""
    for n in names(suffix):
        v = os.environ.get(n, "")
        if v:
            return v
    return default


def add_id_arg(parser, **kw):
    """`--shoal ID` on an argparse parser, with `--hag` kept as a hidden alias (same dest,
    `hag`) because existing role documents carry `--hag` re-arm commands. The alias stays in
    the parser's option map, so it parses; help, usage and errors name `--shoal` only."""
    kw.setdefault("metavar", "ID")
    action = parser.add_argument("--shoal", "--hag", dest="hag", **kw)
    action.option_strings = ["--shoal"]
    return action


def config_path() -> str:
    return env("CONFIG") or os.path.join(REPO_DIR, "config", "host.toml")


def _warn(key: str, msg: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        print(f"shoal config: {msg}", file=sys.stderr)


def file_values() -> dict:
    """The parsed host config file ({} when absent, unreadable or no TOML reader)."""
    path = config_path()
    if path in _CACHE:
        return _CACHE[path]
    vals: dict = {}
    if os.path.isfile(path):
        try:
            import tomllib as _toml  # type: ignore[import-not-found]
        except ImportError:
            try:
                import tomli as _toml  # type: ignore[import-not-found,no-redef]
            except ImportError:
                _toml = None
        if _toml is None:
            _warn("reader", f"{path} skipped: neither tomllib nor tomli is importable")
        else:
            try:
                with open(path, "rb") as fh:
                    vals = _toml.load(fh)
            except Exception as exc:  # noqa: BLE001 - a bad file never takes a command down
                _warn("parse", f"{path} skipped: {exc}")
                vals = {}
    _CACHE[path] = vals
    return vals


def _get(key: str, envs: tuple[str, ...], default):
    for e in envs:
        v = os.environ.get(e, "")
        if v:
            return v
    f = file_values()
    if key in f and f[key] not in (None, ""):
        return f[key]
    return default() if callable(default) else default


def _path(v: str) -> str:
    return os.path.expanduser(str(v)) if v else ""


def data_root() -> str:
    def dflt():
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        return os.path.join(xdg, "shoal")
    return _path(_get("data_root", names("DATA_ROOT"), dflt))


def ledger_dir() -> str:
    return _path(_get("ledger_dir", names("LEDGER_DIR"), lambda: os.path.join(data_root(), "ledger")))


def ledger_repo() -> str:
    return str(_get("ledger_repo", names("LEDGER_REPO"), ""))


def tool_path() -> str:
    return str(_get("tool_path", names("TOOL_PATH"), lambda: os.environ.get("PATH", "")))


def tool_env(base: dict | None = None) -> dict:
    """An environment whose PATH is `tool_path` ahead of the current PATH (duplicates dropped,
    first occurrence wins, so lookup order is unchanged)."""
    env = dict(os.environ if base is None else base)
    seen, parts = set(), []
    for p in (tool_path() + ":" + env.get("PATH", "")).split(":"):
        if p and p not in seen:
            seen.add(p)
            parts.append(p)
    env["PATH"] = ":".join(parts)
    return env


def lease_cmd() -> str:
    return _path(_get("lease_cmd", names("LEASE_CMD") + ("GPU_LEASE_CMD",), ""))


def default_repos() -> list[str]:
    v = file_values().get("default_repos") or []
    return [str(x) for x in v] if isinstance(v, list) else []


def cli() -> str:
    """The command line that runs the SHOAL CLI, as briefs, docs and units print it: `shoal`
    when that console script is on PATH, else `python3 <this package>/cli.py`."""
    def dflt():
        if shutil.which("shoal"):
            return "shoal"
        return f"python3 {os.path.join(PKG_DIR, 'cli.py')}"
    return str(_get("cli", names("CLI"), dflt))


def cli_exec() -> tuple[str, str]:
    """(interpreter-or-executable, script) for a unit ExecStart line. A `python3 <script>` cli
    runs under this interpreter (sys.executable); any other cli runs as its own executable
    (resolved on the tool PATH) with no separate script."""
    toks = shlex.split(cli())
    if toks and os.path.basename(toks[0]).startswith("python") and len(toks) > 1:
        return sys.executable, " ".join(toks[1:])
    exe = shutil.which(toks[0], path=tool_env()["PATH"]) if toks else None
    return (exe or (toks[0] if toks else sys.executable)), " ".join(toks[1:])


def cli_pkg_dir() -> str:
    """The package directory the cli runs from (units reference sibling scripts there)."""
    _, script = cli_exec()
    if script and script.endswith(".py") and os.path.isabs(script):
        return os.path.dirname(script)
    return PKG_DIR


def psi_out() -> str:
    return _path(_get("psi_out", names("PSI_OUT"),
                      lambda: os.path.join(data_root(), "_instruments", "psi_events.jsonl")))


def skill_homes() -> list[str]:
    """Harness skill homes that receive this package's skills: `skill_homes` in the host config,
    else the standard homes that exist (Claude Code, the neutral ~/.agents, opencode)."""
    v = _get("skill_homes", names("SKILL_HOMES"), None)
    if v:
        homes = v if isinstance(v, list) else str(v).split(os.pathsep)
        return [os.path.expanduser(h) for h in homes]
    std = ["~/.claude/skills", "~/.agents/skills", "~/.config/opencode/skills"]
    return [p for p in (os.path.expanduser(h) for h in std) if os.path.isdir(p)]


def server() -> str:
    import socket
    return str(_get("server", names("SERVER"), socket.gethostname))


def timezone() -> str:
    """IANA zone for local wall-clock display (telemetry `ts_pdt`, report times)."""
    def dflt():
        for cand in (os.environ.get("TZ", ""),):
            if "/" in cand:
                return cand.lstrip(":")
        try:
            with open("/etc/timezone") as fh:
                v = fh.read().strip()
            if "/" in v:
                return v
        except OSError:
            pass
        return "UTC"
    return str(_get("timezone", names("TZ"), dflt))


def tzinfo():
    """A tzinfo for `timezone()`, or None when zoneinfo cannot resolve it."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(timezone())
    except Exception:  # noqa: BLE001
        return None


def default_owner() -> str:
    """The owner stripped from item keys (`<owner>/<repo>#<n>` -> `<repo>#<n>`)."""
    return str(_get("default_owner", names("DEFAULT_OWNER"), "")).lower()


def azure_org() -> str:
    return str(_get("azure_org", names("AZURE_ORG"), ""))


def azure_project() -> str:
    return str(_get("azure_project", names("AZURE_PROJECT"), ""))


def summary() -> dict:
    """Every resolved setting (doctor prints it)."""
    return {"config_file": config_path() + ("" if os.path.isfile(config_path()) else " (absent)"),
            "data_root": data_root(), "ledger_dir": ledger_dir(), "ledger_repo": ledger_repo(),
            "tool_path": tool_path(), "lease_cmd": lease_cmd(), "default_repos": default_repos(),
            "cli": cli(), "psi_out": psi_out()}


if __name__ == "__main__":
    for k, v in summary().items():
        print(f"{k} = {v}")
