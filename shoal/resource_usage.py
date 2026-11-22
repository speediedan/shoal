"""Host resource observations, lease efficiency and a conservative placement pilot.

This file can also run standalone on a compute host without installing SHOAL.
It never acquires a lease, changes a host mode, or migrates a session.

`watch` runs two paths. The full sample (GPUs, the lease provider, memory; subprocesses allowed) runs every
`--interval` seconds (15) in its own thread and appends to `events-YYYYMMDD.jsonl`. The fast path runs every
`--fast-interval` seconds (2) and reads files only, so it keeps recording while the host is too short of memory to
start a process: memory PSI, MemAvailable/SwapFree/Dirty, per-cgroup memory and the top processes by RSS, appended
to `fast-YYYYMMDD.jsonl` (format: FastSampler.record). The journal gets a start line, errors and a summary every
`--summary-min` minutes, never each sample.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import config
except ModuleNotFoundError:  # a copied, standalone sampler on a compute-only host
    config = None

GIB = 1024 ** 3


def utc(epoch=None):
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch, dt.timezone.utc).isoformat()


def read_number(path):
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def run(argv, timeout=8):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env=config.tool_env() if config else None)


def gpu_observations(drm="/sys/class/drm"):
    """Stable device identities; missing metrics remain null, never fabricated zeroes."""
    rows, errors = [], []
    tool_path = config.tool_env().get("PATH") if config else None
    if shutil.which("nvidia-smi", path=tool_path):
        try:
            r = run(["nvidia-smi", "--query-gpu=uuid,pci.bus_id,name,utilization.gpu,memory.total,memory.used",
                     "--format=csv,noheader,nounits"])
            if r.returncode:
                raise ValueError(f"nvidia-smi exited {r.returncode}")
            for values in csv.reader(r.stdout.splitlines()):
                if len(values) != 6:
                    raise ValueError("invalid NVIDIA inventory row")
                uid, pci, name, busy, total, used = [s.strip() for s in values]
                def number(s):
                    try:
                        return float(s)
                    except ValueError:
                        return None
                rows.append({"uuid": uid, "pci": pci, "name": name, "vendor": "nvidia",
                             "utilization_pct": number(busy), "vram_total_mib": number(total),
                             "vram_used_mib": number(used)})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
    for card in sorted(Path(drm).glob("card*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        try:
            if (device / "vendor").read_text().strip().lower() != "0x1002":
                continue
            uid = (device / "unique_id").read_text().strip().lower().removeprefix("0x")
            if not re.fullmatch(r"[0-9a-f]{1,16}", uid) or int(uid, 16) == 0:
                continue
        except OSError:
            continue  # excludes integrated GPUs without an allocatable identity
        total, used = read_number(device / "mem_info_vram_total"), read_number(device / "mem_info_vram_used")
        try:
            runtime_status = (device / "power/runtime_status").read_text().strip()
        except OSError:
            runtime_status = None
        rows.append({"uuid": "GPU-" + uid.zfill(16), "pci": device.resolve().name, "name": "AMD GPU",
                     "vendor": "amd", "runtime_status": runtime_status,
                     "utilization_pct": read_number(device / "gpu_busy_percent"),
                     "vram_total_mib": total / 1048576 if total is not None else None,
                     "vram_used_mib": used / 1048576 if used is not None else None})
    return rows, errors


def memory_observation(path="/proc/meminfo"):
    try:
        fields = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in Path(path).read_text().splitlines()}
        return {"total_bytes": fields.get("MemTotal"), "available_bytes": fields.get("MemAvailable")}
    except (OSError, ValueError, IndexError):
        return {"total_bytes": None, "available_bytes": None}


# ---- fast path: memory forensics from files only ---------------------------------------------------

MIB = 1024 ** 2
FAST_INTERVAL = 2.0     # seconds between fast records (0 turns the fast path off)
FAST_RETAIN_DAYS = 7    # compressed fast days kept (a busy host: about 100 MiB plain for today, 4 MiB a day gzipped)
FAST_MIN_MIB = 128      # an app.slice unit using less is folded into the record's `rest`
SUMMARY_MIN = 15.0      # minutes between journal summaries
TOP_PROCS = 5
DOCKER_RESCAN_S = 30.0  # how often the cgroup tree is walked for docker-*.scope cgroups


def _read_text(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def _mib(n):
    return None if n is None else round(n / MIB)


def psi_memory(proc="/proc"):
    """/proc/pressure/memory as {"some10", "some60", "full10", "full60"} (percent); {} when unreadable."""
    out = {}
    for line in (_read_text(os.path.join(proc, "pressure", "memory")) or "").splitlines():
        kind, *fields = line.split()
        vals = dict(f.split("=", 1) for f in fields if "=" in f)
        for avg in ("avg10", "avg60"):
            try:
                out[f"{kind}{avg[3:]}"] = float(vals[avg])
            except (KeyError, ValueError):
                pass
    return out


def meminfo_fast(proc="/proc"):
    """MemAvailable, SwapFree and Dirty from meminfo, in MiB (a missing field is absent)."""
    want = {"MemAvailable": "avail_mib", "SwapFree": "swap_free_mib", "Dirty": "dirty_mib"}
    out = {}
    for line in (_read_text(os.path.join(proc, "meminfo")) or "").splitlines():
        key, _, rest = line.partition(":")
        if key in want:
            try:
                out[want[key]] = _mib(int(rest.split()[0]) * 1024)
            except (IndexError, ValueError):
                pass
    return out


def cgroup_memory(path):
    """One cgroup's memory in MiB: `cur` (memory.current), `peak` (memory.peak, when the kernel has it), `anon` and
    `file` (memory.stat), and `oom` (memory.events oom_kill, a running count). None when memory.current is
    unreadable (no memory controller there, or the cgroup is gone)."""
    cur = _read_text(os.path.join(path, "memory.current"))
    try:
        out = {"cur": _mib(int(cur))}
    except (TypeError, ValueError):
        return None
    peak = _read_text(os.path.join(path, "memory.peak"))
    if peak and peak.strip().isdigit():
        out["peak"] = _mib(int(peak))
    for line in (_read_text(os.path.join(path, "memory.stat")) or "").splitlines():
        k, _, v = line.partition(" ")
        if k in ("anon", "file") and v.strip().isdigit():
            out[k] = _mib(int(v))
    for line in (_read_text(os.path.join(path, "memory.events")) or "").splitlines():
        k, _, v = line.partition(" ")
        if k == "oom_kill" and v.strip().isdigit():
            out["oom"] = int(v)
    return out


_USER_MGR = re.compile(r"^user\.slice/user-(\d+)\.slice/user@\1\.service/")


def short_cg(path):
    """A cgroup path below the root as records write it: `user.slice/user-<uid>.slice/user@<uid>.service/` becomes
    `user@<uid>/`, which keeps a record about half as long; anything else is unchanged."""
    return _USER_MGR.sub(lambda m: f"user@{m.group(1)}/", path.strip("/"))


def top_processes(proc="/proc", n=TOP_PROCS):
    """The `n` processes with the largest RSS (from /proc/<pid>/statm), each with its uid, comm and cgroup path.
    No command lines are read."""
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    rows = []
    try:
        entries = [e.name for e in os.scandir(proc) if e.name.isdigit()]
    except OSError:
        return []
    for pid in entries:
        statm = _read_text(os.path.join(proc, pid, "statm"))
        try:
            rows.append((int(statm.split()[1]) * page, pid))
        except (AttributeError, IndexError, ValueError):
            continue  # gone, or a kernel thread
    out = []
    for rss, pid in sorted(rows, reverse=True)[:n]:
        cg = ""
        for line in (_read_text(os.path.join(proc, pid, "cgroup")) or "").splitlines():
            if line.startswith("0::"):
                cg = short_cg(line[3:].strip())
        try:
            uid = os.stat(os.path.join(proc, pid)).st_uid
        except OSError:
            uid = None
        out.append({"pid": int(pid), "uid": uid, "rss_mib": _mib(rss),
                    "comm": (_read_text(os.path.join(proc, pid, "comm")) or "").strip()[:32], "cg": cg})
    return out


class FastSampler:
    """The fast path's state: which cgroups to read, the open summary window and the errors already logged.

    One record per tick, a JSON line in `<directory>/<host>/fast-YYYYMMDD.jsonl`:

        {"v": 1, "type": "fast_sample", "host": H, "ts": <epoch s>,
         "psi": {"some10", "some60", "full10", "full60"},               # /proc/pressure/memory, percent
         "mem": {"avail_mib", "swap_free_mib", "dirty_mib"},             # /proc/meminfo
         "cg": {"<cgroup path below the root, short_cg>": {"cur", "peak", "anon", "file", "oom"}, ...},
         "rest": {"n": <units folded>, "cur": <their MiB>},              # small app.slice units
         "top": [{"pid", "uid", "rss_mib", "comm", "cg"}, ...],          # largest RSS first
         "errors": [...]}                                                # only when a read failed

    Sizes are MiB (rounded), `oom` is memory.events' running oom_kill count, and a field that could not be read is
    absent, never zero. `cg` always holds `system.slice`, every `user.slice/user-<uid>.slice` and every
    `docker-*.scope` under user.slice (rootless containers of any uid); a unit under a user manager's `app.slice` is
    listed while it uses at least `min_mib` and is otherwise counted in `rest`. Closed days are gzipped by the first
    record of a new UTC day and compressed days older than `retain_days` are removed (compact_events)."""

    def __init__(self, host, directory, proc="/proc", cgroup_root="/sys/fs/cgroup", retain_days=FAST_RETAIN_DAYS,
                 min_mib=FAST_MIN_MIB):
        self.host, self.dir = host, Path(directory) / host
        self.proc, self.root = proc, cgroup_root
        self.retain_days, self.min_mib = retain_days, min_mib
        self._docker, self._docker_at = [], None
        self._oom_prev = {}  # cgroup path -> the oom_kill count last read
        self.logged = set()
        self.reset_window()

    def reset_window(self):
        self.win = {"samples": 0, "full_samples": 0, "errors": 0, "avail_min_mib": None, "swap_free_min_mib": None,
                    "some10_max": None, "full10_max": None, "max_cg": None, "oom_kills": {}}

    def docker_scopes(self, now):
        if self._docker_at is None or now - self._docker_at >= DOCKER_RESCAN_S:
            found = []
            for dirpath, dirnames, _files in os.walk(os.path.join(self.root, "user.slice")):
                for d in [d for d in dirnames if d.startswith("docker-") and d.endswith(".scope")]:
                    found.append(os.path.relpath(os.path.join(dirpath, d), self.root))
                    dirnames.remove(d)  # a container's own cgroups are its business
            self._docker, self._docker_at = sorted(found), now
        return self._docker

    def cgroups(self, now):
        """[(path below the root, always listed)] for this tick."""
        out = [("system.slice", True)]
        users = sorted(glob_dirs(os.path.join(self.root, "user.slice"), "user-*.slice"))
        for u in users:
            out.append((f"user.slice/{u}", True))
            for mgr in sorted(glob_dirs(os.path.join(self.root, "user.slice", u), "user@*.service")):
                base = f"user.slice/{u}/{mgr}/app.slice"
                out += [(f"{base}/{unit}", False) for unit in sorted(glob_dirs(os.path.join(self.root, base), "*"))]
        seen = {p for p, _ in out}
        out += [(p, True) for p in self.docker_scopes(now) if p not in seen]
        return out

    def record(self, now=None):
        now = time.time() if now is None else now
        rec = {"v": 1, "type": "fast_sample", "host": self.host, "ts": round(now, 1),
               "psi": psi_memory(self.proc), "mem": meminfo_fast(self.proc), "cg": {}, "rest": {"n": 0, "cur": 0}}
        errors = []
        if not rec["psi"]:
            errors.append("psi: /proc/pressure/memory unreadable")
        if not rec["mem"]:
            errors.append("meminfo unreadable")
        try:
            for path, always in self.cgroups(now):
                m = cgroup_memory(os.path.join(self.root, path))
                if m is None:
                    continue
                if always or (m["cur"] or 0) >= self.min_mib:
                    rec["cg"][short_cg(path)] = m
                else:
                    rec["rest"]["n"] += 1
                    rec["rest"]["cur"] += m["cur"] or 0
        except OSError as exc:
            errors.append(f"cgroups: {exc}")
        if not rec["cg"]:
            errors.append(f"no cgroup memory readable under {self.root}")
        rec["top"] = top_processes(self.proc)
        if errors:
            rec["errors"] = errors
        self.new_ooms = self._fold(rec)
        return rec

    def _fold(self, rec):
        w = self.win
        w["samples"] += 1
        w["errors"] += len(rec.get("errors") or [])
        for key, field in (("avail_min_mib", "avail_mib"), ("swap_free_min_mib", "swap_free_mib")):
            v = rec["mem"].get(field)
            if v is not None and (w[key] is None or v < w[key]):
                w[key] = v
        for key, field in (("some10_max", "some10"), ("full10_max", "full10")):
            v = rec["psi"].get(field)
            if v is not None and (w[key] is None or v > w[key]):
                w[key] = v
        new_ooms = {}
        for path, m in rec["cg"].items():
            # the largest unit or container (a slice that only sums its children is not a culprit)
            if path.startswith("user@") and (w["max_cg"] is None or (m["cur"] or 0) > w["max_cg"]["cur_mib"]):
                w["max_cg"] = {"path": path, "cur_mib": m["cur"] or 0}
            if "oom" in m:
                before = self._oom_prev.get(path)
                if before is not None and m["oom"] > before:
                    new_ooms[path] = m["oom"] - before
                    w["oom_kills"][path] = w["oom_kills"].get(path, 0) + m["oom"] - before
                self._oom_prev[path] = m["oom"]
        return new_ooms

    def write(self, rec):
        self.dir.mkdir(parents=True, exist_ok=True)
        day = dt.datetime.fromtimestamp(rec["ts"], dt.timezone.utc).strftime("%Y%m%d")
        target = self.dir / f"fast-{day}.jsonl"
        if not target.exists():
            compact_events(self.dir, day, self.retain_days, prefix="fast-")
        with target.open("a") as out:
            out.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def tick(self, now=None):
        rec = self.record(now)
        self.write(rec)
        for e in rec.get("errors") or []:
            if e not in self.logged:  # each distinct error once; the summary counts the rest
                self.logged.add(e)
                log({"fast_path_error": e})
        if self.new_ooms:  # rare and decisive: said at once, not only in the next summary
            log({"oom_kill": {"ts": rec["ts"], "cgroups": self.new_ooms, "mem": rec["mem"], "psi": rec["psi"]}})
        return rec

    def summary(self, window_min):
        out = dict(self.win, window_min=round(window_min, 1))
        self.reset_window()
        return {"resources_summary": out}


def glob_dirs(path, pattern):
    import fnmatch
    try:
        return [e.name for e in os.scandir(path) if e.is_dir(follow_symlinks=False) and fnmatch.fnmatch(e.name, pattern)]
    except OSError:
        return []


def log(value):
    """One journal line (the service's stdout): start, errors and summaries only."""
    print(json.dumps(value, sort_keys=True), flush=True)


def lease_observation(command):
    if not command:
        return {"ok": False, "error": "no lease provider configured", "leases": [], "queue": []}
    try:
        r = run(shlex.split(command) + ["--status", "--json"])
        if r.returncode:
            raise ValueError(f"lease provider exited {r.returncode}")
        value = json.loads(r.stdout)
        if not isinstance(value.get("leases"), list) or not isinstance(value.get("queue"), list):
            raise ValueError("lease provider must return leases and queue arrays")
        if any(not isinstance(row, dict) or not isinstance(row.get("held"), bool) for row in value["leases"]):
            raise ValueError("invalid lease row")
        if not value["leases"] or any(not row.get("key") for row in value["leases"]):
            raise ValueError("lease provider returned no identifiable leases")
        return dict(value, ok=True)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc), "leases": [], "queue": []}


