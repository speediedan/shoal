# Shoal home

The shoal splash screen and host chooser, drawn natively: the SHOAL logo with five plush sea
creatures around it (three fish, a seahorse and a blowfish), and one card per host that can serve
the shoal view, `local` first. Each card says whether the host is reachable, which shoals run
there, how many roles need you, and whether its Herdr pin is right. Click a card to open that
host's shoal spaces in the app.

![Shoal home](preview.png)

## What you see

- **Background:** the lattice view's canvas: a near-black violet to navy gradient (`#14111a` to
  `#03050b`, 165 degrees) with a navy wash (35 degrees), and a soft static bloom behind the logo
  (six stacked ellipses, blue then cyan, 2.2 percent alpha each).
- **Top right:** a `↻ refresh` button (96 by 24, steel `#1c2a30` at half alpha).
- **Logo:** `SHOAL` at 84 px times the scale, weight 800, in a display serif (Noto Serif Display, else
  DejaVu Serif, else Liberation Serif, else Geist), so it reads like a ship's name. Layers, back to
  front: a drop shadow `#020a18` at 70 percent offset by font/16; a navy `#062a4f` outline (eight copies
  offset by font/34: GPUI text has no stroke); the letters in deep sea blue `#1f6fd8`; and their top 53
  percent again in sunlit `#7fe7ff`, clipped, so the letters sit half above a waterline.
- **The sea** (`ocean.js`, a pure display list): kelp strands rise from a seabed line just above the
  tagline, only in the room either side of the logo (a strand's sway is clamped to its side). Each
  strand is a dark `#0d4f4c` body, a `#1f8f7a` middle and a light `#5fd3b0` midrib, with three leaf
  blades and two or three floats (`#47d6b4`, a glow ring and a highlight). The floats are a network:
  tendrils (`#38c8ff`, a 7 percent halo under a 42 percent line) link same-height floats of
  neighbouring strands and link each creature's rim to its nearest float; a tendril that would cross
  the logo is dropped. Below the logo, two foam waves; above its right end, four rising bubbles. Items
  of one style are merged into one path (about 40 layers in all), and at the `full` motion tier each
  strand sways a few pixels natively.
- **Creatures** (`assets/fish-*.png`, 384 px RGBA), placed like the terminal splash: coral
  centered above the logo, the seahorse above left, the blowfish above right, teal left of the
  logo, violet right of it. Size in units of B = 92 px times the scale: coral 1.1, seahorse 0.85,
  blowfish 0.95, teal and violet 1.35.
- **Tagline** in amber `#ffb547` at 78 percent: `stigmergic · heterogeneous · open · agent ·
  lattice`; then `which host serves the view?` in ink `#d4d4d4`, semibold.
- **Host cards** (248 to 340 px wide, up to four per row, rows balanced and centered, radius 10,
  `#0b0e12` at 92 percent, steel border): header with the reachable dot (green `#50d090`, red
  `#ff4d5e` for unreachable, grey `#6b7780` for not probed yet), the host name (14.5 px semibold),
  a `last` chip, a `N need you` pill in the theme's `blocked` color, and the host's digit key.
  Lines below, 18 px each: the route (`this machine` for local) in Geist Mono; one row per shoal
  (up to four, then `+n more shoals`): a tint dot, the tag in its pastel, `N need you` in the
  blocked color, then `W working · Q waiting · R roles`; `⚠ Herdr pin differs: shoal ui doctor`
  in amber when `pin_ok` is false; and the open status.
- **Last choice:** a cyan ring 5 px outside the card. **Keyboard selection:** a brighter border.
- **Footer:** `click a host to open · ↑↓ or j k move · 1-9 jump · Enter open · r refresh`.
- **Text screen:** below 640 by 460, when the art does not fit, with `art: false`, or when an
  asset file is missing: the same logo (48 px, 34 px under 420 wide), tagline and cards, no
  creatures, scrolling when the cards need it.

## How it behaves

- Reads the host list once at start, every 20 s (`poll_ms`), and on `↻` or `r`. A poll that
  changes nothing does not redraw. A failed refresh keeps the last good list and says so in the
  footer.
- **Clicking a card** (or Enter on the selected one) runs `shoal ui app-open --machine <name>`
  here. The card shows `opening…`, then `✓` and the command's summary (a JSON `summary`,
  `message` or `error` field, else its last stdout line), or `✕ exit N:` and the error's last
  line in red. Success rereads the list, so the ring follows the new last choice. One open at a
  time. With the fixture source a click only says what it would run.
- **Unreachable cards** are at half opacity and do not click; the dot and card carry the doctor
  hint as a tooltip. A not-probed card (`reachable: null`) can be clicked.
- **Keys** (the mod is focusable): arrows or `j`/`k` move (wrapping), a digit selects that host,
  Enter opens, `r` refreshes. The mouse is primary.
- **Motion:** only at the `full` tier (lattice-view's budget, `auto` by default: full on a GPU
  renderer with the window focused), each creature drifts 10 px times the scale up and down with
  a native `.loop("top")`, periods 4.7 to 6.4 s. Every other tier is still. All loops stop for
  good once a host has been opened, because any native animation redraws the whole window.

## Built from

- A pane mod (`mod.json`: `place: "none"`, `focusable: true`), opened as a tab:
  `tab.new {name: "home", mod: "shoal-home", props}`.
- Props: `source` (`{kind: "exec", argv, on}`, default argv
  `["shoal","ui","launch","--list","--json","--summary"]` here, or `{kind: "fixture", path?}`),
  `motion`, `renderer`, `art`, `poll_ms`.
- `main.js` (the View), `model.js` (list parsing, card lines, keys, open results), `layout.js`
  (composition per window size), `motion.js` (a verbatim copy of lattice-view's).
- Herdr: `exec` (the list and `ui app-open`), `readTextFile` (the fixture; `/proc/self/environ`
  and `/proc/self/maps` once for the renderer probe), `fs/promises` `exists` (the assets), `theme()`,
  `nextCommand` (`focus`, `refresh`, `open`), `window()`.
- GPUI: `image()` for the creatures, `window.paint_path` for the gradient and the bloom, absolute
  divs for everything else, `on_size_change` on the root.

## Watch out

- **Copy the folder, do not symlink it.** In Herdr app 0.1.12 `image()` does not load a PNG
  from a mod folder that is a symlink pointing outside the app home ("Embedded resource not
  found"), though `svg()` does. The existence check passes through the link, so the layout keeps
  room for creatures that never draw.
- Card heights are computed from the line count (`layout.cardHeight`), so every line must stay
  exactly 18 px (`.h(CARD.line)`), or the cards clip.
- The logo width is an estimate (3.5 em). Noto Serif Display at weight 800 fits it; a much wider face would crowd the side fish.
- The list contract is additive: unknown fields are ignored, a repeated host name keeps its first
  row, a `last` naming no host is dropped.

## Done when

- `herdr-app check` passes on the copied folder.
- With `{"source":{"kind":"fixture"}}`: five creatures around the logo at a full-size window, five
  cards (`local` first), `lane-a` ringed and selected, `lane-b` greyed with its hint as a tooltip,
  `lane-c` grey dot `not probed yet`, `lane-d` with the pin warning.
- With `art: false`: the text screen.
- `node --test tests/herdr_app/test_shoal_home.mjs` passes.
