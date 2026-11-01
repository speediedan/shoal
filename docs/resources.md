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

Samples default to `<data_root>/_resources/<host>/events-YYYYMMDD.jsonl`. Copy or gather each host's
stream to a reporting host and repeat `--input` to combine them. `--directory` chooses another root.
The sampler file can run standalone on a compute-only host:

```bash
python3 resource_usage.py sample --host <host> --directory <output-root> --lease-command '<lease-provider>'
python3 resource_usage.py watch --host <host> --directory <output-root> --lease-command '<lease-provider>'
```

With `--prom-out <file>`, `sample` and `watch` also write each sample's GPU and lease projection
(utilization, VRAM, runtime-suspended state, held reservations and queue lengths, with measured zeros for known idle lease keys) as Prometheus text for a textfile collector;
a missing measurement gets no series. With `--role-time-spec <spec>` (repeatable) they also refresh
that shoal's role-time native status cache each sample (architecture doc §9). `install` carries
both flags into the generated unit.

The default cadence is 15 seconds. The service is `shoal-resources.service`, shared across groups.
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
