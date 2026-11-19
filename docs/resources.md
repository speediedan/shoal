# Host resources and the placement pilot

The host sampler observes resources without running a model, acquiring a lease, changing a host
mode or moving a session. Run one per host even when that host serves several shoals.

```bash
shoal resources sample --lease-command '<lease-provider>'
shoal resources install --lease-command '<lease-provider>' --dry-run
shoal resources install --lease-command '<lease-provider>' --enable
shoal resources report --input <host-stream-directory>
shoal resources balance --input <host-stream-directory> --memory-gib 32 --gpus 1 --gpu-vendor amd --vram-mib 24000
```

Samples default to `<data_root>/_resources/<host>/events-YYYYMMDD.jsonl`. The first sample of a new
UTC day gzips every closed day (`events-YYYYMMDD.jsonl.gz`; a late append to a compressed day becomes
another gzip member) and removes compressed days older than `--retain-days` (default 90). Reports
read both forms. Copy or gather each host's stream to a reporting host and repeat `--input` to
combine them. `--directory` chooses another root.
The sampler file can run standalone on a compute-only host:

```bash
python3 resource_usage.py sample --host <host> --directory <output-root> --lease-command '<lease-provider>'
python3 resource_usage.py watch --host <host> --directory <output-root> --lease-command '<lease-provider>'
```

With `--prom-out <file>`, `sample` and `watch` also write each sample's GPU and lease projection
(utilization, VRAM, runtime-suspended state, held reservations and queue lengths, with measured zeros for known idle lease keys) as Prometheus text for a textfile collector;
a missing measurement gets no series. Each sample also refreshes the role-time native status cache
(architecture doc §9) of every live group with a foreground role on this host, read from the ledger
clone each time, so a group added, moved here or retired is followed without touching the unit.
`--role-time-spec <spec>` (repeatable) adds a spec by path; `--no-role-time-live` keeps only those.
A copied standalone sampler refreshes none. `install` carries these flags into the generated unit.

## The fast path (memory forensics)

`watch` also runs a fast path every `--fast-interval` seconds (default 2; 0 turns it off) that reads files only,
no subprocess, so it keeps recording when the host is too short of memory to start one. The full sample above
(GPUs, the lease provider) keeps its own `--interval` in a separate thread, so a slow provider never delays it.
Each fast record is one compact JSON line in `<data_root>/_resources/<host>/fast-YYYYMMDD.jsonl`:

```json
{"v":1,"type":"fast_sample","host":"<host>","ts":1800000000.0,
 "psi":{"some10":12.5,"some60":4.0,"full10":6.25,"full60":2.0},
 "mem":{"avail_mib":2048,"swap_free_mib":1,"dirty_mib":50},
 "cg":{"system.slice":{"cur":900,"peak":1200,"anon":300,"file":500,"oom":0},
       "user.slice/user-1000.slice":{"cur":30000,"anon":28000,"file":1500,"oom":0},
       "user@1000/app.slice/<unit>.service":{"cur":14700,"peak":15000,"anon":14000,"file":600,"oom":0},
       "user@1001/user.slice/docker-<id>.scope":{"cur":25000,"anon":24000,"file":800,"oom":0}},
 "rest":{"n":24,"cur":796},
 "top":[{"pid":4242,"uid":1000,"rss_mib":14700,"comm":"pytest","cg":"user@1000/app.slice/<unit>.service"}]}
```

- `psi`: `/proc/pressure/memory`, `some` and `full` over 10 and 60 s, in percent.
- `mem`: `MemAvailable`, `SwapFree` and `Dirty` from `/proc/meminfo`, in MiB.
- `cg`: per cgroup, below the cgroup root, with `user.slice/user-<uid>.slice/user@<uid>.service/` written
  `user@<uid>/`: `cur` (`memory.current`), `peak` (`memory.peak`, when the kernel has it), `anon` and `file`
  (`memory.stat`), in MiB, and `oom`, the running `oom_kill` count from `memory.events`. It always holds
  `system.slice`, each `user.slice/user-<uid>.slice` and every `docker-*.scope` under any user's tree (a rootless
  container of another uid among them; its own inner cgroups are not walked). A unit under a user manager's
  `app.slice` is listed while it uses at least `--fast-min-mib` (128), and otherwise counted in `rest` (`n` units,
  `cur` MiB together).
