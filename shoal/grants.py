#!/usr/bin/env python3
"""Signed standing grants: maintainer authorizations that travel with a role (design:
docs/decisions.md).

    shoal grant  --shoal H [--role R] --scope TEXT [--kind TAG] [--until ISO] [--dry-run]
    shoal revoke --shoal H --id GRANT_ID [--dry-run]
    shoal grants --shoal H [--role R] [--json] [inspect GRANT_ID]

A grant is a ledger event (type `grant`, writer `user` only) carrying an ssh signature
(`ssh-keygen -Y sign -n shoal-grant`) over its canonical form. Readers verify against the host's
`allowed_signers` and ignore anything unverified, so a grant holds on every host and in every
harness, and forging one is noisy (ledger-lint) rather than useful. Everything here runs only
behind spec feature `grants`; with it off, no grant is verified, shown, or written.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

NS = "shoal-grant"
_VERIFIED: dict[str, bool] = {}


def enabled(spec) -> bool:
    return bool((getattr(spec, "features", {}) or {}).get("grants"))


def key_path() -> str:
    return config.grant_key()


def signers_path() -> str:
    return config.grant_allowed_signers()


def canonical(ev: dict) -> bytes:
    """The signed bytes: the event without `sig` and read-time keys, keys sorted."""
    d = {k: v for k, v in ev.items() if k != "sig" and not k.startswith("_")}
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sshkeygen() -> str | None:
    import shutil
    return shutil.which("ssh-keygen", path=config.tool_env()["PATH"]) or shutil.which("ssh-keygen")


def sign(ev: dict, key: str) -> str:
    """An armored signature over canonical(ev). Raises ValueError with ssh-keygen's complaint."""
    kg = _sshkeygen()
    if not kg:
        raise ValueError("ssh-keygen not found on the tool PATH")
    if not key or not os.path.exists(key):
        raise ValueError(f"no grant signing key at {key or '(unset)'} (host config grant_key)")
    with tempfile.TemporaryDirectory() as td:
        msg = os.path.join(td, "grant")
        open(msg, "wb").write(canonical(ev))
        r = subprocess.run([kg, "-Y", "sign", "-f", key, "-n", NS, msg],
                           capture_output=True, text=True, env=config.tool_env())
        if r.returncode != 0:
            raise ValueError(f"ssh-keygen sign failed: {(r.stderr or r.stdout).strip()[:200]}")
        return open(msg + ".sig").read()


def verify(ev: dict, signers: str | None = None) -> bool:
    """True only when the event's signature checks against allowed_signers. Fail-closed:
    no ssh-keygen, no signers file, or no signature all read as unverified."""
    sig = ev.get("sig")
    if not sig:
        return False
    signers = signers or signers_path()
    import hashlib
    cache_key = f"{ev.get('id', '')}|{signers}|{hashlib.sha256(canonical(ev) + sig.encode()).hexdigest()[:12]}"
    cached = _VERIFIED.get(cache_key)
    if cached is not None:
        return cached
    ok = False
    kg = _sshkeygen()
    if kg and signers and os.path.exists(signers):
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "grant.sig")
            open(sf, "w").write(sig if sig.endswith("\n") else sig + "\n")
            r = subprocess.run([kg, "-Y", "verify", "-f", signers, "-I", NS, "-n", NS, "-s", sf],
                               input=canonical(ev), capture_output=True, env=config.tool_env())
            ok = r.returncode == 0
    _VERIFIED[cache_key] = ok
    return ok


def key_loaded(key: str) -> bool:
    """True when the signing key is loaded in ssh-agent (any same-user session could use it)."""
    try:
        pub = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", key], capture_output=True, text=True,
                             env=config.tool_env())
        want = pub.stdout.split()[1] if pub.returncode == 0 and len(pub.stdout.split()) > 1 else ""
        lst = subprocess.run(["ssh-add", "-L"], capture_output=True, text=True, env=config.tool_env())
        return bool(want) and want in lst.stdout
    except OSError:
        return False


