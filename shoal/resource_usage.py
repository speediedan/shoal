"""Host resource observations, lease efficiency and a conservative placement pilot.

This file can also run standalone on a compute host without installing SHOAL.
It never acquires a lease, changes a host mode, or migrates a session.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
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


def append_sample(root, sample):
    """Keep observed lifecycle edges with explicit bounds; failed probes never close a lease."""
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
        with (directory / f"events-{day}.jsonl").open("a") as out:
            for event in events:
                out.write(json.dumps(event, sort_keys=True) + "\n")
    return events


def load_events(paths):
    events = []
    for path in paths:
        files = sorted(Path(path).rglob("events-*.jsonl")) if Path(path).is_dir() else [Path(path)]
        for file in files:
            with file.open() as stream:
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
              harness="", current="", now=None, max_age=120):
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
        if cpu_heavy and "cpu-heavy" not in known:
            reasons.append("CPU/memory lease key not observed")
        memory = s["memory"].get("available_bytes")
        if memory is None or memory < memory_gib * GIB:
            reasons.append("insufficient or unknown host memory")
        reserved = {uid for r in s["leases"] if r["gpu_reserved"] for uid in r["devices"]}
        fitting = [g for g in s["gpus"] if g["uuid"] not in reserved
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
            "recommended_host": chosen["host"] if chosen else None, "hosts": rows}


def defaults():
    base = config.data_root() if config else os.environ.get("SHOAL_DATA_ROOT", str(Path.home() / ".local/share/shoal"))
    return {"directory": str(Path(base) / "_resources"),
            "host": config.server() if config else socket.gethostname(),
            "lease_command": config.lease_cmd() if config else os.environ.get("GPU_LEASE_CMD", "")}


def execute(a):
    if a.resource_cmd in ("sample", "watch"):
        while True:
            s = snapshot(a.host, a.lease_command, a.session_ready.split(",") if a.session_ready else ())
            append_sample(a.directory, s)
            print(json.dumps(s), flush=True)
            if a.resource_cmd == "sample":
                return 0
            time.sleep(a.interval)
    if a.resource_cmd == "install":
        argv = [sys.executable, str(Path(__file__).resolve()), "watch", "--directory", a.directory,
                "--host", a.host, "--lease-command", a.lease_command, "--interval", str(a.interval),
                "--session-ready", a.session_ready]
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
            subprocess.run(["systemctl", "--user", "enable", "--now", "shoal-resources.service"], check=True)
        return 0
    events = load_events(a.input or [a.directory])
    if a.resource_cmd == "report":
        value = {"v": 1, "leases": efficiency(events, a.threshold, a.max_gap),
                 "limitation": "observed lifecycle; short leases between samples can be missed"}
    else:
        value = placement(events, a.memory_gib, a.gpus, a.vram_mib, a.cpu_heavy, a.harness, a.current,
                          max_age=a.max_age)
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
        if name in ("watch", "install"):
            q.add_argument("--interval", type=positive, default=15)
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