def normalized_leases(provider, gpus, host, boot):
    """An exclusive global GPU claim covers all devices; a shared global gate is not a claim."""
    out = []
    for row in provider.get("leases", []):
        key = str(row.get("key") or "")
        if not row.get("held") or (key == "gpu" and row.get("mode") == "shared"):
            continue
        if key == "gpu":
            scope, devices = "host_gpu", [g["uuid"] for g in gpus]
        elif key.startswith("gpu@"):
            scope, devices = "device_gpu", [key[4:]]
        elif key == "cpu-heavy":
            scope, devices = "host_cpu_memory", [g["uuid"] for g in gpus]
        else:
            scope, devices = "other", []
        identity = [host, boot, key, row.get("pid"), row.get("since"), row.get("holder")]
        out.append({"lease_id": hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24],
                    "allocation_id": hashlib.sha256(json.dumps(identity[:2] + identity[3:]).encode()).hexdigest()[:24],
                    "key": key, "scope": scope, "gpu_reserved": scope in ("host_gpu", "device_gpu"),
                    "devices": devices, "holder": row.get("holder"), "pid": row.get("pid"),
                    "since": row.get("since"), "attribution": "reservation_overlap"})
    return out


def snapshot(host, lease_command, session_ready=(), now=None):
    now = time.time() if now is None else now
    gpus, errors = gpu_observations()
    provider = lease_observation(lease_command)
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = "unknown"
    tool_path = config.tool_env().get("PATH") if config else None
    harnesses = [name for name in ("claude", "opencode", "codex", "muse") if shutil.which(name, path=tool_path)]
    return {"v": 1, "type": "resource_sample", "ts": now, "ts_utc": utc(now), "host": host, "boot_id": boot,
            "gpus": gpus, "gpu_errors": errors, "memory": memory_observation(),
            "lease_provider": {**{k: v for k, v in provider.items() if k in ("ok", "error")},
                               "known_keys": [r["key"] for r in provider["leases"]]},
            "leases": normalized_leases(provider, gpus, host, boot), "queue": [{k: v for k, v in row.items() if k in ("key", "waiter", "pid", "since", "want", "gpus")}
                      for row in provider.get("queue", []) if isinstance(row, dict)],
            "capabilities": {"shoal": bool(config), "harnesses": harnesses,
                             "session_ready": [name for name in session_ready if name in harnesses and config]}}


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _lease_scope(key):
    """The scope a lease key reserves (the same mapping normalized_leases applies to held rows),
    so a known key with nothing held can still be drawn at zero in its own scope."""
    key = str(key or "")
    if key == "gpu":
        return "host_gpu"
    if key.startswith("gpu@"):
        return "device_gpu"
    if key == "cpu-heavy":
        return "host_cpu_memory"
    return "other"