def live_grants(view, spec, now: str = "") -> list[dict]:
    """Verified, unexpired, unrevoked grants from a folded View (feature off: none)."""
    if spec is None or not enabled(spec):
        return []
    from ledger import iso
    now = now or iso()
    revoked = {e.get("grant") for e in view.grant_events
               if e.get("state") == "revoked" and verify(e)}
    out = []
    for e in view.grant_events:
        if e.get("state") != "granted" or e["id"] in revoked:
            continue
        if e.get("until") and str(e["until"]) <= now:
            continue
        if verify(e):
            out.append(e)
    return sorted(out, key=lambda e: (e.get("role") != "*", e["ts"]))


def grants_for(view, spec, slug: str) -> list[dict]:
    return [g for g in live_grants(view, spec) if g.get("role") in ("*", slug)]


def state_of(view, spec, ev: dict, now: str = "") -> str:
    """active | expired | revoked | unverified, for listings (verification runs regardless of the flag)."""
    from ledger import iso
    if not verify(ev):
        return "unverified"
    if ev.get("state") == "revoked":
        return "revoked"
    revoked = {e.get("grant") for e in view.grant_events if e.get("state") == "revoked" and verify(e)}
    if ev["id"] in revoked:
        return "revoked"
    if ev.get("until") and str(ev["until"]) <= (now or iso()):
        return "expired"
    return "active"


def grants_lines(view, spec, slug: str) -> list[str]:
    """The standing-grants block briefs and `next` print (empty when the feature is off or none apply)."""
    mine = grants_for(view, spec, slug)
    if not mine:
        return []
    out = ["STANDING GRANTS (verified signatures; these authorizations are already yours, do not re-ask):"]
    for g in mine:
        level = "shoal" if g.get("role") == "*" else f"role {g.get('role')}"
        until = f" until {g['until']}" if g.get("until") else ""
        kind = f" [{g['kind']}]" if g.get("kind") else ""
        out.append(f"  {g['id']} ({level}{kind}) {g.get('scope', '')}{until}")
    return out


def covered(grants_needed: str, live: list[dict]) -> bool:
    """A grants-needed bullet ending `[grant: <kind-or-id>]` is covered by a live grant with that kind or id."""
    import re
    m = re.search(r"\[grant:\s*([^\]]+)\]\s*$", grants_needed.strip())
    if not m:
        return False
    mark = m.group(1).strip()
    return any(mark in (g.get("kind") or "", g.get("id") or "") for g in live)


# ---- CLI ---------------------------------------------------------------------

def _load(a):
    from ledger import Ledger, LedgerError, View
    from roles import ShoalSpec, find_spec
    spec = ShoalSpec.load(find_spec(a.sid, getattr(a, "spec", "")))
    led = Ledger(spec.sid, root=getattr(a, "ledger_dir", "") or spec.ledger_dir or None,
                 sync=not getattr(a, "no_sync", False))
    if not led.exists():
        raise LedgerError(f"no ledger for {spec.sid} at {led.ldir}")
    if led.sync:
        led.pull()
    return spec, led, View(led, spec)


def _require_enabled(spec) -> bool:
    if enabled(spec):
        return True
    print(f"ERROR: signed grants are off for {spec.sid} (enable with "
          f"`shoal upgrade --spec <spec> --enable grants`)")
    return False


def _sign_into(ev: dict) -> None:
    ev["sig"] = sign(ev, key_path())


def cmd_grant(a) -> int:
    from ledger import LedgerError
    try:
        spec, led, view = _load(a)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    if not _require_enabled(spec):
        return 1
    role = a.role or "*"
    if role != "*" and spec.role(role) is None:
        print(f"ERROR: {role} is not a role in {spec.sid}")
        return 1
    key = key_path()
    if not a.dry_run and key_loaded(key):
        print(f"ERROR: the grant key {key} is loaded in ssh-agent, so any same-user session could sign "
              f"with it; unload it (ssh-add -d) or use a touch key")
        return 1
    fields = dict(state="granted", role=role, scope=a.scope, kind=a.kind or None, until=a.until or None)
    if a.dry_run:
        print(f"[dry-run] would sign and append grant: role={role} scope={a.scope!r} "
              f"kind={a.kind or '-'} until={a.until or 'revoked'} (key {key or 'unset'})")
        return 0
    try:
        ev = led.append("user", "grant", _finalize=_sign_into, **fields)
    except Exception as exc:  # noqa: BLE001 - signing or the append itself
        print(f"ERROR: {exc}")
        return 1
    if not verify(ev):
        print(f"ERROR: grant {ev['id']} was written but does not verify against "
              f"{signers_path()}; roles will ignore it (check grant_allowed_signers)")
        return 1
    print(f"grant {ev['id']} [{role}] {a.scope} (signed, verified)")
    return 0


