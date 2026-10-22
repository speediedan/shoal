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
  directly (an idle session is stopped and resumed with the message; never by relay).
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
| Claude | a new background job on the prompt, with the old job's saved name, model and effort, in its directory; then `claude stop` on the old job. The registry follows the new job |
| opencode, Muse, Codex | not yet: the handover stops after the advice, and the reset is done by hand |
| ACP client | the target form: `session/new` (same cwd and MCP servers), then `session/prompt` with the start doc. Any harness reachable over ACP gets a reset with no harness code |

**Why Claude's reset is not `/clear`.** Measured 2026-10-22 on a probe session:
- `/clear` can be delivered headlessly and does clear.
- But a background job that was cleared and later stopped splits in two: the job maps back to its original conversation,
  and the cleared conversation becomes a separate job when resumed.
- Waking the original then fails while the other runs.

So a session cleared by hand runs fine until its worker is retired. A new job does not have that problem.

## Related upkeep

**Stale workers** (`features.health.stale_upkeep`, default on). A Claude worker running an older Claude Code than the
installed one runs its own updater, which reinstalls the current version. Every reinstall restarts the background service
and drops every attached client: measured 2026-10-22, 124 restarts in a day, driven by three old workers.
- The quorum respawns such a worker when it is idle. That takes no turn: a new worker on the same conversation, idle at its
  prompt.
- A busy one (the wake relay runs relay-watch) is reported once per version, to be relaunched at a quiet moment.
- The running version comes from the background service's roster (`~/.claude/daemon/roster.json`).

**Reattach.** When the background service restarts under an attached card, the card reattaches the same session, up to three
times. A card whose client binary was replaced by an update reattaches once on the new version.
