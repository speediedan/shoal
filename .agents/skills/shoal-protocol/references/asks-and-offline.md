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
5. **After the answers,** remove each answered ask with `queue --answered <n>`. An ask whose answer
   was a condition stays recorded until the condition holds.

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