def render_prometheus(sample):
    """The GPU and lease projection of one resource sample, as Prometheus text for a textfile
    collector. Missing measurements stay missing (no series), never fabricated zeroes: a suspended
    GPU's utilization is unmeasurable, so it gets a runtime-suspended series instead of a zero.
    Lease counts are different: zero held (or queued) for a key the provider knows is a measured
    fact, and is emitted, so an idle host is represented rather than absent."""
    host = _esc(sample.get("host", ""))
    L = ["# HELP shoal_resources_sample_timestamp_seconds Time of the last host resource sample.",
         "# TYPE shoal_resources_sample_timestamp_seconds gauge",
         f'shoal_resources_sample_timestamp_seconds{{host="{host}"}} {float(sample.get("ts") or 0):.3f}']
    L += ["# HELP shoal_gpu_utilization_percent GPU busy percent at sample time.",
          "# TYPE shoal_gpu_utilization_percent gauge",
          "# HELP shoal_gpu_vram_used_bytes GPU VRAM in use.", "# TYPE shoal_gpu_vram_used_bytes gauge",
          "# HELP shoal_gpu_vram_total_bytes GPU VRAM installed.", "# TYPE shoal_gpu_vram_total_bytes gauge",
          "# HELP shoal_gpu_runtime_suspended GPU runtime-suspended (1) or not (0); utilization is unmeasurable while suspended.",
          "# TYPE shoal_gpu_runtime_suspended gauge"]
    for g in sample.get("gpus", []):
        lab = f'host="{host}",uuid="{_esc(g.get("uuid", ""))}",vendor="{_esc(g.get("vendor", ""))}"'
        if g.get("utilization_pct") is not None:
            L.append(f'shoal_gpu_utilization_percent{{{lab}}} {float(g["utilization_pct"]):.3f}')
        if g.get("vram_used_mib") is not None:
            L.append(f'shoal_gpu_vram_used_bytes{{{lab}}} {float(g["vram_used_mib"]) * 1048576:.0f}')
        if g.get("vram_total_mib") is not None:
            L.append(f'shoal_gpu_vram_total_bytes{{{lab}}} {float(g["vram_total_mib"]) * 1048576:.0f}')
        if g.get("runtime_status") is not None:
            L.append(f'shoal_gpu_runtime_suspended{{{lab}}} {1 if g["runtime_status"] == "suspended" else 0}')
    known = [str(k) for k in ((sample.get("lease_provider") or {}).get("known_keys") or [])]
    L += ["# HELP shoal_lease_reservations Held lease reservations by key and scope (0 for a known key with none held).",
          "# TYPE shoal_lease_reservations gauge"]
    counts = {}
    for row in sample.get("leases", []):
        counts[(row.get("key", ""), row.get("scope", ""))] = counts.get((row.get("key", ""), row.get("scope", "")), 0) + 1
    for key in known:
        counts.setdefault((key, _lease_scope(key)), 0)
    for (key, scope), n in sorted(counts.items()):
        L.append(f'shoal_lease_reservations{{host="{host}",key="{_esc(key)}",scope="{_esc(scope)}"}} {n}')
    L += ["# HELP shoal_lease_queue_length Lease requests queued by key (0 for a known key with none queued).",
          "# TYPE shoal_lease_queue_length gauge"]
    qcounts = {}
    for row in sample.get("queue", []):
        qcounts[row.get("key", "")] = qcounts.get(row.get("key", ""), 0) + 1
    for key in sorted(set(qcounts) | {k for k, _s in counts} | set(known)):
        L.append(f'shoal_lease_queue_length{{host="{host}",key="{_esc(key)}"}} {qcounts.get(key, 0)}')
    return "\n".join(L) + "\n"


