#!/usr/bin/env python3
"""psi_watch: log every memory-PSI trigger event the Claude harness reaper would see.

The Claude harness (Bun runtime) arms a Linux PSI trigger ``some 150000 2000000`` (150 ms of
memory stall inside a 2 s window) and emits a ``memoryPressure`` event when it fires; the
background-shell reaper listens for that event. Measured 2026-09-22: the harness holds the
HOST-WIDE ``/proc/pressure/memory`` open (a user cannot arm a trigger on its own session
scope's ``memory.pressure``, EACCES), so any stall anywhere on the host fires every Claude
harness at once. This instrument arms the IDENTICAL trigger on the host root (plus every
cgroup holding a Claude process, where permitted) and appends one JSON line per fired event
with the host memory picture at that instant, so a later kill can be matched to its stall.
Each event records the PSI ``total`` so a spurious fire (total unchanged since the previous
record) is distinguishable from a real stall.

Observability only: it reads procfs/cgroupfs and writes its own log. Run it outside any
harness (``systemd-run --user``) so the reaper it observes cannot reap it.

    psi_watch.py [--out PATH] [--rescan 60] [--tick 600] [--once-probe]

Log records (``kind``): ``armed`` (a trigger was armed on a cgroup), ``event`` (a trigger
fired), ``tick`` (self-tick every --tick seconds; an old tick means a dead watcher, never a
quiet world), ``error``, ``exit`` (verdict line on every designed exit).
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

TRIGGER = b"some 150000 2000000\0"  # byte-identical to the harness runtime's trigger
CG_ROOT = "/sys/fs/cgroup"
DEFAULT_OUT = config.psi_out()
VMSTAT_KEYS = ("allocstall_normal", "allocstall_movable", "pgscan_direct", "pgsteal_direct",
               "pgscan_kswapd", "workingset_refault_file", "workingset_refault_anon",
               "pswpin", "pswpout")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def meminfo() -> dict:
    out = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemFree", "MemAvailable", "Cached", "SwapFree", "Dirty", "Writeback"):
            out[k] = int(v.split()[0])
    return out


def vmstat() -> dict:
    out = {}
    for line in read("/proc/vmstat").splitlines():
        k, _, v = line.partition(" ")
        if k in VMSTAT_KEYS:
            out[k] = int(v)
    return out


def psi(path: str) -> dict:
    out = {}
    for line in read(path).splitlines():
        parts = line.split()
        if parts:
            out[parts[0]] = {kv.split("=")[0]: float(kv.split("=")[1]) for kv in parts[1:]}
    return out


def claude_procs() -> dict[str, list[int]]:
    """cgroup path (relative) -> pids of Claude harness processes in it."""
    found: dict[str, list[int]] = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if "claude-code" not in exe and not exe.endswith("/claude"):
            continue
        cg = read(f"/proc/{pid}/cgroup").strip().split("::", 1)[-1]
        if cg:
            found.setdefault(cg, []).append(int(pid))
    return found


def top_rss(n: int = 5) -> list:
    rows = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        st = read(f"/proc/{pid}/statm").split()
        if len(st) < 2:
            continue
        rows.append((int(st[1]) * os.sysconf("SC_PAGE_SIZE") // 1024, int(pid),
                     read(f"/proc/{pid}/comm").strip()))
    rows.sort(reverse=True)
    return [{"rss_kb": r, "pid": p, "comm": c} for r, p, c in rows[:n]]


class Watch:
    def __init__(self, out: str):
        self.out = out
        os.makedirs(os.path.dirname(out), exist_ok=True)
        self.poll = select.poll()
        self.fds: dict[int, str] = {}      # fd -> label
        self.paths: dict[str, int] = {}    # pressure path -> fd
        self.refused: set[str] = set()     # paths the kernel refused (EACCES): reported once
        self.last_vm = vmstat()
        self.last_top = 0.0
        self.counts: dict[str, int] = {}

    def emit(self, rec: dict) -> None:
        rec = {"ts": now(), **rec}
        with open(self.out, "a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def arm(self, label: str, path: str, pids: list[int] | None = None) -> None:
        if path in self.paths or path in self.refused:
            return
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            os.write(fd, TRIGGER)
        except OSError as exc:
            self.refused.add(path)
            self.emit({"kind": "error", "label": label, "path": path, "error": str(exc)})
            return
        self.poll.register(fd, select.POLLPRI)
        self.fds[fd] = label
        self.paths[path] = fd
        self.emit({"kind": "armed", "label": label, "path": path, "claude_pids": pids or []})

    def disarm_missing(self) -> None:
        for path, fd in list(self.paths.items()):
            if not os.path.exists(path):
                self.poll.unregister(fd)
                os.close(fd)
                self.emit({"kind": "disarmed", "label": self.fds.pop(fd), "path": path})
                del self.paths[path]

    def rescan(self) -> None:
        self.arm("root", "/proc/pressure/memory")
        self.arm("user-1000.slice", f"{CG_ROOT}/user.slice/user-1000.slice/memory.pressure")
        for cg, pids in claude_procs().items():
            self.arm(cg.rsplit("/", 1)[-1], f"{CG_ROOT}{cg}/memory.pressure", pids)
        self.disarm_missing()

    def fired(self, fd: int) -> None:
        label = self.fds.get(fd, "?")
        path = next((p for p, f in self.paths.items() if f == fd), "")
        vm = vmstat()
        delta = {k: vm.get(k, 0) - self.last_vm.get(k, 0) for k in VMSTAT_KEYS}
        self.last_vm = vm
        rec = {"kind": "event", "label": label, "path": path, "psi": psi(path),
               "meminfo_kb": meminfo(), "vmstat_delta": delta}
        if time.time() - self.last_top >= 10:
            rec["top_rss"] = top_rss()
            self.last_top = time.time()
        self.counts[label] = self.counts.get(label, 0) + 1
        self.emit(rec)

    def run(self, rescan_s: int, tick_s: int) -> int:
        stop = {"why": None}

        def on_signal(sig, _frm):
            stop["why"] = signal.Signals(sig).name

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        self.rescan()
        next_rescan = time.time() + rescan_s
        next_tick = time.time()
        while stop["why"] is None:
            for fd, ev in self.poll.poll(1000):
                if ev & select.POLLPRI:
                    self.fired(fd)
                elif ev & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                    self.emit({"kind": "error", "label": self.fds.get(fd), "error": f"poll ev {ev}"})
                    self.poll.unregister(fd)
            t = time.time()
            if t >= next_rescan:
                self.rescan()
                next_rescan = t + rescan_s
            if t >= next_tick:
                self.emit({"kind": "tick", "armed": len(self.fds), "event_counts": self.counts,
                           "meminfo_kb": meminfo()})
                next_tick = t + tick_s
        self.emit({"kind": "exit", "verdict": f"SIGNAL {stop['why']}", "event_counts": self.counts})
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--rescan", type=int, default=60, help="seconds between cgroup rescans")
    ap.add_argument("--tick", type=int, default=600, help="seconds between self-tick records")
    ap.add_argument("--once-probe", action="store_true",
                    help="arm every target, print what was armed, exit (dry-run proof)")
    a = ap.parse_args()
    w = Watch(a.out)
    if a.once_probe:
        w.emit = lambda rec: print(json.dumps({"ts": now(), **rec}))  # type: ignore[method-assign]
        w.rescan()
        print(json.dumps({"kind": "exit", "verdict": "PROBE-OK", "armed": len(w.fds)}))
        return 0 if w.fds else 1
    return w.run(a.rescan, a.tick)


if __name__ == "__main__":
    sys.exit(main())
