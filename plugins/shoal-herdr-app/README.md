# shoal for the Herdr app

Mods for the Herdr app (the GPUI desktop window over herdr-core), as opposed to
`plugins/shoal-herdr/`, which targets the herdr 0.9.x terminal multiplexer. Nothing here changes
how shoal runs; every mod is read-only over shoal's own feeds.

| Mod | What it is |
|---|---|
| `mods/lattice-view` | The shoal graph doc v1 (`shoal ui graph --json`) as an animated, hoverable network: a global view of every shoal around the operator hub, a per-shoal view with host regions, a metrics and needs-you rail, and a compact `pulse` mode for an edge drawer. See its `RECIPE.md`. |

![lattice-view](mods/lattice-view/preview.png)

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

   Each row is one shoal: its most urgent state dot (blinking while something needs you, when the motion tier allows), the
   needs-you count, messages per hour and a sparkline. Clicking a row asks the Lattice tab (if
   open) to drill into that shoal with the loudest role's card pinned.

4. Run `herdr-app check` after every edit, and `herdr-app status` for `errors`.

### Props

| Prop | Default | Meaning |
|---|---|---|
| `source` | `{"kind":"fixture"}` | `{"kind":"exec","argv":[...],"on":<machine or null>}` (after the first read the mod appends `--since <cursor>`), `{"kind":"file","path":"...","on":...}`, or `{"kind":"fixture","path"?:...}` (the bundled `fixtures/graph-v1.json`, with synthetic events every tick for a demo) |
| `mode` | `"full"` | `"pulse"`: one compact row per shoal |
| `view` | `"global"` | a shoal id to open drilled in |
| `pin` | none | a node id whose card opens pinned |
| `motion` | `"auto"` | `"full"`, `"balanced"`, `"low"`, `"off"` or `"auto"` (see Motion budget; `m` cycles) |
| `budget` | none | overrides for the tier: `{"max_dots", "max_pulses", "frame_ms", "packets", "render_ms"}` |
| `renderer` | probed | `"cpu"` or `"gpu"`: a launcher's hint for `auto` |
| `items` | `true` | `false` hides the item satellites (also `i`) |
| `legend` | `false` | open with the legend shown (also `l`) |
| `poll_ms` | `4000` | data poll interval, 1500 to 60000 |

### Keys and pointer (Lattice tab)

Drag to pan (4 px threshold), wheel to zoom at the pointer, hover a node to fade everything not
connected to it and show its card, click to pin the card (links open in the browser; click the
`$ shoal view ...` line to copy it), click a cluster to drill in. `escape` unpins, closes the
legend, then goes back to the global view; `l` legend (it shows the motion tier), `m` cycles the
motion tier, `f` fit, `i` items, `+`/`-` zoom, arrows pan. With `"focusable": true`, `pane.go` reaches the pane.

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
process (`/proc/self/environ`, then `/proc/self/maps`, on Linux): a lavapipe, llvmpipe or
SwiftShader ICD means CPU. `SHOAL_LATTICE_RENDERER=cpu|gpu` in the app's environment, or the
`renderer` prop, overrides the probe.

## Development

The mod's logic is in pure ES modules that run under Node (`model.js`, `layout.js`, `geom.js`,
`scene.js`, `motion.js`); `main.js` only fetches, translates the display list into GPUI elements and handles
input.

```sh
node tests/herdr_app/test_lattice_view.mjs                       # unit tests on the fixture
node plugins/shoal-herdr-app/preview/render_svg.mjs --legend --out /tmp/lattice.svg
```

`preview/render_svg.mjs` draws the same display list to SVG for hosts that cannot run the Herdr
app. `preview.png` is a screenshot of the real app (`herdr-app shot`) on the fixture.
