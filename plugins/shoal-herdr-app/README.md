# shoal for the Herdr app

Mods for the Herdr app (the GPUI desktop window over herdr-core), as opposed to
`plugins/shoal-herdr/`, which targets the herdr 0.9.x terminal multiplexer. Nothing here changes
how shoal runs; every mod reads shoal's own feeds, and the only verb any mod runs is
`shoal ui app-open` (shoal-home, on a click), which arranges the app's spaces and wakes no role.

| Mod | What it is |
|---|---|
| `mods/lattice-view` | The shoal graph doc v1 (`shoal ui graph --json`) as an animated, hoverable network: a global view of every shoal around the operator hub, a per-shoal view with host regions, a metrics and needs-you rail, and a compact `pulse` mode for an edge drawer. See its `RECIPE.md`. |
| `mods/shoal-home` | The splash and host chooser, natively: the nautical SHOAL logo, the five splash creatures in a kelp network and one card per host (`shoal ui launch --list --json --summary`); a click runs `shoal ui app-open --machine <name>`. See its `RECIPE.md`. |

![lattice-view](mods/lattice-view/preview.png)

![shoal-home](mods/shoal-home/preview.png)

## Install

The mod reads the graph doc by running a program (`exec`) or reading a file, both optionally on
another machine through herdr-core (`on`). It reaches no network itself and writes nothing.

**The mod runs whatever argv its `source` props name, on any herdr-core machine that `on`
names.** Keep that argv a read-only shoal command: `shoal ui graph --json`, or an ssh wrapper
around exactly that. Never point it at anything that writes, wakes a role or sends a message.

1. Copy or link the mod folder into the Herdr app home (`~/.config/herdr-app`, or
   `$HERDR_APP_HOME`):

   ```sh
   ln -s "$PWD/plugins/shoal-herdr-app/mods/lattice-view" ~/.config/herdr-app/mods/lattice-view
   herdr-app check ~/.config/herdr-app/mods/lattice-view
   ```

   `mod.json` says `"place": "none"`, so nothing docks by itself.

2. **Lattice tab.** Open it as a pane, ideally alone in a dedicated tab (labels need the room):

   ```sh
   herdr-app do tab.new '{"name":"lattice","mod":"lattice-view","props":{"source":{"kind":"exec","argv":["shoal","ui","graph","--json"],"on":null}}}'
   # or beside the focused terminal:
   herdr-app do pane.split '{"direction":"right","mod":"lattice-view","props":{"source":{"kind":"exec","argv":["shoal","ui","graph","--json"],"on":null}}}'
   ```

   `on` names a machine from `hc machine ls` when shoal runs there (`"on": "<machine>"`); `null`
   runs it here. Without herdr-core on that machine, use an ssh argv instead:
   `"argv": ["ssh", "<host>", "shoal", "ui", "graph", "--json"]`.

3. **Pulse drawer.** In a cloned layout (`herdr-app do layout.clone '{"name":"main","as":"my-main"}'`),
   add a floating block to the root node of `layouts/my-main.json`:

   ```json
   "floating": [
     { "id": "shoal-pulse", "edge": "right", "align": "end", "peek": 44,
       "content": { "mod": "lattice-view",
                    "props": { "mode": "pulse", "source": { "kind": "exec", "argv": ["shoal", "ui", "graph", "--json"], "on": null } } } }
   ]
   ```

   One row per shoal (its most urgent state's dot, the needs-you count, its tag, messages per
   hour), then up to five needs-you roles, loudest first (the state badge, the role, its wait
   event, its host). Dots, counts and badges sit on the edge side, so they show while the drawer
   peeks. It is still unless the motion tier is `full`, and a poll redraws it only when a row
   changed. Clicking a role row sets the shared key `lattice.focus` (`{node, shoal, t}`; a plain
   node id string works too, for other mods); every Lattice tab honors it by drilling into that
   role's shoal and pinning its card. A shoal row does the same with the shoal's loudest role.

