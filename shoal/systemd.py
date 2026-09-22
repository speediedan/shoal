#!/usr/bin/env python3
"""systemd --user units for HAG watchers (replaces the cron lines).

One-shot services + Persistent timers, journal logging, explicit tool PATH
in the unit (systemd's user PATH lacks openchamber, claude, node), no manual
cron. Regenerate per HAG generation (session ids change); stale units for a
retired HAG are removed by `uninstall-systemd --hag <old-id>`.

Ledger mode (2026-09-20): no queue-<file> jobs (every role runs its own
`ledger-watch`); lanes/pipeline/heartbeat notify through the ledger
(`ledger:<hag>` target) so they need no coordinator session id. Legacy
(no ledger in the spec) keeps the coordinator-addressed shape below.

Units land in ~/.config/systemd/user/ as hag-<hag>-<job>.{service,timer}.
`install-systemd --enable` runs daemon-reload + `enable --now` on every
timer it wrote, so a launch is one command. Jobs:
  queue-<file>   every :0/10   STATUS change -> QUEUE-WATCH to the coordinator
  lanes          every :0/5    lane transitions + SERVER-RESPAWNED
  pipeline       every :0/10   depth-zero starvation -> PIPELINE-WATCH
  heartbeat      every :0/5    STALL-SUSPECT (deduped) unless a wait is proven
  digest         every :3/10   one board read per HAG -> board_digest.json (features.digest)
  reconcile      04,16:07      GitHub ground truth into telemetry
  weekly-report  Mon 05:11     rolling 168h report (md + html) for the review
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HAGPY = os.path.join(HERE, "hag.py")
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
TOOL_PATH = ("/home/speediedan/.nvm/versions/node/v24.13.0/bin:"
             "/home/speediedan/.bun/bin:/usr/local/bin:/usr/bin:/bin")


def unit_name(hag: str, job: str, ext: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in hag)
    return f"hag-{safe}-{job}.{ext}"


def service_unit(description: str, repo_dir: str, argv: list[str],
                 exe: str = "") -> str:
    python = sys.executable
    exe = exe or HAGPY
    return (f"[Unit]\nDescription={description}\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={repo_dir}\n"
            f"Environment=PATH={TOOL_PATH}\n"
            f"ExecStart={python} {exe} {' '.join(argv)}\n"
            f"StandardOutput=journal\nStandardError=journal\n")


def timer_unit(description: str, on_calendar: str) -> str:
    return (f"[Unit]\nDescription={description}\n"
            f"[Timer]\nOnCalendar={on_calendar}\nPersistent=true\n"
            f"[Install]\nWantedBy=timers.target\n")


def jobs_for(spec, spec_path: str, coord_id: str) -> list[tuple[str, str, list[str], str, str]]:
    """(job, description, argv, OnCalendar, exe)."""
    lanes = [r.session for r in spec.roles
             if not r.coordinator and r.ecosystem == "opencode" and r.session]
    consumer_q = [r.queue for r in spec.roles if r.queue]
    jobs = []
    if spec.ledger_mode:
        target = f"ledger:{spec.hag}"
        if lanes:
            jobs.append(("lanes", f"HAG {spec.hag} lanes watch (server respawn -> ledger)",
                         ["watch", "--hag", spec.hag, "lanes", target, spec.dir] + lanes,
                         "*:0/5", ""))
        jobs.append(("pipeline", f"HAG {spec.hag} pipeline watch (starvation -> ledger)",
                     ["watch", "--hag", spec.hag, "pipeline", target, spec.dir],
                     "*:0/10", ""))
        coord_id = ""  # heartbeat delivers through the ledger
    elif coord_id:
        for q in consumer_q:
            jobs.append((f"queue-{os.path.basename(q).replace('.md', '')}",
                         f"HAG {spec.hag} queue watch {os.path.basename(q)}",
                         ["watch", "--hag", spec.hag, "queue", q, coord_id, spec.dir],
                         "*:0/10", ""))
        if lanes:
            jobs.append(("lanes", f"HAG {spec.hag} lanes watch",
                         ["watch", "--hag", spec.hag, "lanes", coord_id, spec.dir] + lanes,
                         "*:0/5", ""))
        jobs.append(("pipeline", f"HAG {spec.hag} pipeline watch",
                     ["watch", "--hag", spec.hag, "pipeline", coord_id, spec.dir],
                     "*:0/10", ""))
    jobs.append(("heartbeat", f"HAG {spec.hag} stall daemon",
                 ["heartbeat", "--spec", spec_path, "--threshold-min", "60"]
                 + (["--notify", coord_id] if coord_id else []),
                 "*:0/5", ""))
    if (getattr(spec, "features", {}) or {}).get("digest") and spec.sources:
        jobs.append(("digest", f"HAG {spec.hag} board digest (one board read per HAG)",
                     ["digest", "--hag", spec.hag, "--spec", spec_path, "--refresh"], "*:3/10", ""))
    jobs.append(("reconcile", f"HAG {spec.hag} reconcile (twice daily)",
                 ["--hag", spec.hag, "--spec", spec_path], "04,16:07",
                 os.path.join(HERE, "telemetry", "hag-reconcile.py")))
    jobs.append(("weekly-report", f"HAG {spec.hag} weekly report",
                 ["report", "--spec", spec_path, "--hours", "168"], "Mon 05:11", ""))
    return jobs


def cmd_install(spec, spec_path: str, coord_id: str,
                dry_run: bool = False, enable: bool = False) -> int:
    from roles import HagSpec
    assert isinstance(spec, HagSpec)
    if not coord_id and not spec.flattened and not spec.ledger_mode:
        print("need a coordinator session id (--coord-id, or a spec saved after a live start)")
        return 1
    if not any(r.session for r in spec.roles):
        print("spec has no live session ids — run start/restart live first (ids are saved back)")
        return 1
    os.makedirs(UNIT_DIR, exist_ok=True)
    timers = []
    for job, desc, argv, cal, exe in jobs_for(spec, spec_path, coord_id):
        svc = service_unit(desc, spec.dir, argv, exe)
        sn, tn = unit_name(spec.hag, job, "service"), unit_name(spec.hag, job, "timer")
        if dry_run:
            print(f"[dry-run] {sn} + {tn} ({cal}): {os.path.basename(exe or HAGPY)} {' '.join(argv)[:90]}")
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
              f"hag-{spec.hag}-*.timer; do systemctl --user enable --now "
              "\"$(basename $t)\"; done\n(or re-run install-systemd --enable)")
    return 0


def cmd_uninstall(hag: str, dry_run: bool = False) -> int:
    removed = []
    for path in sorted(glob.glob(os.path.join(UNIT_DIR, f"hag-{hag}-*"))):
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
        print(f"removed {len(removed)} unit(s) for {hag}: {removed}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return 0
