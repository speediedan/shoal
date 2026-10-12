# Failure-mode metrics

How each catalogued failure mode is measured, so its frequency and duration can be tracked across
reviews. `scripts/fm_report.py` computes the derivable ones. Read this before adding a metric:
most failure modes can be derived from streams shoal already writes.

## Sources and their retention

| source | what it holds | durable? |
| --- | --- | --- |
| ledger (`shoals/<id>/ledger/*.jsonl`) | queue (posture, next, `next_blocked`, `next_blocked_meta`, asks), state (sub, target, rescan), claim, release, done, offer, msg, system | yes (git) |
| telemetry (`<data root>/<id>/telemetry/events-*.jsonl`) | `session_transition`; `watch_event` kinds `health` (`<condition> step<N>:<wake\|page> -> <channel>`), `job-start`, `job-verdict`, `ledger-wake`, `digest-refresh`; `pr_merged`, `issue_closed` | yes |
| state dir (`/tmp/<id>`) | job `.json`/`.rc`, wait proofs, watcher sidecars, `health.json` | **no**: wiped at reboot |
| health journal | the same ladder actions as telemetry | depends on journald rotation |

Keep durable metrics in the ledger or telemetry, never in the state dir alone.

**Not recorded anywhere yet:**
- health condition onset and clear (only ladder actions are);
- per-role candidate counts at a point in time (the digest is overwritten);
- a job's end independent of a waiter.

## Map

| FM | metric (`fm_report.py` name) | derivation | history |
| --- | --- | --- | --- |
| 1 | `FM1:active_silent_h`, `wake:turn-ended-active` | ledger: `active` segments with no ledger write by the role for more than 90 min (a proxy, since turn state is not logged); telemetry: ladder wakes | yes |
| 2 | `FM2:idle_all_next_blocked_h` | ledger: posture `idle` and every next item has a blocker. Overstates FM-2: the candidates at that time are not recorded | yes, from the start of `next_blocked` |
| 3 | `FM3:idle_on_hold_gt4h_h` | as FM-2, where a `hold:` blocker's `next_blocked_meta.first` is older than `hold_ttl_min`. An age proxy, not a truth test | from the start of the metadata |
| 4 | none yet | ledger: a `release` with `suggest=` an offer-only role and no later offer of that item. The real evidence is in the harness question history | weak |
| 5 | `wake:wait-resolved` (upper bound) | telemetry ladder wakes joined to the wait target at fire time | partial |
| 6 | `FM6:target_blanked` | ledger state events: a CI wait with an empty target within 5 min of the same substate with one | yes |
| 7 | `FM7:no_verdict_and_gap` | job `.rc` mtime as the end; no `job-verdict` event, and the role's next ledger write more than 10 min later. Use this joint signature: a gap alone includes normal work after a wake | until the last reboot |
| 8 | `FM8:done_items_in_next`, `FM8:done_in_next_h` | ledger: a `done` item that appears in a later `next`, until it leaves | yes |
| 9 | `FM9:hold_renewals`, `page:hold-long` | ledger: increments of `next_blocked_meta.n`; telemetry: pages | from the start of the metadata |
| 10 | `FM10:localci_with_stale_hold_h` | ledger: waiting `local-ci` while a hold is older than `hold_ttl_min` | from the start of the metadata |
| 11 | none (snapshot only) | `ps` for poll loops whose parent is init | no |
| 12 | none (snapshot only) | the instruction sync status, and `git status` of the versioned files | no |
| 13 | none | a liveness probe error is not emitted | no |
| 14 | `FM14:wakes_no_path`, `FM14:respawns`, `FM14:relay_relaunches` | telemetry health actions: a wake whose channel was `no-adapter`; a `; respawned` channel; a `relaunch` step | yes (the no-path count) |
| all | `pages:total`, `wake:<condition>`, per-role idle hours, job end-to-verdict latency | telemetry and ledger | yes |

## Gaps, and the smallest addition for each

| gap | addition | covers |
| --- | --- | --- |
| condition onset and duration | DONE 2026-10-12: the `health_condition` telemetry event (event open/close, slug, condition, key, since, duration_min); fm_report `cond-open:<condition>` counts and `cond-h:<condition>` hours | 1, 2, 3, 9, 10 directly, and every future condition |
| candidates at the time | per-role strong-candidate counts in the `digest-refresh` event | 2 |
| job end without a waiter | a `job-end` event from the unit wrapper that writes the rc file | 7 (durable) |
| answered asks | the maintainer's answer recorded as a ledger `msg` with `re=<ask>` | 4 |
| probe and host hygiene | a `probe-error` event from the liveness probe; orphan-waiter and instruction-drift counts from `doctor` | 11, 12, 13 |

The first row is the highest-leverage addition: one event type makes every health condition,
current and future, measurable for frequency and duration without a new metric per failure mode.

## Reading the trend

- The trend column compares the later half of the window with the earlier half. A metric whose
  instrument began inside the window (for example the blocker metadata) reads "up" because it
  begins at zero, so compare it only across windows where it existed throughout.
- The last day of the window is partial. Read it as a rate, not a total.
- Normalize by load when lanes or jobs change a lot: pages and wakes per active lane-hour, and
  failures per job.

## Baseline (the first measured window, 2026-09-23 to 2026-10-01)

These are the numbers later reviews compare against. They were taken before the fixes for FM-6 to
FM-9 landed.
- Pages: 37 in total, rising to 10 per day at the end, driven by `hold-long`.
- Wakes: 151 `wait-expired` and 120 `wait-validity`, the majority of all wakes.
- `FM6:target_blanked`: 117.
- `FM8:done_items_in_next`: 12 items, 26.7 h.
- `FM7:no_verdict_and_gap`: 10, all on one day.
- Job end-to-verdict latency: p50 7 s, max 56 s.
- Idle with all next items blocked: 151.8 h across roles.