4. **Per-shoal lattice tabs.** Give each shoal's space a `lattice` tab that reads the one shared
   feed file (`shoal ui graph-feed` writes the whole doc each tick) and opens on that shoal,
   instead of a per-tab `exec` that would start Python on the server every poll per tab:

   ```sh
   herdr-app do tab.new '{"name":"lattice","mod":"lattice-view","props":{"shoal":"<shoal id>","source":{"kind":"file","path":"<graph.json>","on":"<machine>"}}}'
   ```

   Escape returns to that shoal rather than to the global view; `g` or the header chip shows
   every shoal and comes back.

5. Run `herdr-app check` after every edit, and `herdr-app status` for `errors`.

### Props

| Prop | Default | Meaning |
|---|---|---|
| `source` | `{"kind":"fixture"}` | `{"kind":"exec","argv":[...],"on":<machine or null>}` (after the first read the mod appends `--since <cursor>`), `{"kind":"file","path":"...","on":...}`, or `{"kind":"fixture","path"?:...}` (the bundled `fixtures/graph-v1.json`, with synthetic events every tick for a demo) |
| `mode` | `"full"` | `"pulse"`: the compact edge drawer (shoal rows, then up to five needs-you roles) |
| `shoal` | none | a shoal id: open drilled into it and treat it as home (Escape returns there; `g` toggles global). In pulse mode, list only that shoal |
| `view` | the home view | a shoal id to open drilled in once (Escape then goes to the home view) |
| `pin` | none | a node id whose card opens pinned |
| `motion` | `"auto"` | `"full"`, `"balanced"`, `"low"`, `"off"` or `"auto"` (see Motion budget; `m` cycles) |
| `budget` | none | overrides for the tier: `{"max_dots", "max_pulses", "frame_ms", "packets", "render_ms"}` |
| `renderer` | probed | `"cpu"` or `"gpu"`: a launcher's hint for `auto` |
| `items` | `true` | `false` hides the item satellites (also `i`) |
| `legend` | `false` | open with the legend shown (also `l`) |
| `poll_ms` | `4000` | data poll interval, 1500 to 60000 |

### Keys and pointer (Lattice tab)