def _atomic_write(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, p)


# Compressed daily event files are kept this many days, then removed.
RETAIN_DAYS = 90


def _day_of(name, suffix, prefix="events-"):
    stem = name[len(prefix):-len(suffix)]
    return stem if len(stem) == 8 and stem.isdigit() else None


def compact_events(directory, today, retain_days=RETAIN_DAYS, prefix="events-"):
    """Gzip every closed day's event file (any day before `today`, YYYYMMDD) and remove compressed
    days older than `retain_days`. A late append to a day already compressed becomes one more gzip
    member, which readers take as part of the same file. The compressed file is replaced
    atomically, so a crash leaves either the old pair or the new file, never a torn member.
    `prefix` picks the stream (`events-` samples, `fast-` fast-path records)."""
    directory = Path(directory)
    for plain in sorted(directory.glob(f"{prefix}*.jsonl")):
        day = _day_of(plain.name, ".jsonl", prefix)
        if day is None or day >= today:
            continue
        gz = plain.with_name(plain.name + ".gz")
        tmp = plain.with_name(plain.name + ".gz.tmp")
        with tmp.open("wb") as raw:
            if gz.exists():
                with gz.open("rb") as old:
                    shutil.copyfileobj(old, raw)
            with plain.open("rb") as src, gzip.GzipFile(fileobj=raw, mode="ab") as dst:
                shutil.copyfileobj(src, dst)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, gz)
        plain.unlink()
    cutoff = (dt.datetime.strptime(today, "%Y%m%d") - dt.timedelta(days=retain_days)).strftime("%Y%m%d")
    for gz in sorted(directory.glob(f"{prefix}*.jsonl.gz")):
        day = _day_of(gz.name, ".jsonl.gz", prefix)
        if day is not None and day < cutoff:
            gz.unlink()


