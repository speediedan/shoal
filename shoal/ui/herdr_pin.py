#!/usr/bin/env python3
"""The pinned Herdr release every shoal host runs, and its checksum-verified installer (`shoal ui herdr-get`).

Pinned, never auto-updated: Herdr ships several releases a week with frequent breaking changes, and the adapter
checks the socket methods it uses (`shoal ui doctor`). Bump PIN deliberately, lane host first, then run the
adapter's checks; the plugin's `min_herdr_version` follows. Never `herdr update` or the curl|sh installer on a
shoal host: both follow latest.

Layout: $PREFIX/share/herdr/<version>/herdr with a stable symlink at $PREFIX/bin/herdr (PREFIX defaults to
~/.local). A running Herdr server keeps its old binary until it restarts; a client and server of different
compatible versions negotiate. Checksum source: the GitHub release API's per-asset `digest` (sha256), read over
HTTPS and compared against the downloaded asset. No sudo.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.request

PIN = "0.9.3"
REPO = "herdrdev/herdr"


def prefix() -> str:
    return os.path.expanduser(os.environ.get("HERDR_PREFIX") or "~/.local")


def installed(pfx: str = "") -> str:
    """The version of $PREFIX/bin/herdr, or '' when there is none."""
    b = os.path.join(pfx or prefix(), "bin", "herdr")
    try:
        out = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else ""


def asset_name() -> str:
    osname = {"Linux": "linux", "Darwin": "macos"}.get(platform.system())
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine().lower())
    if not osname or not arch:
        raise RuntimeError(f"unsupported platform {platform.system()} {platform.machine()}")
    return f"herdr-{osname}-{arch}"


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "shoal-herdr-get"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https URLs
        return r.read()


def install(version: str = PIN, pfx: str = "") -> str:
    """Install `version`, verified against the release's sha256 digest; returns what was done."""
    pfx = pfx or prefix()
    ver = version.lstrip("v")
    if installed(pfx) == ver:
        return f"already at the pin ({ver})"
    asset = asset_name()
    rel = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/tags/v{ver}", 20))
    digest = next((a.get("digest") or "" for a in rel.get("assets", []) if a.get("name") == asset), "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise RuntimeError(f"no sha256 digest for {asset} in v{ver}")
    data = _get(f"https://github.com/{REPO}/releases/download/v{ver}/{asset}", 300)
    if hashlib.sha256(data).hexdigest() != digest.split(":", 1)[1]:
        raise RuntimeError(f"CHECKSUM FAILED for {asset} v{ver}")
    dest = os.path.join(pfx, "share", "herdr", ver)
    os.makedirs(dest, exist_ok=True)
    os.makedirs(os.path.join(pfx, "bin"), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, os.path.join(dest, "herdr"))
    link = os.path.join(pfx, "bin", "herdr")
    tmpl = link + ".shoal-tmp"
    if os.path.lexists(tmpl):
        os.remove(tmpl)
    os.symlink(os.path.join(dest, "herdr"), tmpl)
    os.replace(tmpl, link)
    return f"installed {installed(pfx) or ver} (sha256 verified)"


def cmd_get(a) -> int:
    """`shoal ui herdr-get [--version X.Y.Z] [--check]`."""
    ver = (a.version or os.environ.get("HERDR_VERSION") or PIN).lstrip("v")
    have = installed()
    print(f"herdr-get: installed={have or 'none'} pin={ver}")
    if a.check:
        return 0 if have == ver else 1
    try:
        print("herdr-get: " + install(ver))
    except Exception as exc:  # noqa: BLE001 - a failed install is reported, never half-done (atomic replace)
        print(f"herdr-get: {exc}", file=sys.stderr)
        return 1
    return 0