Drag to pan (4 px threshold), wheel up to zoom in at the pointer and down to zoom out (a trackpad
the same way), hover a node to fade everything not connected to it and show its card, click to
pin the card (links open in the browser; a long item or blocker line is cut, its full text on the
line's tooltip; click the `$ shoal view ...` line to copy it), click a cluster to drill in. The
card stays inside the canvas, flipping left of or above its node near an edge. `escape` unpins,
closes the legend, then goes back to the home view (the `shoal` prop's shoal, else global); `g`
toggles the global view; `l` legend (it shows the motion tier), `m`
cycles the motion tier, `f` lays the view out afresh and fits every node, label and cluster ring
inside the canvas, `i` items, `+`/`-` zoom, arrows pan. Node positions are kept across polls: a
poll with the same nodes moves nothing, and a new node is placed without moving the rest. With `"focusable": true`, `pane.go` reaches the pane.

### Motion budget

While anything animates, the Herdr app redraws the whole window, and a frame's cost is what
matters: cheap on a GPU, expensive on a software renderer (on lavapipe with two threads, about a
core-second per frame for a 1280x820 window, whatever the mod draws). So the tiers differ in how
many frames they ask for:

| Tier | Motion | Frames asked for |
|---|---|---|
| `full` | breathing halos and rings, ripples, spinners, marching dots at their own speed, packets | about 30 per second (`loop`) |
| `balanced` | stepped pulses on needs-you and working nodes, dots on the six busiest edges, packets | one shared interval, 220 ms |
| `low` | none (needs-you stays loud statically: ring, badge, rail order); `budget.frame_ms` opts into a needs-you blink | none; data redraws at most every 15 s unless what needs you changed |
| `off` | none | none; data redraws at most every 30 s unless what needs you changed |

In every tier a data poll redraws only when something shown changed. `auto` (the default)
starts at `balanced`, takes `low` on a CPU renderer and `full` on a GPU, and drops a tier for
the pulse drawer, a small pane, or a window without focus. The renderer is read once from the app
process on Linux. First its environment: `SHOAL_LATTICE_RENDERER=cpu|gpu` or
`LIBGL_ALWAYS_SOFTWARE=1` forces it, and `VK_ICD_FILENAMES`, `VK_DRIVER_FILES` or
`GALLIUM_DRIVER` naming a software driver (lavapipe, llvmpipe, SwiftShader) means CPU and naming a
hardware one means GPU. Else its loaded libraries (`/proc/self/maps`), where hardware wins: the
Vulkan loader maps every installed ICD, lavapipe included, so an NVIDIA driver library or a mapped
GPU device node (`/dev/nvidia*`, `/dev/dri/renderD*`) means GPU, and CPU needs a software driver
with no hardware driver library beside it. Every Mesa ICD and no device stays unknown
(`balanced`). The `renderer` prop overrides the probe.

## shoal-home

The home screen: a full-window pane, best alone in a `home` tab of a `shoal` space on the laptop.
It reads one list and runs one verb, both here (`on` the local machine):

- the list: `shoal ui launch --list --json --summary` (read-only, the same list the terminal
  chooser reads), every 20 s and on `↻` / `r`;
- on a click or Enter: `shoal ui app-open --machine <name>`, which creates or focuses that host's
  shoal spaces and remembers the choice.

1. **Copy** the folder (do not symlink it: `image()` in Herdr app 0.1.12 does not load PNGs from
   a mod folder linked from outside the app home, so the creatures would not draw):

   ```sh
   cp -r plugins/shoal-herdr-app/mods/shoal-home ~/.config/herdr-app/mods/shoal-home
   herdr-app check ~/.config/herdr-app/mods/shoal-home
   herdr-app do tab.new '{"name":"home","mod":"shoal-home"}'
   # the bundled demo list instead (a click then only says what it would run):
   herdr-app do tab.new '{"name":"home","mod":"shoal-home","props":{"source":{"kind":"fixture"}}}'
   ```

2. Props:

   | Prop | Default | Meaning |
   |---|---|---|
   | `source` | `{"kind":"exec"}` | `{"kind":"exec","argv":[...],"on":<machine or null>}` (default argv `["shoal","ui","launch","--list","--json","--summary"]`), or `{"kind":"fixture","path"?:...}` (the bundled `fixtures/launch-list.json`) |
   | `motion` | `"auto"` | the lattice tiers; only `full` moves anything (a slow drift of the creatures), and never again after a host is opened |
   | `renderer` | probed | `"cpu"` or `"gpu"`: a launcher's hint for `auto` |
   | `art` | `true` | `false` draws the text screen (logo and host list) |
   | `poll_ms` | `20000` | list poll interval, 10000 to 300000 |

Cards: `local` first, then each route; a green, red or grey dot (reachable, unreachable with the
doctor hint as its tooltip, not probed yet); a ring on the last choice; per shoal its tag, the
roles that need you (in the theme's blocked color), working and waiting; a warning when the Herdr
pin differs. Unreachable cards are greyed and do not click. Keys: arrows or `j`/`k` move, a digit
selects, Enter opens, `r` refreshes. Below 640 by 460, or without the images, the same screen is
text only.

## Development

The mod's logic is in pure ES modules that run under Node (`model.js`, `layout.js`, `geom.js`,
`scene.js`, `motion.js`); `main.js` only fetches, translates the display list into GPUI elements and handles
input.

shoal-home is built the same way (`model.js`, `layout.js`, and `motion.js`, which is a verbatim
copy of lattice-view's: edit it there, then copy; a test checks they match).

```sh
node --test tests/herdr_app/*.mjs                                # unit tests for both mods
node tests/herdr_app/test_lattice_view.mjs                       # unit tests on the fixture
node plugins/shoal-herdr-app/preview/render_svg.mjs --legend --out /tmp/lattice.svg
```

`preview/render_svg.mjs` draws the same display list to SVG for hosts that cannot run the Herdr
app. `preview.png` is a screenshot of the real app (`herdr-app shot`) on the fixture.