def cmd_revoke(a) -> int:
    from ledger import LedgerError
    try:
        spec, led, view = _load(a)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    if not _require_enabled(spec):
        return 1
    target = next((e for e in view.grant_events if e.get("id") == a.gid), None)
    if target is None:
        print(f"ERROR: no grant {a.gid} in {spec.sid}")
        return 1
    if a.dry_run:
        print(f"[dry-run] would revoke {a.gid}: [{target.get('role')}] {target.get('scope', '')}")
        return 0
    if key_loaded(key_path()):
        print("ERROR: the grant key is loaded in ssh-agent; unload it first (ssh-add -d)")
        return 1
    try:
        ev = led.append("user", "grant", _finalize=_sign_into,
                        state="revoked", grant=a.gid, role=target.get("role", "*"))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return 1
    print(f"revoked {a.gid} [{ev['id']}]")
    return 0


def cmd_grants(a) -> int:
    from ledger import LedgerError
    try:
        spec, led, view = _load(a)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    evs = [e for e in view.grant_events if e.get("state") == "granted"]
    if a.role:
        evs = [e for e in evs if e.get("role") in ("*", a.role)]
    if a.inspect:
        ev = next((e for e in view.grant_events if e.get("id") == a.inspect), None)
        if ev is None:
            print(f"ERROR: no grant {a.inspect} in {spec.sid}")
            return 1
        print(json.dumps({k: v for k, v in ev.items() if not k.startswith("_")}, indent=2))
        print(f"state: {state_of(view, spec, ev)} (signers: {signers_path()})")
        return 0
    rows = [{"id": e["id"], "level": "shoal" if e.get("role") == "*" else "role",
             "role": e.get("role", "*"), "scope": e.get("scope", ""), "kind": e.get("kind"),
             "issued": e["ts"], "until": e.get("until"), "state": state_of(view, spec, e)} for e in evs]
    if a.json:
        print(json.dumps({"shoal": spec.sid, "grants_enabled": enabled(spec), "grants": rows}, indent=2))
        return 0
    if not rows:
        print(f"no grants in {spec.sid}" + ("" if enabled(spec) else " (feature grants is off)"))
        return 0
    for r in rows:
        print(f"{r['id']}  {r['level']:<5} {r['role']:<24} {r['state']:<10} "
              f"{('until ' + r['until']) if r['until'] else 'standing':<24} {r['scope']}")
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("grant", help="sign a standing grant (maintainer only: needs the grant key)")
    config.add_id_arg(p, required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--ledger-dir", default="")
    p.add_argument("--role", default="", help="role slug; omitted: a shoal-level grant")
    p.add_argument("--scope", required=True, help="what the grant authorizes (maintainer prose)")
    p.add_argument("--kind", default="", help="optional short tag (gpu-block, review-comment, ...)")
    p.add_argument("--until", default="", help="ISO UTC expiry; omitted: until revoked")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser("revoke", help="sign a revocation of a standing grant")
    config.add_id_arg(p, required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--ledger-dir", default="")
    p.add_argument("--id", dest="gid", required=True, help="the grant id to revoke")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("grants", help="list standing grants and their verification state (read-only)")
    config.add_id_arg(p, required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--ledger-dir", default="")
    p.add_argument("--role", default="", help="only this role's grants (plus shoal-level)")
    p.add_argument("--inspect", default="", help="print one grant record and its state")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-sync", action="store_true")
    p.set_defaults(func=cmd_grants)
