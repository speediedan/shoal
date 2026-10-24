# Providers

SHOAL depends on a few host facilities through narrow contracts. The system ships
implementations for common setups; any other environment can supply its own by meeting the
contract. Configure providers in `config/host.toml` or with the environment variables named below.

## Resource leases

**Purpose:** serialize work that needs a scarce shared resource (GPUs, a memory-heavy slot, a
single test device) across roles, users and CI.

**Configuration:** `SHOAL_LEASE_CMD`, the command to run; `GPU_LEASE_CMD` is accepted as a fallback.
Unset means no lease provider: `lease:` wait targets resolve as unknown and are covered only by the
periodic validity check.

**Contract:**

| invocation | required | behaviour |
| --- | --- | --- |
| `<cmd> --status --json` | yes | Exit 0. Print one JSON object to stdout: `{"leases": [<lease>], "queue": [<waiter>]}` |
| `<cmd> [--lease <key>] -- <command>` | recommended | Block until the lease is free, hold it for the command's life, and exit with the command's status (75 if the lease could not be acquired within `--timeout`) |
| `<cmd> --hold --pidfile <p>` / `--release --pidfile <p>` | optional | Acquire in a background holder and release explicitly, for multi-step jobs |

- **`<lease>`:** `{"key": str, "held": bool}`, plus, when held: `"holder"` (who took it, free
  text), `"pid"` (int or null), `"since"` (ISO-8601), `"cmd"` (str or null) and `"container"`
  (bool).
- **`<waiter>`:** `{"key": str, "waiter": str, "since": str}`. The queue may be empty when the
  mechanism cannot enumerate waiters (for example a `flock`).

**How SHOAL uses it.** A wait with `--target lease:<key>` is pending while the lease named `<key>`
is held by, or queued for, the waiting role, and resolved otherwise. A provider that fails or prints
invalid JSON makes the target unknown, never resolved.

**Rolling your own.** Any scheduler that can answer "who holds `<key>` right now" can be a
provider: a file lock, a Slurm reservation, a Kubernetes lease object, a Redis lock. The only hard
requirement is `--status --json`.

## Wait targets

A role's `queue --wait ... --target <kind>:<ref>` names what clears the wait, so the health quorum
can resolve it without the role. Each kind resolves to `pending`, `resolved` or `unknown`:

| kind | ref | resolves from | ships with |
| --- | --- | --- | --- |
| `job` | the name given to `shoal job run --name` | the job unit's state: active is pending, an exit-code file is resolved, and a missing unit with no exit file is unknown | core (needs a `systemd --user` host) |
| `lease` | a lease key | the lease provider above | core |
| `gh-run` | `<owner/repo>:<run id>` | `gh run view` | core (needs `gh`) |
| `gh-pr` | `<owner/repo>#<n>` | `gh pr view`: a merged or closed PR is resolved; for an open one, `gh pr checks`: any pending check is pending, and an empty check list is unknown, never resolved. With upstream semantics (an `upstream` wait or an `upstream:` blocker) an open PR is pending whatever its checks say, because the dependency is the merge, not the CI | core (needs `gh`) |
| `gh-issue` | `<owner/repo>#<n>` | `gh issue view`: open is pending, closed is resolved (for `upstream` waits) | core (needs `gh`) |
| `az-build` | `[<project>/]<build id>` | `az pipelines build show`: notStarted, inProgress and postponed are pending. A bare id is looked up in each of `azure_projects` in turn (default `[azure_project]`), because the CLI needs the project and an id from another project reads "not found". A timeout is unknown: `az` with no Azure DevOps credential waits rather than failing, so a token exported only from a login profile needs `login_shell_tools = ["az"]` | optional (needs `az`) |

GitHub refs may be written `repo#n` or `repo:n` and take the configured `default_owner`; they are
stored in the canonical `owner/repo#n` form. An unknown kind or an unparseable ref is refused when
the wait is written, and so is a target that is already resolved.

Under wait discipline, a target its provider confirms pending gets no periodic `wait-validity` wake: `wait-resolved`
wakes the role when it ends, and the wait's rescan cap still expires it. A target that reads unknown keeps the validity
wakes, and their detail says UNVERIFIABLE with the provider's reason.

## Board source

The priority table the digest reads. Today: a GitHub Projects v2 board (`gh` GraphQL). A source
returns items in board order with `item`, `repo`, `number`, `kind`, `title`, `state`, `status`,
`labels`, `excerpt`, `blocked_by`, `parent` and `linked_prs`.

## Harness adapters

How SHOAL starts a session, sends it a prompt and wakes it:

| ecosystem | create | wake |
| --- | --- | --- |
| Claude Code | `claude --bg` with the brief | a wake file the role's own watcher reads; a relay role for dead watchers |
| opencode (+ openchamber) | a server session, then the brief sent as its first prompt | a direct send to the session |
| Codex | not yet implemented | not yet implemented |

## Semantic matcher model routes

Free models through `opencode run`, then the configured paid tiers. See `architecture.md`.