def append_sample(root, sample, retain_days=RETAIN_DAYS):
    """Keep observed lifecycle edges with explicit bounds; failed probes never close a lease.
    The first sample of a new UTC day also compresses the closed days and applies retention."""
    directory = Path(root) / sample["host"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "sample.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = directory / "state.json"
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {"leases": {}}
        events = [sample]
        if sample["lease_provider"]["ok"]:
            old = state.get("leases", {})
            identities = {r.get("identity", r["lease_id"]): r for r in old.values()}
            for lease in sample["leases"]:
                identity = lease["lease_id"]
                lease["identity"] = identity
                lease["lease_id"] = identities.get(identity, {}).get("lease_id") or hashlib.sha256(
                    f"{identity}:{sample['ts']}".encode()).hexdigest()[:24]
            current = {r["lease_id"]: r for r in sample["leases"]}
            for key, lease in current.items():
                if key not in old:
                    events.append({"v": 1, "type": "lease_observed_acquire", "host": sample["host"],
                                   "ts": sample["ts"], "ts_utc": sample["ts_utc"], "lease": lease})
            for key, lease in old.items():
                if key not in current:
                    events.append({"v": 1, "type": "lease_observed_release", "host": sample["host"],
                                   "ts": sample["ts"], "ts_utc": sample["ts_utc"], "lease": lease,
                                   "release_after": state.get("ts"), "release_by": sample["ts"]})
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": sample["ts"], "leases": current}))
            os.replace(tmp, state_path)
        day = dt.datetime.fromtimestamp(sample["ts"], dt.timezone.utc).strftime("%Y%m%d")
        target = directory / f"events-{day}.jsonl"
        if not target.exists():
            compact_events(directory, day, retain_days)
        with target.open("a") as out:
            for event in events:
                out.write(json.dumps(event, sort_keys=True) + "\n")
    return events


def load_events(paths):
    events = []
    for path in paths:
        if Path(path).is_dir():
            files = sorted(set(Path(path).rglob("events-*.jsonl")) | set(Path(path).rglob("events-*.jsonl.gz")))
        else:
            files = [Path(path)]
        for file in files:
            opener = gzip.open if file.name.endswith(".gz") else open
            with opener(file, "rt") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict) and "ts" in value:
                            events.append(value)
                    except ValueError:
                        continue  # a torn final append is not a zero-activity sample
    return events


def epoch(value, fallback):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else fallback
    except ValueError:
        return fallback


