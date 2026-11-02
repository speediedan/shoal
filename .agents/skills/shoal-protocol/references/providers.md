# Providers: leases and wait targets

The group depends on a few host facilities through narrow contracts, so any environment can supply
its own implementation. The operator configures them. As a role you only need to know what they
mean when a wait or a lease misbehaves. The full contracts are in the repository's
`docs/providers.md`.

## Resource leases (serializing GPUs or anything scarce)

A lease provider is a command (`SHOAL_LEASE_CMD`; `GPU_LEASE_CMD` is accepted as a fallback) that
serializes access to shared resources by key.
- **`status --json`** reports holders and waiters per key. That is what a `lease:<key>` wait target
  reads.
- **`run --key <k> -- <cmd>`** (optional) holds the lease for the command's life.
- **`release`** (optional) clears an explicit hold.

**Rules for a role:**
- Wrap work that needs a shared resource in the provider (usually inside a job unit).
- **Take only the scope you need.** CPU-only work takes `--lease cpu-heavy`, never a bare
  `--cpu-heavy`: the bare flag also reserves every GPU, and the GPU queue waits behind your CPU suite.
  GPU work asks for `--gpus 1 --min-vram <GiB>` unless it really needs the whole host. A long job with a
  CPU phase and a short GPU phase is two jobs. `job run --place` picks the scope from `--needs` for you.
- Never force-clear a lease you did not take. An unattributed hold is treated as another job's
  hold: find its owner, or wait.

## Wait targets

`--target <kind:ref>` on a wait lets the health quorum check the wait for you:

| kind | resolves from |
| --- | --- |
| `job:<name>` | the job unit of that name (`shoal job run --name <name>`) |
| `lease:<key>` | the lease provider's status for that key |
| `gh-run:<repo>:<run id>` | a GitHub Actions run |
| `gh-pr:<repo>#<n>` | a PR's checks (an empty check list is unknown, never green) |
| `az-build:<id>` | an Azure Pipelines build (optional provider) |

**Behaviour:**
- A target reported resolved (or invalid) triggers a wake.
- An unknown kind is covered only by the periodic validity wake.
- A provider that cannot answer reports unknown, never resolved.
