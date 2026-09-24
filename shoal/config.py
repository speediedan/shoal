#!/usr/bin/env python3
"""Host configuration: the one place a host-specific value is resolved.

Every setting resolves in the same order: an environment variable, then the host
config file, then a generic default that works on any host. The host config file is
`$HAG_CONFIG` when set, else `<package dir>/config/host.toml` (gitignored; see
`config/host.example.toml`). TOML is read with `tomllib` (Python 3.11+) or `tomli`;
when neither is importable the file is skipped with one warning on stderr.

  key            env var                         generic default
  data_root      HAG_DATA_ROOT                   $XDG_DATA_HOME/hag (else ~/.local/share/hag)
  ledger_dir     HAG_LEDGER_DIR                  <data_root>/ledger
  ledger_repo    HAG_LEDGER_REPO                 "" (start / ledger-init then need --ledger-repo)
  tool_path      HAG_TOOL_PATH                   this process's PATH
  lease_cmd      HAG_LEASE_CMD, GPU_LEASE_CMD    "" (no lease provider: lease waits read unknown)
  default_repos  (none)                          [] (a spec's own `repos` still wins)
  cli            HAG_CLI                         python3 <this package>/hag.py
  psi_out        HAG_PSI_OUT                     <data_root>/_instruments/psi_events.jsonl

An empty environment variable counts as unset. Values are read at call time, so a test
that exports a variable before importing a module sees it.
"""
from __future__ import annotations

import os
import shlex
import shutil
import sys

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE: dict[str, dict] = {}
_WARNED: set[str] = set()


def config_path() -> str:
    return os.environ.get("HAG_CONFIG") or os.path.join(PKG_DIR, "config", "host.toml")


def _warn(key: str, msg: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        print(f"hag config: {msg}", file=sys.stderr)


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
        return os.path.join(xdg, "hag")
    return _path(_get("data_root", ("HAG_DATA_ROOT",), dflt))


def ledger_dir() -> str:
    return _path(_get("ledger_dir", ("HAG_LEDGER_DIR",), lambda: os.path.join(data_root(), "ledger")))


def ledger_repo() -> str:
    return str(_get("ledger_repo", ("HAG_LEDGER_REPO",), ""))


def tool_path() -> str:
    return str(_get("tool_path", ("HAG_TOOL_PATH",), lambda: os.environ.get("PATH", "")))


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
    return _path(_get("lease_cmd", ("HAG_LEASE_CMD", "GPU_LEASE_CMD"), ""))


def default_repos() -> list[str]:
    v = file_values().get("default_repos") or []
    return [str(x) for x in v] if isinstance(v, list) else []


def cli() -> str:
    """The command line that runs hag.py, as briefs, docs and units print it."""
    return str(_get("cli", ("HAG_CLI",), lambda: f"python3 {os.path.join(PKG_DIR, 'hag.py')}"))


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
    return _path(_get("psi_out", ("HAG_PSI_OUT",),
                      lambda: os.path.join(data_root(), "_instruments", "psi_events.jsonl")))


def summary() -> dict:
    """Every resolved setting (doctor prints it)."""
    return {"config_file": config_path() + ("" if os.path.isfile(config_path()) else " (absent)"),
            "data_root": data_root(), "ledger_dir": ledger_dir(), "ledger_repo": ledger_repo(),
            "tool_path": tool_path(), "lease_cmd": lease_cmd(), "default_repos": default_repos(),
            "cli": cli(), "psi_out": psi_out()}


if __name__ == "__main__":
    for k, v in summary().items():
        print(f"{k} = {v}")