def efficiency(events, threshold=10, max_gap=30):
    """Time-weighted reservation overlap, with unknown gaps excluded and coverage always shown."""
    if not 0 < threshold <= 100 or max_gap <= 0:
        raise ValueError("activity threshold must be in (0, 100] and maximum gap positive")
    samples = sorted((e for e in events if e.get("type") == "resource_sample"), key=lambda e: e["ts"])
    episodes, by_host = {}, {}
    for s in samples:
        by_host.setdefault(s["host"], []).append(s)
        for lease in s["leases"]:
            key = lease["lease_id"]
            if key not in episodes:
                episodes[key] = dict(lease, host=s["host"], start=min(s["ts"], epoch(lease.get("since"), s["ts"])),
                                     end=s["ts"], closed=False, metrics={}, memory_covered_s=0,
                                     memory_used_pct_seconds=0)
            episodes[key]["end"] = s["ts"]
    for e in events:
        if e.get("type") == "lease_observed_release" and e["lease"]["lease_id"] in episodes:
            item = episodes[e["lease"]["lease_id"]]
            item.update(end=e["ts"], closed=True, release_after=e.get("release_after"), release_by=e["ts"])
    for host_samples in by_host.values():
        for a, b in zip(host_samples, host_samples[1:]):
            span = b["ts"] - a["ts"]
            if span <= 0 or span > max_gap or not a["lease_provider"]["ok"] or not b["lease_provider"]["ok"]:
                continue
            if a["boot_id"] != b["boot_id"]:
                continue
            gpu = {g["uuid"]: g for g in a["gpus"]}
            # A release is known only between observations. Do not extrapolate activity into that interval.
            still = {r["lease_id"] for r in b["leases"]}
            for lease in a["leases"]:
                if lease["lease_id"] not in still:
                    continue
                item = episodes[lease["lease_id"]]
                memory = a.get("memory", {})
                total, available = memory.get("total_bytes"), memory.get("available_bytes")
                if total and available is not None and 0 <= available <= total:
                    item["memory_covered_s"] += span
                    item["memory_used_pct_seconds"] += 100 * (total - available) / total * span
                for uid in lease["devices"]:
                    value = gpu.get(uid, {}).get("utilization_pct")
                    if not isinstance(value, (int, float)) or not 0 <= value <= 100:
                        continue
                    m = item["metrics"].setdefault(uid, {"covered_s": 0, "utilization_seconds": 0, "active_s": 0})
                    m["covered_s"] += span
                    m["utilization_seconds"] += value * span
                    m["active_s"] += span if value >= threshold else 0
    for item in episodes.values():
        item["reserved_s"] = max(0, item["end"] - item["start"])
        item["duration_kind"] = "observed_upper_bound" if item["closed"] else "open_right_censored"
        item["active_threshold_pct"] = threshold
        item["mean_host_memory_used_pct"] = (item["memory_used_pct_seconds"] / item["memory_covered_s"]
                                             if item["memory_covered_s"] else None)
        del item["memory_used_pct_seconds"]
        item["devices_report"] = []
        for uid in item["devices"]:
            m = item["metrics"].get(uid, {})
            covered = m.get("covered_s", 0)
            item["devices_report"].append({"uuid": uid, "covered_s": covered,
                "coverage_pct": 100 * covered / item["reserved_s"] if item["reserved_s"] else None,
                "mean_utilization_pct": m["utilization_seconds"] / covered if covered else None,
                "active_fraction_pct": 100 * m["active_s"] / covered if covered else None})
        del item["metrics"]
    return sorted(episodes.values(), key=lambda e: (e["host"], e["start"], e["key"]))


def placement(samples, memory_gib=0, gpu_count=0, vram_mib=0, cpu_heavy=False,
              harness="", current="", now=None, max_age=120, gpu_vendor=""):
    """Advisory only. Unknown, stale or unprovisioned hosts cannot become migration targets."""
    if not all(0 <= n < float("inf") for n in (memory_gib, gpu_count, vram_mib)) or max_age <= 0:
        raise ValueError("resource demands must be finite and nonnegative; maximum age positive")
    now = time.time() if now is None else now
    latest = {}
    for s in samples:
        if s.get("type") == "resource_sample" and s["ts"] >= latest.get(s["host"], {}).get("ts", -1):
            latest[s["host"]] = s
    rows = []
    for host, s in latest.items():
        reasons = []
        if not 0 <= now - s["ts"] <= max_age:
            reasons.append("stale or future-dated observation")
        if not s["lease_provider"]["ok"]:
            reasons.append("unknown lease state")
        known = s["lease_provider"].get("known_keys", [])
        if gpu_count and "gpu" not in known:
            reasons.append("GPU lease key not observed")
        if gpu_count and gpu_vendor not in ("amd", "nvidia", "any"):
            reasons.append("GPU workload vendor compatibility not declared")
        if cpu_heavy and "cpu-heavy" not in known:
            reasons.append("CPU/memory lease key not observed")
        memory = s["memory"].get("available_bytes")
        if memory is None or memory < memory_gib * GIB:
            reasons.append("insufficient or unknown host memory")
        reserved = {uid for r in s["leases"] if r["gpu_reserved"] for uid in r["devices"]}
        fitting = [g for g in s["gpus"] if g["uuid"] not in reserved
                   and (gpu_vendor == "any" or g.get("vendor") == gpu_vendor)
                   and g.get("vram_total_mib") is not None and g.get("vram_used_mib") is not None
                   and g["vram_total_mib"] - g["vram_used_mib"] >= vram_mib
                   and (g.get("runtime_status") == "suspended" or
                        (g.get("utilization_pct") is not None and 0 <= g["utilization_pct"] < 10))]
        if len(fitting) < gpu_count:
            reasons.append("insufficient unreserved fitting GPUs")
        if cpu_heavy and any(r["key"] == "cpu-heavy" for r in s["leases"]):
            reasons.append("host CPU/memory isolation lease held")
        if gpu_count and any(r.get("key") == "gpu" for r in s.get("queue", [])):
            reasons.append("GPU lease requests already queued")
        if cpu_heavy and any(r.get("key") == "cpu-heavy" for r in s.get("queue", [])):
            reasons.append("host CPU/memory requests already queued")
        if harness and harness not in s.get("capabilities", {}).get("session_ready", []):
            reasons.append("session preflight incomplete for " + harness)
        score = (memory or 0) / GIB + 16 * len(fitting)
        rows.append({"host": host, "eligible": not reasons, "reasons": reasons, "score": score,
                     "free_fitting_gpus": len(fitting), "memory_available_gib": memory / GIB if memory is not None else None,
                     "sample_ts_utc": s["ts_utc"]})
    eligible = sorted((r for r in rows if r["eligible"]), key=lambda r: (-r["score"], r["host"]))
    chosen = eligible[0] if eligible else None
    incumbent = next((r for r in eligible if r["host"] == current), None)
    if chosen and incumbent and incumbent["score"] >= chosen["score"] * 0.9:
        chosen = incumbent
    return {"v": 1, "advisory_only": True, "kind": "session" if harness else "compute",
            "gpu_vendor": gpu_vendor or None, "recommended_host": chosen["host"] if chosen else None, "hosts": rows}


