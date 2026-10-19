#!/usr/bin/env python3
"""systemd --user units for shoal watchers (replaces the cron lines).

One-shot services + Persistent timers, journal logging, explicit tool PATH
in the unit (systemd's user PATH lacks openchamber, claude, node), no manual
cron. Regenerate per shoal generation (session ids change); stale units for a
retired shoal are removed by `uninstall-systemd --shoal <old-id>`.

Every role runs its own `ledger-watch`; lanes/pipeline/heartbeat notify
through the ledger (`ledger:<id>` target), so no unit needs a session id.

Units land in ~/.config/systemd/user/ as shoal-<id>-<job>.{service,timer}.
`install-systemd --enable` runs daemon-reload + `enable --now` on every
timer it wrote, so a launch is one command. Jobs:
  lanes          every :0/5    lane transitions + SERVER-RESPAWNED
  pipeline       every :0/10   depth-zero starvation -> PIPELINE-WATCH
  heartbeat      every :0/5    STALL-SUSPECT (deduped) unless a wait is proven (without features.health)
  health-a/-b    :0/5, :2/5    health quorum (features.health): policy ladder, per-ecosystem wakes
  digest         every :3/10   one board read per shoal -> board_digest.json (features.digest)
  reconcile      04,16:07      GitHub ground truth into telemetry
  weekly-report  Mon 05:11     rolling 168h report (md + html) for the review
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

# units run the configured cli (config `cli`), so units written from any checkout point
# at the one the host config names
_CLI_EXE, SHOAL_CLI = config.cli_exec()
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
TOOL_PATH = config.tool_path()


def unit_name(sid: str, job: str, ext: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in sid)
    return f"shoal-{safe}-{job}.{ext}"


def service_unit(description: str, repo_dir: str, argv: list[str],
                 exe: str = "") -> str:
    if exe:  # a sibling python script (reconcile)
        head = f"{sys.executable} {exe}"
    else:
        head = f"{_CLI_EXE} {SHOAL_CLI}".rstrip()
    env = "".join(f"Environment={k}={v}\n" for k, v in sorted(config.tool_env_extra().items()))
    return (f"[Unit]\nDescription={description}\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={repo_dir}\n"
            f"Environment=PATH={TOOL_PATH}\n" + env
            + f"ExecStart={head} {' '.join(argv)}\n"
            f"StandardOutput=journal\nStandardError=journal\n")


def timer_unit(description: str, on_calendar: str) -> str:
    return (f"[Unit]\nDescription={description}\n"
            f"[Timer]\nOnCalendar={on_calendar}\nPersistent=true\n"
            f"[Install]\nWantedBy=timers.target\n")


def jobs_for(spec, spec_path: str) -> list[tuple[str, str, list[str], str, str]]:
    """(job, description, argv, OnCalendar, exe)."""
    lanes = [r.session for r in spec.roles
             if r.ecosystem == "opencode" and r.session]
    jobs = []
    target = f"ledger:{spec.sid}"
    if lanes:
        jobs.append(("lanes", f"shoal {spec.sid} lanes watch (server respawn -> ledger)",
                     ["watch", "--shoal", spec.sid, "lanes", target, spec.dir] + lanes,
                     "*:0/5", ""))
    jobs.append(("pipeline", f"shoal {spec.sid} pipeline watch (starvation -> ledger)",
                 ["watch", "--shoal", spec.sid, "pipeline", target, spec.dir],
                 "*:0/10", ""))
    if (getattr(spec, "features", {}) or {}).get("health"):
        # the health quorum: two identical, offset instances (architecture doc §6)
        jobs.append(("health-a", f"shoal {spec.sid} health quorum (a)",
                     ["health", "--spec", spec_path, "--instance", "a"], "*:0/5", ""))
        jobs.append(("health-b", f"shoal {spec.sid} health quorum (b)",
                     ["health", "--spec", spec_path, "--instance", "b"], "*:2/5", ""))
    else:
        jobs.append(("heartbeat", f"shoal {spec.sid} stall daemon",
                     ["heartbeat", "--spec", spec_path, "--threshold-min", "60"],
                     "*:0/5", ""))
    if (getattr(spec, "features", {}) or {}).get("digest") and spec.sources:
        jobs.append(("digest", f"shoal {spec.sid} board digest (one board read per shoal)",
                     ["digest", "--shoal", spec.sid, "--spec", spec_path, "--refresh"], "*:3/10", ""))
    jobs.append(("reconcile", f"shoal {spec.sid} reconcile (twice daily)",
                 ["--shoal", spec.sid, "--spec", spec_path], "04,16:07",
                 os.path.join(config.cli_pkg_dir(), "telemetry", "shoal-reconcile.py")))
    jobs.append(("weekly-report", f"shoal {spec.sid} weekly report",
                 ["report", "--spec", spec_path, "--hours", "168"], "Mon 05:11", ""))
    return jobs


def cmd_install(spec, spec_path: str,
                dry_run: bool = False, enable: bool = False) -> int:
    from roles import ShoalSpec
    assert isinstance(spec, ShoalSpec)
    if not any(r.session for r in spec.roles):
        print("spec has no live session ids — run start/restart live first (ids are saved back)")
        return 1
    os.makedirs(UNIT_DIR, exist_ok=True)
    timers = []
    for job, desc, argv, cal, exe in jobs_for(spec, spec_path):
        svc = service_unit(desc, spec.dir, argv, exe)
        sn, tn = unit_name(spec.sid, job, "service"), unit_name(spec.sid, job, "timer")
        if dry_run:
            print(f"[dry-run] {sn} + {tn} ({cal}): {os.path.basename(exe or SHOAL_CLI or _CLI_EXE)} {' '.join(argv)[:90]}")
            continue
        with open(os.path.join(UNIT_DIR, sn), "w") as fh:
            fh.write(svc)
        with open(os.path.join(UNIT_DIR, tn), "w") as fh:
            fh.write(timer_unit(desc + " timer", cal))
        timers.append(tn)
        print(f"wrote {sn} + {tn} ({cal})")
    if dry_run:
        return 0
    if enable:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        for tn in timers:
            r = subprocess.run(["systemctl", "--user", "enable", "--now", tn],
                               capture_output=True, text=True)
            print(f"{'enabled' if r.returncode == 0 else 'ENABLE FAILED'}: {tn}"
                  + ("" if r.returncode == 0 else f" ({(r.stdout + r.stderr).strip()[-120:]})"))
        subprocess.run(["systemctl", "--user", "list-timers", "--no-pager"], check=False)
    else:
        print("enable with:\n  systemctl --user daemon-reload\n"
              "  for t in ~/.config/systemd/user/"
              f"shoal-{spec.sid}-*.timer; do systemctl --user enable --now "
              "\"$(basename $t)\"; done\n(or re-run install-systemd --enable)")
    return 0


def cmd_uninstall(sid: str, dry_run: bool = False) -> int:
    removed = []
    for path in sorted(glob.glob(os.path.join(UNIT_DIR, f"shoal-{sid}-*"))):
        base = os.path.basename(path)
        if dry_run:
            print(f"[dry-run] disable + remove {base}")
            continue
        if base.endswith(".timer"):
            subprocess.run(["systemctl", "--user", "disable", "--now", base],
                           capture_output=True)
        os.remove(path)
        removed.append(base)
    if not dry_run:
        print(f"removed {len(removed)} unit(s) for {sid}: {removed}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return 0
