# Context handover

A cold turn re-reads the session's whole context, so a session whose context has grown large is cheaper to hand to a fresh
session than to keep. The handover is the same protocol for every session that keeps a start doc ([`start-doc.md`](start-doc.md)):

1. **Advise.** Past its limit, the session is asked once to refresh its start doc and its `Last updated` stamp, then end its
   turn with nothing mid-flight.
2. **Verify.** The start doc meets the contract, and its stamp is at or after the advice (`shoal doc-check`).
3. **Reset.** A new conversation starts in the same seat, with the start doc as its first message, and the old one is retired.

## Shoal roles

Configured per shoal and per role in `features.health`:
- `context_restart_k`: the limit, in thousands of tokens. A dict sets it per role; 0 exempts a role.
- `context_restart`: `page` (the operator restarts) or `auto` (the quorum restarts). A dict sets it per role:
  `{"*": "page", "<slug>": "auto"}`.

The advice is the `CONTEXT` line in `next` and in wakes. The proof of refresh is `queue --restart-ready`. The reset is
`restart --only <role>`, a new incarnation. See architecture §7, "Bounding context".

## Standalone sessions (opt-in)

A session saved in the viewer registry (`shoal ui pick --save`) opts in with its start doc:

```bash
shoal ui handover <label|job> --doc <plan_state.md> --mode auto   # or advise; --k 500 (default 500)
shoal ui handover <label|job> --mode off                          # turn it off
shoal ui handover --list                                          # every saved session: mode, limit, context, step
```

- **Live toggle:** it works on a live session. Opting in while past the limit starts the advice at once: one turn, delivered
  directly (an idle session is stopped and resumed with the message).
- **Who drives it:** after that, the feeder (`shoal ui feed`) checks every opted-in session about every 5 minutes, with no
  model calls. It advises at most once per crossing; in `auto` mode it then resets once the doc is refreshed and the session
  is idle at its prompt.
- **`advise` mode** stops after the advice, and you reset by hand.
- **A stale doc** three hours after the advice shows as `STALLED` in `--list`.

## The reset, per harness

`Backend.reset(session, prompt)` is ecosystem neutral: a new conversation in the same seat, whose first message names the start
doc. Each harness implements it in its own way:

| Harness | Reset |
|---|---|
| Claude | a new background job on the prompt, with the old job's saved name, model and effort, in its directory; then `claude stop` on every job holding the old conversation, watched for 20 s and stopped again if a worker comes back (still live ones show in `--list`). The registry follows the new job, and the session's card follows the registry |
| opencode, Muse, Codex | not yet: the handover stops after the advice, and the reset is done by hand |
| ACP client | the target form: `session/new` (same cwd and MCP servers), then `session/prompt` with the start doc. Any harness reachable over ACP gets a reset with no harness code |

**Why Claude's reset is not `/clear`.** Measured 2026-10-22 on a probe session:
- `/clear` can be delivered headlessly and does clear.
- But a background job that was cleared and later stopped splits in two: the job maps back to its original conversation,
  and the cleared conversation becomes a separate job when resumed.
- Waking the original then fails while the other runs.

So a session cleared by hand runs fine until its worker is retired. A new job does not have that problem.

**Copies of one conversation.** Delivering the advice to an idle session stops and resumes it. When the job's id is not its
conversation's prefix (a long-lived job whose conversation was replaced), the resume starts a second job on the same
conversation. Measured 2026-10-24: the advice resumed b193724a's conversation as job 4decf3de, the reset stopped only
b193724a, and the copy stayed beside the new session. So the delivery follows the live copy, the reset stops every copy,
and the reset removes nothing: `claude rm` of the copy made the background service respawn the stopped original. To clean
up by hand, `claude stop` each copy; do not `claude rm` one while another job holds the conversation. Removal is left to
the guarded prune below, which refuses a job whose conversation a live or protected job shares, and stops every job on
the conversation again after each removal.

## Pruning old incarnations

A reset or restart stops the old session and leaves it in its harness's listing. Over weeks the listings fill with retired
`<slug>-r<N>` sessions. A first dry run on one host (2026-11-01, `--all-specs --standalone --matcher`) found 56 prunable
Claude jobs of 74 shoal created, holding 69 GB (mostly in their `tmp/`), and 66 prunable opencode sessions, 63 of them
matcher sessions leaked before the matcher deleted its own. A prune removes them, preferring purge to an archive that
would only duplicate:

| Harness | Prune |
|---|---|
| Claude | no archive exists, so a purge: a tombstone first (`~/.claude/archived_sessions/jobs/<job>/`: `state.json`, `timeline.jsonl`, `ARCHIVE_INFO.txt`; never `tmp/`), then `claude rm <job>`. The transcript under `~/.claude/projects/` stays (Claude Code expires it after 30 days, and `context-report` reads it). Every other job on the conversation is then stopped again and watched, as a reset does |
| Codex | `thread/archive` through `codex app-server` (a flag on the one stored rollout; `codex unarchive` restores it); `--delete`: `thread/delete` |
| opencode | OpenChamber's archive flag (`POST /api/openchamber/sessions/archive`: opencode 2 has no route that sets `time.archived`); `--delete`: opencode's `DELETE /api/session/<id>`, proxied by OpenChamber. Never the opencode CLI |
| Muse | no API: the session directory moves to `archived-sessions/` beside `sessions/` (same date layout); `--delete` removes it. Refused while `.session.lock` is held |