def defaults():
    base = config.data_root() if config else os.environ.get("SHOAL_DATA_ROOT", str(Path.home() / ".local/share/shoal"))
    return {"directory": str(Path(base) / "_resources"),
            "host": config.server() if config else socket.gethostname(),
            "lease_command": config.lease_cmd() if config else os.environ.get("GPU_LEASE_CMD", "")}


def role_time_specs(a):
    """The specs whose role-time native status cache each sample refreshes: every live group with a foreground role on
    this host, read from the ledger clone each sample (so the caches follow the specs, with no per-group flag to keep in
    step), plus any `--role-time-spec`, once each. `--no-role-time-live` keeps only the named ones; a copied standalone
    sampler has no specs to read and refreshes none."""
    out = list(getattr(a, "role_time_spec", []) or [])
    if config is not None and not getattr(a, "no_role_time_live", False):
        try:
            import role_time_adapters
            out += role_time_adapters.live_local_specs()
        except Exception as exc:  # noqa: BLE001 - telemetry extras never stop the sampler
            print(json.dumps({"role_time_live_specs_error": str(exc)[:200]}), file=sys.stderr, flush=True)
    seen, kept = set(), []
    for path in out:
        if os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            kept.append(path)
    return kept


def full_sample(a):
    """One full sample: appended to the event stream, projected to Prometheus text, and the per-sample extras."""
    s = snapshot(a.host, a.lease_command, a.session_ready.split(",") if a.session_ready else ())
    append_sample(a.directory, s, a.retain_days)
    if getattr(a, "prom_out", ""):
        text = render_prometheus(s)
        # cross-host imbalance over compatible demand rides the same file where the pool
        # planner is importable; a copied standalone sampler simply skips it
        try:
            import pool_metrics
            text += pool_metrics.render(s["ts"], s["host"])
        except Exception:  # noqa: BLE001 - telemetry extras never stop the sampler
            pass
        _atomic_write(a.prom_out, text)
    if config is not None:
        # research reservations expire at --until even when nobody probes the pool; this tick (never a probe's)
        # also reads the other pool hosts' probes for the agent rotation's swap rule, while a reservation stands here
        try:
            import pool_reserve
            pool_reserve.expire(s["ts"], peers=True)
        except Exception as exc:  # noqa: BLE001 - never stops the sampler
            print(json.dumps({"reservation_expiry_error": str(exc)[:200]}), file=sys.stderr, flush=True)
    for spec_path in role_time_specs(a):
        # role-time native status rides this loop on hosts that run sessions; a copied
        # standalone sampler (no shoal package beside it) simply skips it
        try:
            import role_time_adapters
            from roles import ShoalSpec
            role_time_adapters.write_native_cache(ShoalSpec.load(spec_path), now=s["ts"])
        except Exception as exc:  # noqa: BLE001 - telemetry extras never stop the sampler
            print(json.dumps({"role_time_native_status_error": str(exc)[:200],
                              "spec": spec_path}), file=sys.stderr, flush=True)
    return s


def watch(a, stop=None):
    """The `watch` loop: full samples every `interval` in a thread, fast records every `fast_interval` here, and a
    journal summary every `summary_min`. `stop()` true ends it (tests); a failed full sample ends it with the
    exception, as before, so systemd restarts the service."""
    stop = stop or (lambda: False)
    fast_s = float(getattr(a, "fast_interval", FAST_INTERVAL) or 0)
    fast = (FastSampler(a.host, a.directory, getattr(a, "proc_root", "/proc"),
                        getattr(a, "cgroup_root", "/sys/fs/cgroup"), getattr(a, "fast_retain_days", FAST_RETAIN_DAYS),
                        getattr(a, "fast_min_mib", FAST_MIN_MIB)) if fast_s > 0 else None)
    summary_s = float(getattr(a, "summary_min", SUMMARY_MIN) or 0) * 60
    log({"resources_watch_start": {"host": a.host, "directory": str(a.directory), "interval_s": a.interval,
                                   "fast_interval_s": fast_s, "summary_min": summary_s / 60}})
    failed, done = [], threading.Event()
    counts = {"full": 0}

    def full_loop():
        while not done.is_set():
            t0 = time.monotonic()
            try:
                full_sample(a)
                counts["full"] += 1
            except BaseException as exc:  # noqa: BLE001 - handed to the main loop, which exits with it
                failed.append(exc)
                done.set()
                return
            done.wait(max(0.0, a.interval - (time.monotonic() - t0)))
    worker = threading.Thread(target=full_loop, name="full-sample", daemon=True)
    worker.start()
    start = last_summary = time.monotonic()
    nxt = start
    try:
        while not stop() and not failed:
            now = time.monotonic()
            if fast is not None and now >= nxt:
                try:
                    fast.tick()
                except Exception as exc:  # noqa: BLE001 - a failed fast record never stops the sampler
                    if str(exc) not in fast.logged:
                        fast.logged.add(str(exc))
                        log({"fast_path_error": str(exc)[:200]})
                nxt = max(nxt + fast_s, now)
            if summary_s and now - last_summary >= summary_s:
                if fast is not None:
                    s = fast.summary((now - last_summary) / 60)
                    s["resources_summary"]["full_samples"] = counts["full"]
                else:
                    s = {"resources_summary": {"window_min": round((now - last_summary) / 60, 1),
                                               "full_samples": counts["full"]}}
                counts["full"] = 0
                log(s)
                last_summary = now
            wake = nxt if fast is not None else now + 1.0
            if summary_s:
                wake = min(wake, last_summary + summary_s)
            done.wait(max(0.05, wake - time.monotonic()))
    finally:
        done.set()
        worker.join(timeout=max(10.0, a.interval))
    if failed:
        raise failed[0]
    return 0


