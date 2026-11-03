# Shoal actions

A compact dialog (640 by 540) for operating a shoal: pick a shoal and a role, pick an action, read
the CLI's dry-run preview and wake cost, and confirm. It replaces herdr 0.9.3's popup palette.

## What you see

- **Header:** `Shoal actions`, the machine the CLI runs on in the theme's dim color (`on <machine>`
  or `on this computer`), a refresh glyph and `Esc`. Below, a row of shoal chips (tag, needs-you
  count) and a row of role chips; a role or shoal that needs you carries a dot in the theme's
  `blocked` color. `* everyone` shows for Message only; for shoal-wide verbs the role chips dim.
- **Verb buttons:** Message, Offer, Withdraw, Next, Asks, Offline, Freeze, Status, Conditions,
  Upgrade preview (wrapping; the chosen one in the accent color).
- **Body** (scrolls): the verb list with one line each; a write verb's form (text inputs, a wake
  switch, on/off chips); the plan (command and dry run in a monospace block, the wake cost in an
  amber band); the result (rc, command, output); a view's text; or an error.
- **Footer:** a key hint, then Cancel and Preview, Cancel and Confirm (amber when the plan names a
  cost), Another and Close, or Back and Close.

## How it behaves

- Every call is `exec("shoal", argv, {on: machine})`, machine from the role's card terminal
  (`shoal.pane=<shoal id>/<role>`) and its space. Writes: `ui act-plan <verb> ... --json`, then on
  Confirm `ui act-apply --token <t> --json`. A `confirm: false` plan (offline) applies at once and
  still shows everything. Views: `ui act-plan <verb> --shoal <id> [--role <slug>] --json`.
- Changing the shoal or role drops a plan and re-reads an open view. A reply that a newer call
  has overtaken is ignored; while an apply runs, the target and verb stay put.
- Esc closes from the verb list, else goes back a step; Enter previews, then confirms; Tab moves
  between text fields. Answer in the asks view opens Message to that role with wake on.

## Built from

- `mod.json` `{"place":"none","focusable":true,"overlay":{"kind":"dialog","width":640,"height":540}}`.
- `main.js` (View): `exec`, `terminals`, `focused`, `spaces`, `panes`, `theme`, `closeOverlay`,
  `nextCommand`, `InputState` + `Input`. `model.js` (pure, Node-tested): verbs, forms, argv,
  reply parsing, preselection, keys, fixture answers, palette. `fixtures/act.json`: one reply of
  every kind.

## Watch out

- Free text goes as `--opt=value`, so a message starting with a dash stays a value.
- The token is never rendered or logged and is used once.
- An input's submit and the root key handler both see Enter: one press is debounced to one action.

## Done when

- `herdr-app check` passes; `node --test tests/herdr_app/test_shoal_actions.mjs` passes.
- With `props.fixture: true`, every screen renders: the menu, each form, a plan with its amber
  cost, an offline result applied without a second click, the asks view with Answer, an error.