**What is considered.** Only sessions shoal created: a name `<stem>-r<N>` whose stem is a role slug or title in a spec,
or an id in a spec's generation history. `--standalone` adds Claude predecessors of saved standalone seats (the reset
names the new job after the seat, so an older job of the same name); `--matcher` adds leftover `shoal-matcher-*` opencode
sessions.

**What is kept**, with the reason in the table:
- (a) the current session of any role in any spec, retired ones included, matched by job id and by current and original
  conversation id; (b) any role's parked session; (c) any entry in the viewer registry, dismissed ones too, matched
  by the session and job ids it records (a job that only shares the seat's name, such as a stopped `/bg` fork or an
  older job, is not the seat; only an entry that records neither id is matched by its label);
- (d) a live worker, or state working, blocked or idle; (e) a conversation shared with a job that has a worker or is
  protected;
- (f) a recorded worktree that still exists, a git worktree under the job's `tmp/`, or a process running inside either;
- (g) the job the prune runs in; (h) a held Muse `.session.lock` or shoal turn lock, or a ledger-watch still bound to it;
- anything changed more recently than `--min-age` (default 24 h).

**By hand**, dry-run first:

```bash
shoal prune-incarnations --all-specs --dry-run                       # table per harness: id, name, keep/prune, reason, bytes
shoal prune-incarnations --spec <spec> --harness claude,codex        # apply, 10 per batch (--batch N)
shoal prune-incarnations --all-specs --standalone --matcher --delete --min-age 2d
```

**Automatic** (`features.prune`, default off): once incarnation N proves itself (its first queue write after a quorum
restart, the `first-turn` handover event, or `verify_consumer` in `restart`), the role's incarnations up to N-2 are pruned
with the harness's default mode. N-1 stays one generation as the fallback. A park never prunes. `{"enabled": true,
"delete": true}` deletes instead of archiving where a harness can. Retirement condition: fold into the default after two
clean weeks.

Each removal is one `pruned` telemetry event. A pruned Claude incarnation keeps its name in `context-report`, from its
tombstone and from the spec's generation history.

## Setting the limit

Context cost is measured, not guessed: `shoal context-report [--days N] [--json]` reads this host's Claude transcripts, with no
model calls. Per session it reports:
- calls and calls per day;
- cold calls (more than half the context written fresh) and cold wakes (gaps over the cache's hour);
- average and current context;
- the cost split (warm reads, writes, cold writes, output), and the cost in base-input tokens (cache read 0.1, 1 h write
  2.0, output 5.0);
- the payback of a handover at the current context, in warm calls and in cold wakes;
- turns (each prompt from the maintainer, a peer or a task notification starts one), calls per turn, and the context
  growth per turn;
- for each candidate limit (`--limits`, default 300,400,500), a replay of the window with a handover at that limit: how many
  would have fired, the turns and hours between them, and the turns a fresh session takes to reach the limit at that growth.

The turn count is what a limit means to the session: how often it would be asked to refresh its start doc. Measured
2026-10-24 over three days on speediedl: the busiest standalone seat grew about 19k per turn, so a 500k limit would have
handed it over every 16 to 27 turns (11 to 13 h); most roles grew 12k to 20k per turn (a fresh role reaches 500k in 21 to
34 turns, one role at 35k per turn in 12); the wake relay (since retired) grew under 3k per turn and would not reach it
for 150 turns.

The first health run on each host after midnight UTC writes yesterday's snapshot to
`<data_root>/_context/<server>-<YYYY-MM-DD>.json` (`features.health.context_snapshot`, default on; `context-report
--snapshot` by hand). A few days of snapshots show each session's pattern, busy or mostly idle, and that is what a limit is
set from.

Two regimes were measured (2026-10-22):
- **A busy interactive session** pays 70 to 80% of its cost in warm reads, a tenth of its context on every call. A handover
  at 850k repays after about 8 calls.
- **A mostly idle role** pays mostly in cold rebuilds, twice its context per wake. A handover repays on its first cold wake.

So the token case for a handover arrives early in both. The limit is mostly a judgment about continuity: how well the
start doc carries a session.

## Related upkeep

**Stale workers** (`features.health.stale_upkeep`, default on). A Claude worker running an older Claude Code than the
installed one runs its own updater, which reinstalls the current version. Every reinstall restarts the background service
and drops every attached client: measured 2026-10-22, 124 restarts in a day, driven by three old workers.
- The quorum respawns such a worker when it is idle. That takes no turn: a new worker on the same conversation, idle at its
  prompt.
- A busy one is reported once per version, to be relaunched at a quiet moment.
- The running version comes from the background service's roster (`~/.claude/daemon/roster.json`).

**Reattach.** When the background service restarts under an attached card, the card reattaches the same session, up to three
times. A card whose client binary was replaced by an update reattaches once on the new version.
