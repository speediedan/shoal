# The start doc (plan_state)

Every session that shoal hands over keeps one **start doc**: a present-tense live plan from which a fresh session can
continue with nothing else. A role's start doc is its spec `doc`. Its brief names it, `shoal next` prints the context advisory
that asks for it to be refreshed, and the new incarnation reads it first. The handover is only as good as this document, so
shoal states what it must hold and checks it: `shoal doc-check <path>`.

## Required

| Section | What it holds |
|---|---|
| `Last updated: <UTC timestamp>` | A line anywhere in the doc, for example `Last updated: 2026-10-22T19:05Z`. Update it whenever the doc is refreshed. The newest stamp counts. It is the handover's proof of refresh |
| Mission | What the session is for and what it delivers |
| Current state | Where things stand now, dated. A "Resume here" section counts |
| Next steps | What to do next, in order. A "Resume here" section counts |
| grants-needed | One line per authorization a successor needs (scope and giver); `- None pending` when there is none |

## Recommended

| Section | What it holds |
|---|---|
| Decisions in force | What was decided, so a successor does not reopen it |
| Running monitors | Every watcher or background job that must survive, with its re-arm command |

**An append-only companion log** with the same file stem is recommended for everything that is history: session
narratives, negative results with their measurements, superseded state. Moving history there keeps the start doc short, which
is what makes a handover cheap. Shoal does not read the log.

**The header** is everything above the first `##` section: the title, the `Last updated` stamp and a few lines of current
state (status, the active item and its blocker, open asks). A peer that needs only a role's state reads just this, with
`shoal doc-head <path>` or `shoal doc-head --shoal <id> --slug <role>`, so it is kept short: at most 40 lines and 3 KB unless
the repository's [`lifecycle.md`](lifecycle.md) settings say otherwise. `doc-check` warns on a header over its cap, or one
holding only the title and stamp; it does not fail on either. `shoal doc-head --all` reports every root start doc's header
against its cap.

Sections are found by keyword in the headings, case-insensitive. `Mission and deliverables`, `Current state (2026-10-22)` and
`Grants-needed` all qualify.

## How shoal uses it

- **The context advisory.** Past `features.health.context_restart_k`, `next` and every wake ask the role to refresh its start
  doc, update `Last updated`, and end with `queue --idle --restart-ready`.
- **Durable references.** Everything the doc names must outlive the session: scripts in `tools/` beside the doc, data in a repo
  or a data directory. A job folder goes when its incarnation is pruned, so `queue --restart-ready` refuses a doc that names one
  still on this host, and notes one that is not. Prune keeps a job its doc names until the doc stops naming it.
- **The proof of refresh.** `queue --restart-ready` runs this check. It refuses a doc that misses a required section, or whose
  stamp predates the first time this session was shown the advisory (or, if it never was, its previous boundary). A refused
  handover costs nothing: the role is mid-turn and fixes the doc.
- **The restart.** In `page` mode the operator restarts the role. In `auto` mode the quorum does: it stops the old session
  and runs `restart --only <role>`. The new session reads the start doc first.

A new role's first doc can be written from a few lines of guidance: `shoal doc-init <path> --role <slug> --mission "<what
it is for>" [--shoal <id>] [--title T] [--next "a; b"] [--grants "scope (giver); ..."]` writes one that meets this contract
(the Herdr app's New role does the same with "Write a starter doc"). It never overwrites an existing file, and the new
session is expected to make it its own at its first boundary.

```bash
shoal doc-init <path> --role <slug> --mission "<guidance>"   # a starter doc; --dry-run prints it
shoal doc-check <path>                          # ok, or each missing section
shoal doc-check <path> --since 2026-10-22T19:00Z   # also: stamped at or after this time
shoal doc-check <path> --json
shoal doc-head <path>                           # the header only (a role's state, cheaply)
shoal doc-head --all                            # every root start doc's header against its cap
```

The whole handover, including opt-in handover for standalone sessions and the reset per harness: [`handover.md`](handover.md).
What names each file in a start doc's directory, before anything there is moved: [`refs.md`](refs.md).