- `top`: the five processes with the largest RSS (`/proc/<pid>/statm`), with uid, `comm` and cgroup. No command
  line is read.
- `errors`: present only when a read failed. A value that could not be read is absent, never zero.

The first record of a new UTC day gzips the closed days (`fast-YYYYMMDD.jsonl.gz`) and removes compressed days
older than `--fast-retain-days` (default 7). On a busy host a day is about 100 MiB plain and 4 MiB gzipped.
Fast records are not resource events: `report`, `balance` and the pool's probe read only `events-*`.

The service's journal gets a start line, each distinct error once, an `oom_kill` line as soon as a cgroup's
`oom_kill` count rises, and one `resources_summary` line every `--summary-min` minutes (default 15; 0 for none):
samples taken, the lowest `MemAvailable` and `SwapFree`, the highest `some` and `full` avg10, the largest unit or
container, and the OOM kills in the window. No sample is echoed (the earlier sampler printed each one, about
4,600 lines a day). `--proc-root` and `--cgroup-root` point the fast path at a planted tree (tests).

## Service

`install --enable` reloads systemd, enables the service and restarts it, so a reinstall (new flags, a moved
checkout) replaces a running sampler instead of leaving the old process in place. The default cadence is 15 seconds. The service is `shoal-resources.service`, shared across groups.
Its dry run prints the exact unit before installation. Hardware observations use stable NVIDIA UUIDs
or AMD discrete-GPU `unique_id`, normalized as `GPU-<16 hex digits>`. Integrated GPUs without an
allocatable identity are excluded. Runtime suspension is recorded separately from utilization: a
suspended AMD GPU can return EBUSY for its utilization counter. The counter remains unknown; an
unreserved suspended device is eligible to wake for a job. Missing sensors or failed providers
otherwise remain unknown.

An exclusive `gpu` lease covers every allocatable device, even if each `gpu@UUID` key says free.
A shared global GPU gate is an aggregate: the individual device keys own the reservations. A
`cpu-heavy` lease reserves host CPU/memory isolation and does not by itself reserve a GPU. Combined
requests appear under each held key, with owner/start identity for correlation. The sampler does not
collect raw command lines.

Reports show the observed reservation interval, its bounds, per-device mean utilization and active
fraction, the activity threshold, covered seconds and percentage coverage. Default active threshold:
10 percent. Default maximum interpolation gap: 30 seconds. Mean utilization and active fraction
answer different questions. A quiet CPU/memory phase can legitimately hold a host lease, so host
memory pressure is reported too. GPU activity during a reservation is overlap, not proof of ownership.

These are sampled lifecycle events. A lease entirely between samples can be missed, the prefix
before collection is unknown, and release is bounded by successful observations. Reports do not
extrapolate through sensor failures, long gaps or a host reboot. A missing measurement is null,
not an invented zero. Low coverage prevents conclusions about the complete lease.

`balance` produces a JSON recommendation with its input timestamp and admission reasons. It rejects
stale or future-dated observations, unknown lease state, insufficient host memory, undeclared or incompatible GPU vendor requirements, busy/reserved or
undersized GPUs, and existing requests for the needed resource. An eligible current host remains
preferred within a small score margin. This is a placement pilot, not a scheduler or authorization.
GPU admission requires `--gpu-vendor amd|nvidia|any`; `any` explicitly asserts that the workload
supports both platforms. Vendor compatibility does not establish every model/driver requirement.
A recommendation can become stale immediately. Before a job starts, verify host mode and CI
ownership, satisfy any required grant, and acquire its normal lease. These control-plane checks
are not inferred from free hardware.

For session placement, add `--harness <harness>` and collect with `--session-ready <harnesses>` only
after verifying authentication, repository/start-doc access, ledger routes and handoff support.
Binary presence alone is insufficient. A compute-only host is useful for `job run --on <host>` but
cannot receive a lane session until its environment is ready. No credentials are copied by this tool,
and it never resets or migrates a session.