def execute(a):
    if a.resource_cmd == "sample":
        print(json.dumps(full_sample(a)), flush=True)
        return 0
    if a.resource_cmd == "watch":
        return watch(a)
    if a.resource_cmd == "install":
        argv = [sys.executable, str(Path(__file__).resolve()), "watch", "--directory", a.directory,
                "--host", a.host, "--lease-command", a.lease_command, "--interval", str(a.interval),
                "--session-ready", a.session_ready,
                "--fast-interval", str(getattr(a, "fast_interval", FAST_INTERVAL)),
                "--fast-retain-days", str(getattr(a, "fast_retain_days", FAST_RETAIN_DAYS)),
                "--summary-min", str(getattr(a, "summary_min", SUMMARY_MIN))]
        if getattr(a, "prom_out", ""):
            argv += ["--prom-out", a.prom_out]
        for spec_path in getattr(a, "role_time_spec", []) or []:
            argv += ["--role-time-spec", spec_path]
        if getattr(a, "no_role_time_live", False):
            argv += ["--no-role-time-live"]
        quote = lambda x: json.dumps(str(x).replace("%", "%%"))
        text = ("[Unit]\nDescription=SHOAL host resource observations\n[Service]\nType=simple\n"
                + "ExecStart=" + " ".join(quote(x) for x in argv) + "\nRestart=on-failure\nRestartSec=5\n"
                + "[Install]\nWantedBy=default.target\n")
        if a.dry_run:
            print(text)
            return 0
        directory = Path.home() / ".config/systemd/user"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "shoal-resources.service").write_text(text)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        if a.enable:
            # restart, not `enable --now`: an already-active service would keep running the old ExecStart
            subprocess.run(["systemctl", "--user", "enable", "shoal-resources.service"], check=True)
            subprocess.run(["systemctl", "--user", "restart", "shoal-resources.service"], check=True)
        return 0
    events = load_events(a.input or [a.directory])
    if a.resource_cmd == "report":
        value = {"v": 1, "leases": efficiency(events, a.threshold, a.max_gap),
                 "limitation": "observed lifecycle; short leases between samples can be missed"}
    else:
        value = placement(events, a.memory_gib, a.gpus, a.vram_mib, a.cpu_heavy, a.harness, a.current,
                          max_age=a.max_age, gpu_vendor=a.gpu_vendor)
    print(json.dumps(value, indent=2))
    return 0


def positive(value):
    result = float(value)
    if not 0 < result < float("inf"):
        raise argparse.ArgumentTypeError("must be positive and finite")
    return result


def add_parser(sub):
    p = sub.add_parser("resources", help="host lease/GPU telemetry and advisory placement")
    commands = p.add_subparsers(dest="resource_cmd", required=True)
    d = defaults()
    for name in ("sample", "watch", "report", "balance", "install"):
        q = commands.add_parser(name)
        q.add_argument("--directory", default=d["directory"])
        q.add_argument("--host", default=d["host"])
        if name in ("sample", "watch", "install"):
            q.add_argument("--lease-command", default=d["lease_command"])
            q.add_argument("--session-ready", default="", help="harnesses with completed session preflight, comma separated")
            q.add_argument("--prom-out", default="", help="also write the GPU/lease projection as Prometheus text here")
            q.add_argument("--role-time-spec", action="append", default=[],
                           help="also refresh the role-time native status cache for this spec (repeatable); every live "
                                "group with a role on this host is refreshed without it")
            q.add_argument("--no-role-time-live", action="store_true",
                           help="refresh only the --role-time-spec caches, not every live group's with a role here")
        if name in ("sample", "watch"):
            q.add_argument("--retain-days", type=positive, default=RETAIN_DAYS,
                           help="days compressed daily event files are kept")
        if name in ("watch", "install"):
            q.add_argument("--interval", type=positive, default=15, help="seconds between full samples (GPU, leases)")
            q.add_argument("--fast-interval", type=float, default=FAST_INTERVAL,
                           help="seconds between fast-path records (memory PSI, cgroups, top RSS; files only); 0 = off")
            q.add_argument("--fast-retain-days", type=positive, default=FAST_RETAIN_DAYS,
                           help="days compressed fast-path files are kept")
            q.add_argument("--summary-min", type=float, default=SUMMARY_MIN,
                           help="minutes between journal summaries (no sample is echoed); 0 = none")
        if name == "watch":
            q.add_argument("--fast-min-mib", type=float, default=FAST_MIN_MIB,
                           help="an app.slice unit below this many MiB is folded into the record's rest")
            q.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
            q.add_argument("--cgroup-root", default="/sys/fs/cgroup", help=argparse.SUPPRESS)
        if name == "install":
            q.add_argument("--dry-run", action="store_true")
            q.add_argument("--enable", action="store_true")
        if name in ("report", "balance"):
            q.add_argument("--input", action="append", default=[], help="host JSONL file or directory (repeatable)")
        if name == "report":
            q.add_argument("--threshold", type=float, default=10)
            q.add_argument("--max-gap", type=positive, default=30)
        if name == "balance":
            q.add_argument("--memory-gib", type=float, default=0)
            q.add_argument("--gpus", type=int, default=0)
            q.add_argument("--gpu-vendor", choices=("amd", "nvidia", "any"), default="",
                           help="required for GPU admission; any asserts workload compatibility with both")
            q.add_argument("--vram-mib", type=float, default=0)
            q.add_argument("--cpu-heavy", action="store_true")
            q.add_argument("--harness", default="", help="require session readiness for this harness")
            q.add_argument("--current", default="")
            q.add_argument("--max-age", type=positive, default=120)
        q.set_defaults(func=execute)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(required=True))
    # The copied file accepts the same command tail as `shoal resources ...`.
    args = parser.parse_args(["resources", *sys.argv[1:]])
    raise SystemExit(execute(args))
