# Asks, and user-offline mode

## Raising an ask

**Only the session that needs a decision asks for it, first-person, as a prompted question** with
options that include a decline or hold. A grant relayed by another session is unverifiable from
where you sit, and a question buried in a status report is not an ask.

1. **Run `next` first.** If it says USER OFFLINE, do not raise the question: record it with `shoal
   queue --ask "<question>"` and continue with unblocked work. It is raised when the maintainer
   returns.
2. **Record the ask and a `user-auth` (or `user-design`) wait before calling the question tool.**
   The tool blocks your turn until it is answered, so without the record your lane looks dead and
   the health quorum pages about a stale wait.
3. **Build the question sets with `shoal asks --shoal <id> --slug <you> --question-json`.** It
   labels every PR from live data (title, state, head, checks), so a question can never describe a
   different PR from the one being merged. Raise every set, one question-tool call per set, at most
   four questions each.
4. **Every merge ask offers grant, defer, delegate and deny,** and names the stacked or parallel
   work you will do meanwhile. Never leave a bare merge-or-nothing choice.
5. **After the answers,** remove each answered ask with `queue --answered <n>` (or its id, which does
   not shift as you remove others), in the same `queue` write that records what you do next
   (`queue --answered <id> --wait remote-ci ...`, or with `--active`): one event. A write between
   the answer and the removal can show the maintainer the answered ask again, as a need. Run that
   `queue` unpiped, never through grep or head: an `ERROR` (an ask number out of range) is the only
   sign that it wrote nothing. An ask whose answer was a condition stays recorded until the
   condition holds.

## Answers from the Asks dialog

The maintainer can answer your recorded asks from the viewer's Asks dialog instead of your
question, in any harness:

1. **The answer arrives as mail with a wake,** one line per ask:
   `re ask <n> [<id>] "<opening words>": <choice>. <detail>`. The id is a short hash of the ask's
   text and the quote is its opening words, as they were when the maintainer answered.
2. **Match on the id and the words, never on the number.** You may have reordered, replaced or
   re-raised your asks since. Under the message, `next` says which of your current asks each line
   answers (act on that one, then `queue --answered <its id>`). It may also say a line names
   an ask you no longer have: do not apply that answer to another ask. Tell the maintainer
   (`msg --to user`), and raise the ask again if it still applies. A line that opens like one of your
   asks whose text has changed (a new head SHA, say) is not a grant for the new text: confirm first.
3. **An answer can close your open question.** If your question prompt is still open when a dialog
   answer arrives, shoal stops that turn and resumes you with the answer. Your wake then starts with
   YOUR OPEN QUESTION WAS CLOSED. Take the answer from `next`, and raise again only what it leaves
   open. If a client is attached to your session, the answer waits for the person there instead.

## Headless turns (Codex, Muse)

A turn started by `codex exec` or `muse exec` has no terminal and no question tool, so it never
prompts. Its asks are still first-person, and the maintainer still answers from a set of choices:

1. **Record the ask** with `shoal queue --ask "<question>"`: your role, the item, and for a merge the
   PR and its head SHA. Write the question, not the options: the viewer's Asks view shows every
   recorded ask with its answer set (grant, defer, delegate review and deny for a merge; grant,
   defer and deny otherwise), the same set `shoal asks` builds for the question tool.
2. **Keep it recorded.** A still-valid ask from an earlier session of your role stays as it is:
   grants never transfer, but the ask is yours to keep, so do not clear it only to record it again.
3. **The answer arrives as mail**, `re ask <n> [<id>] "<opening words>": <choice>. <detail>`, read
   in `next` at your boundary (the maintainer's answer wakes you). Act on the ask `next` maps it to,
   then `queue --answered <its id>` (see "Answers from the Asks dialog" above).

## Dropping an ask

An ask that no longer applies is dropped with `queue --drop-ask <n> --drop-reason <why>`, after
checking the item itself (the PR, not a build id you remember). An unanswered drop is recorded, and
`next` lists it under DROPPED ASKS until you disclose it to the maintainer in your next question set.
`--asks-clear` also requires a reason.

## User-offline mode

The maintainer runs `shoal offline on` before stepping away and `shoal offline off` on return.

**While offline:**
- `next` and every wake carry the USER OFFLINE banner.
- Do not wait on the maintainer and raise no prompted question.
- Record each blocking ask with `queue --ask`, stack PRs where work depends on an unmerged one,
  and take the next unblocked item.

**On return** (and whenever `next` prints ASKS TO RAISE):
1. Re-verify every recorded ask against current state. The LIKELY-STALE hints in `next` (merged
   or closed PRs, finished items) are a start, not a verdict.
2. Drop the ones that no longer apply, with a reason.
3. Raise the rest as question sets, following steps 2 to 5 above.

## A question while your job runs

When a job unit or CI build of yours is running, that wait stays your status
(`queue --wait local-ci --target job:<name>` or `remote-ci`) and your question goes beside it
(`queue --ask "<question>"`). Do not switch to a `user-*` wait: the work is not blocked on the
maintainer, only your next decision is. Health and the viewer then read you as waiting on your job,
with an ask open.

