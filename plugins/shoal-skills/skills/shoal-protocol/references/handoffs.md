# Handoffs

A restarted or replacement session inherits nothing except what is written down.

## Authorizations do not transfer

A standing grant was given to a session by the maintainer. A relayed grant is unverifiable from
where the successor sits. So:
- the outgoing session describes what it holds and who granted it;
- the incoming session obtains its own grants.

## The handoff document

One file per role, `<role>.md`, updated in place. Before each update, copy the current file to
`archive/<role>_<UTC YYYYMMDDTHHMMSSZ>.md`. It opens with this header:

```markdown
# Handoff: <role>

- handing-off-model: <provider/model that wrote this update>
- handoff-local-ts: <local timestamp with zone>
- handoff-utc-ts: <UTC timestamp>
- supersedes: <UTC timestamp of the previous update, or `none`>
- base: <repo@sha the state below was read against>
```

It carries:
- the repositories and their CI shape;
- open items with their blocking conditions;
- the measurements and negative results a successor would otherwise rediscover;
- a `grants-needed:` list with one line per authorization the successor needs (scope and giver):
  `- None pending` when there is nothing, `- UPCOMING: ...` for grants that ripen later,
  `- PRE-AUTHORIZED: ...` for standing pre-authorizations, and `- STANDING: ...` for rules about
  future asks.

**Handoffs stay model-agnostic.** They name files, commands, shas and measurements, never the model
or provider that produced them (the header's `handing-off-model` line excepted), so a session in
any harness can consume them.

## The successor's first act

Before any work, ask the maintainer first-person for ALL of the `grants-needed` items in ONE
question, with a grant or deny option on each and your stacked-work option. This is the only
sanctioned interactive step of a restart.
