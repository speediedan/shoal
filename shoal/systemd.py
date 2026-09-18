#!/usr/bin/env python3
"""systemd --user units for HAG watchers (replaces the cron lines).

One-shot services + Persistent timers, journal logging, no manual cron.
Regenerate per HAG generation (ids change); stale units for a retired HAG
are removed by `uninstall-systemd --hag <old-id>`.

Units land in ~/.config/systemd/user/ as hag-<hag>-<job>.{service,timer}.
The operator enables them once:
  systemctl --user daemon-reload
  systemctl --user enable --now hag-<hag>-<job>.timer   (per job, or all)
Linger is already on for this host, so timers survive logout.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HAGPY = os.path.join(HERE, "hag.py")
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")


def unit_name(hag: str, job: str, ext: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in hag)
    return f"hag-{safe}-{job}.{ext}"


def service_unit(description: str, repo_dir: str, argv: list[str]) -> str:
    python = sys.executable
    return (f"[Unit]\nDescription={description}\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={repo_dir}\n"
            f"ExecStart={python} {HAGPY} {' '.join(argv)}\n"
            f"StandardOutput=journal\nStandardError=journal\n")


def timer_unit(description: str, on_calendar: str) -> str:
    return (f"[Unit]\nDescription={description}\n"
            f"[Timer]\nOnCalendar={on_calendar}\nPersistent=true\n"
            f"[Install]\nWantedBy=timers.target\n")


def jobs_for(spec, spec_path: str, coord_id: str) -> list[tuple[str, str, list[str], str]]:
    lanes = [r.session for r in spec.roles
             if not r.coordinator and r.ecosystem == "opencode" and r.session]
    consumer_q = [r.queue for r in spec.roles if r.queue]
    jobs = []
    for q in consumer_q:
        jobs.append((f"queue-{os.path.basename(q)}",
                     f"HAG {spec.hag} queue watch {q}",
                     ["watch", "--hag", spec.hag, "queue", q, coord_id,
                      spec.dir], "*:0/10"))
    if lanes:
        jobs.append(("lanes", f"HAG {spec.hag} lanes watch",
                     ["watch", "--hag", spec.hag, "lanes", coord_id, spec.dir]
                     + lanes, "*:0/5"))
    jobs.append(("pipeline", f"HAG {spec.hag} pipeline watch",
                 ["watch", "--hag", spec.hag, "pipeline", coord_id, spec.dir],
                 "*:0/10"))
    jobs.append(("heartbeat", f"HAG {spec.hag} stall daemon",
                 ["heartbeat", "--spec", spec_path,
                  "--threshold-min", "60", "--notify", coord_id],
                 "*:0/5"))
    jobs.append(("reconcile", f"HAG {spec.hag} nightly reconcile",
                 ["__reconcile__", spec.hag], "04:07"))
    return jobs


def cmd_install(spec, spec_path: str, coord_id: str,
                dry_run: bool = False) -> int:
    from roles import HagSpec
    assert isinstance(spec, HagSpec)
    if not coord_id:
        print("need a coordinator session id (--coord-id, or save-spec first)")
        return 1
    if not any(r.session for r in spec.roles):
        print("spec has no live session ids — run restart --save-spec first")
        return 1
    os.makedirs(UNIT_DIR, exist_ok=True)
    for job, desc, argv, cal in jobs_for(spec, spec_path, coord_id):
        if argv[0] == "__reconcile__":
            import subprocess as _sp
            rec = os.path.join(HERE, "telemetry", "hag-reconcile.py")
            svc = (f"[Unit]\nDescription={desc}\n[Service]\nType=oneshot\n"
                   f"ExecStart={sys.executable} {rec} --hag {spec.hag}\n"
                   f"StandardOutput=journal\nStandardError=journal\n")
        else:
            svc = service_unit(desc, spec.dir, argv)
        sn, tn = unit_name(spec.hag, job, "service"), unit_name(spec.hag, job, "timer")
        if dry_run:
            print(f"[dry-run] {sn} + {tn} ({cal})")
            continue
        with open(os.path.join(UNIT_DIR, sn), "w") as fh:
            fh.write(svc)
        with open(os.path.join(UNIT_DIR, tn), "w") as fh:
            fh.write(timer_unit(desc + " timer", cal))
        print(f"wrote {sn} + {tn} ({cal})")
    if not dry_run:
        print("enable with:\n  systemctl --user daemon-reload\n"
              "  for t in ~/.config/systemd/user/"
              f"hag-{spec.hag}-*.timer; do systemctl --user enable --now "
              "\"$(basename $t)\"; done")
    return 0


def cmd_uninstall(hag: str) -> int:
    import glob as _g
    import subprocess as _sp
    removed = []
    for path in _g.glob(os.path.join(UNIT_DIR, f"hag-{hag}-*")):
        base = os.path.basename(path)
        if base.endswith(".timer"):
            _sp.run(["systemctl", "--user", "disable", "--now", base],
                    capture_output=True)
        os.remove(path)
        removed.append(base)
    print(f"removed {len(removed)} unit(s) for {hag}: {removed}")
    _sp.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return 0
