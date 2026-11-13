# Lattice view

A dark, glowing network of every shoal (a group of coding-agent roles coordinating through a git
ledger): each shoal a cluster of role nodes around the operator ("you") hub, edges for messages,
asks, offers, grants and blockers with dots marching along the busy ones, and every role that
waits on you pulsing loud enough to see from across the room. A rail on the right lists who needs
you, per-shoal throughput and per-host load.

![Lattice view](preview.png)

## What you see

- **Canvas** (left, fills the pane): a near-black violet to navy gradient (`#14111a` to
  `#03050b`, 165 degrees) with a cool navy wash and a soft bloom behind the hub. Header top left in
  Geist Mono 11px: `LATTICE > all shoals`, counts below in `#8c8c8c` 9.5px.
- **Global view:** shoals on a ring around the `you` hub (22px ring, magenta `#ff4f9a` with a count
  badge when asks or grants are open). Each shoal is a translucent disc in a pastel tint (olive
  `#b0b070`, mint `#50d090`, teal `#70b090`, ...), its name above, `N need you` in
  magenta. Roles sit on a ring inside; hovering the middle of the disc shows the shoal's status. External writers (dashed circles) sit
  outside, an aliased one beside its home role with no label of its own (the alias tether says
  it). Hosts show as the dot before each label and in the rail; a role whose CI job runs on
  another host gets a small chip (`remote-ci -> host-b`) in that host's color.
- **Per-shoal view** (flat; in depth it is the cutout sphere, below): the shoal's roles on one ring split into host sectors: translucent annular
  bands in the host color, labeled with the host name (`⌂` marks the shoal's home). The hub in the
  middle, the shoal's status on a hover anywhere in the hole of the ring, externals outside.
- **Nodes:** circles (the agent's type is in the hover card), size by work done
  in 24 h, a host-colored dot before each label, a state badge top right, an olive `⚑` grant badge
  top left. State encodings:

  | state | look |
  |---|---|
  | working | teal `#2ee6c0` body, slow breathing halo |
  | user-auth | hot orange-red `#ff5a2c`, fastest pulse and ring, `⚷` badge |
  | user-ask / user-prompt | magenta, fast ring, `?` / `⏎` |
  | user-design | magenta, `✎` |
  | assignment / sources | softer magenta `#d985b6`, slow halo, `∅` / `↧` |
  | local-ci | blue `#4e8cff`, hatched at 4px, spinner arc, `▣` |
  | remote-ci | sky `#5cc8ff`, hatched at 7px, spinner arc, `☁` |
  | research-proto (a research or prototyping run; role-time group `research`) | the waiting blue, a wide 11px hatch, spinner arc, a dashed outline (the CI waits are solid), `⚗`; its job host reads "research run on" |
  | lease | periwinkle `#7c8cff`, dense hatch, `⧖` |
  | upstream | violet `#a58bff`, hatched, `↑` |
  | session-blocked (peer) | amber `#ffb547` dashed outline, `⊘`, a red dashed `blocked-on` edge to the blocker |
  | self | teal outline, no fill |
  | handover / refill / hold | lavender `⇆` / mint `↺` / grey `∥` |
  | any other wait event | grey dashed, `…`, raw event in the card |
  | idle | dim slate; excluded: hollow ring at 42 percent |
  | unknown or dead | red dotted outline, `✕` |

- **Edges:** curved beziers (a to b and b to a bow apart), each a wide low-alpha glow under a thin
  core, arrowheads at the target. Colors: msg steel cyan, ask magenta, offer mint, grant olive,
  health amber, handoff lavender, blocked-on red dashed, alias grey dotted. Opacity and width by
  recency (hot within the 5 min window, warm within the hour, cold). Cross-host traffic has a long
  subtle dash. Width and brightness also step with the rate (under 1/h, under 4/h, above). A role
  whose CI job runs on another host has a dotted tether to that host's band (per shoal) or a chip
  at the node (global), labeled `remote-ci -> host-b`.
- **Depth:** the lattice opens in depth, iso (an isometric turn), and `d` cycles the camera: iso, flat, tilt (52
  degrees), seen in perspective, so one press from the opening view is flat. In the
  global view each shoal is on its own plane, stacked in depth, the operator and outside seats on a plane above, so
  traffic between planes visibly crosses them. A floor grid lies below every globe,
  its lines converging; a dashed stem drops from each plane's middle and from each lifted node to the floor. The canvas
  is a lighter slate in depth, so the shadows read. One light, a point high over the scene (`depth.js` `lightPoint`, up,
  to the left and a little toward the far side, fixed in the world so it stays put as the camera orbits), shades each
  globe from the way to it from that globe and casts every shadow: each globe's, each lifted node's and each role's on
  a globe (the glass catches none, so a role's falls on the floor, inside its globe's), where the ray from the light
  through its middle meets the floor (`castShadow`), so the shadows fall down and to the right of their casters at the
  presets, further the higher the caster (yours furthest). A shadow is a stack of fine alpha layers cut into equal
  steps of darkness as drawn in 8 bits (about two color levels each, `sphere.js` `shadowLayers`), fewer for a small
  one, so it falls off smoothly from a dark core through a soft penumbra that widens as the caster rises, with no ring
  showing. Each stem casts a faint shadow from its foot to its caster's, and each edge a faint one too, cast as a thing
  in the world: every point of the curve its arrow is drawn along is lifted onto its line of sight at a depth between
  its ends' (`projectLayout`'s `shade`), and the shadow is drawn through where the light's ray through each point meets
  the floor (`scene.js` `castCurve`: cubics through cast points, within a pixel of the cast curve, so a short or straight
  one is a single cubic), arrowheads included, and clipped off the floor's grid. Every shadow paints before every edge
  and node, and a node a shadow reaches stands on an opaque disc of what is under it without the shadow (a box, no path
  commands), so no shadow shows through a faint body. Far things are smaller and fade, near ones larger and drawn last. In the
  global view each shoal is a
  glass sphere: 48 directional light contours, each adding a little alpha, so the shadow side is nearly clear (5
  percent) and the lit side a tinted veil (27 percent), the floor and the far side's grid showing through; with the
  pointer on it (anywhere on the globe, its name, a role, an item or an edge of it) it goes a little more opaque (16
  and 56 percent). Its name is 14 px, 12 px clear above the globe and any role on its top, the needs-you count
  between. A soft highlight and curved surface grid lines finish it. A label
  on a globe takes white or near-black ink, whichever reads better (WCAG contrast) against the glass under it as
  drawn; a needs-you label keeps its hue. In depth, every edge has a dark casing under its core, and its core a little
  more alpha and a lighter ink (its color toward white), so it reads over a pale surface and a dark one. Where a role sits on its globe says how much it matters now: the initial point facing you holds what needs you most
  (auth, then a question, a prompt, a design call, an empty queue, a stalled session), then the roles spiral back by what
  they are doing (a turn, their own job, a hand-over, waiting on others, idle with work queued, idle), then by how fresh
  their last write is. A role round the back is seen through the sphere, fainter. `,` and `.`
  turn the camera 15 degrees. The arrow keys orbit it 8 degrees a press: left and right turn it round as `,` and `.`
  do, up and down raise and lower it (its pitch held between 24 and 80 degrees, so the planes never go edge-on);
  shift with an arrow pans, as a plain arrow does in flat. The projection (`depth.js`) runs on the layout before the scene, only on a data tick or
  a key, never on a timer; at the low and off motion tiers on a CPU renderer the view stays flat.
- **Depth, per shoal: the cutout sphere.** `you` float 0.5 radii from the middle of a sphere whose near part is cut
  away, leaning from its axis toward the camera, out past the cut in front of the opening, and the camera looks into
  the bowl that is left from the side, in perspective: it hangs 2.4 radii from the middle, and the bowl opens turned
  far round, its axis off the line of sight -14 degrees vertically and -70 horizontally (tilt and iso alike), so its
  opening faces your left and its bottom lies to the right of you. The arrow keys orbit the camera round the bowl, 8
  degrees a press: up and down tip its axis further or less down the screen (-30 to 64 degrees), left and right turn it
  round the screen's vertical (-78 to 78 degrees), the two together held to at most 78 degrees off the line of sight
  (cos tilt cos turn above its floor; a press past a limit goes to it and the rest is dropped), so the bowl never goes
  edge-on or flips. The bowl has one light, a point 4 radii out fixed to the bowl (`sphere.js` `BOWL_LIGHT`), placed
  just above the camera at the old opening view (10 and -40), above and behind the opening now, so as the camera orbits
  the light and the shadows turn with the bowl; every shadow in the bowl is the ray from it through the caster, meeting
  the wall (`bowlCast`). You are nearer the camera than the middle (orbited far round the side, leaning further toward
  it), so from an oblique angle you stand visibly off the wall behind you: you are a small glass ball (about 90 path
  commands: a clear body graded to a deep limb, twelve faint layers rounding it toward the light, a highlight toward
  the light, a crescent of gathered light inside the far limb, a lit rim and a dark hairline; at `full` your halo
  breathes behind it), your soft shadow falls on the wall where the light's ray through you meets it (below you at the
  opening view, further off as the camera orbits round, and not drawn where it would land outside the opening), each
  edge's small shadow is cast as on the floor, onto the wall where the ray through each point of its lifted arrow
  leaves the sphere (a point held just in front of the wall, so nothing casts from beyond it; clipped where the wall is
  behind the near wall or outside the opening; an edge to a seat on the ring, outside the sphere, casts none), so an
  arrow from you runs from your shadow to its role's, and a dashed plumb line drops
  from you down the axis to a small ring at the bottom. An edge from you
  is a tapered ribbon (five commands), wide and bright at you and thinner and dimmer at the far wall by the two ends'
  perspective, and an arrowhead is sized with the node it is at, so the edge reads as running away, into the bowl. A
  role keeps its place on the surface as the camera orbits (where the preset puts it), moved along its meridian only to
  stay in view, clear of you, or clear of a role placed before it (at the preset's view, if no place on its meridian is
  clear, to the nearest clear place on its lune). The cut is a slanted
  plane: the far wall rises 29 degrees past a hemisphere's rim (so more of the back shows), the near wall is cut lower, but
  short of where the camera would see over it by up to 26 degrees seen from the side (17 from in front, held where the
  bottom would come within 0.84 of the rim: about 25 at the opening view), so the sphere's outer wall shows as a shell
  beside the opening, from its top round to its bottom. The wall has a thickness, so the
  opening has a lip: a lit band between the rim (a turned ellipse) and its outer edge. Everything inside the rim is
  the inner surface, opaque (it hides its own floor behind it) and lit as a hollow is: the light pools where it falls
  brightest (16 faint layers over a smooth linear sweep, so no band shows), the wall under the lip on the light's side
  is in its shade (the ray past each rim point meets the wall at the shade's edge), and 18 faint caps round the bottom
  (6 of 255 each) darken it smoothly toward the bottom, so no ring of shading shows. Four faint rings of latitude and
  eight faint meridians stay as guides (orbited far round the side, a ring the cut would break is left out). The bowl
  stands over its own floor, square to its axis, and casts its shadow there from the same light. The hosts' lunes,
  pole to rim, tile that whole surface: each over its sector of the flat view widened to the middle of the gaps either
  side, so together they make a whole turn, washed (17 percent) and outlined in the host color, the host colors clearly
  apart (azure, amber, green, rose, violet, cyan, olive: every pair at least 30 apart in Lab; the home host first, from
  the top), its name just outside the rim. A host's roles sit on its lune, spread across its sector in label order (two
  rows from four roles), each kept inside the rim and behind you; they shrink and fade with their
  depth, and you are drawn nearest and largest (1.25 times flat, times your perspective). Outside seats ring the rim, in the cut's plane,
  outside it; a CI tether ends in the middle of its host's lune. Edges and arrows are drawn over the
  bowl, with the depth casing. Labels in the bowl get the same readable ink against its inside. `,` and `.` turn the
  bowl about its axis. Flat per shoal is unchanged (the ring of host sectors).
- **Replay:** `r` plays the last 24 h back on the live picture, a day in 32 s by default (the bar's speeds: a day in
  96, 32, 11 or 3.2 s; `[` and `]` step them): each event a packet
  along its edge, a done a flash, at most a dozen a tick. A bar along the foot shows a quarter hour a column, its
  height that slot's events; a click plays from there, space pauses, `r` or Esc goes back to live. It asks the
  feed's `ui graph --replay` once, on the machine the feed reads; live packets wait while it runs. It is the one thing
  drawn whatever the motion tier, `off` included, because `r` asked for it; the legend says so.
- **Satellites:** each role's active item, next items and items done in 24 h as small dots on
  short spokes fanned outward: the starburst. A next item that waits on something is a ring, so what
  can start stands out; a stale blocker is red. Hovering a dot shows its item, title and blocker, and
  clicking it opens the issue (http and https links only). A ring is the blocker as last recorded: one that has
  cleared still reads as held until the role re-records it or it ages to stale.
- **Outside nodes:** a dashed circle is either a saved standalone session on the feed's host, shown
  whether or not it ever wrote to a ledger or was dismissed from the app's spaces, or a ledger writer with no session behind it. A saved one's
  card gives its harness, id and whether it runs now, and offers `Remove...` and hide. A writer with no
  session has nothing to remove: its card says so and offers to hide it. A long name wraps in the card.
- **A shoal's name:** in the global view, hovering it shows its board's numbers and bar, and clicking
  it opens the board (the rest of its disc drills in). In the per-shoal view the same link is in the
  header, and the card in the ring's hole carries the same numbers.
- **Rail** (272px, `#0b0e12`): the view name and feed time; NEEDS YOU rows (badge, role, shoal,
  ask text, age), loudest first; BOARDS (per shoal in view, each priority board as a link beside how
  old its read is, amber past 30 minutes, then done, open, in flight and assigned over a thin bar in
  those colors: `#6a8592`, `#d4d4d4`, `#2ee6c0`, `#a58bff`, then queued work not open on the board as
  "off the board"; a host that is not the shoal's home has the link and no counts); SHOALS cards
  (urgent-state dot, needs-you pill, done 24h/7d, msg/h, work share bar, sparkline, asks, grants,
  pages); HOSTS (roles, working, waiting, msg/h, CI jobs placed in).

## How it behaves

- Polls the graph doc every 4 s (`exec` of `shoal ui graph --json`, then `--since <cursor>`), or a
  file, or the bundled fixture. A failed read keeps the last good picture with a red note.
- Motion follows a tier (`motion` prop, `m` cycles, the legend and header show it): `full`,
  `balanced`, `low`, `off`, or `auto` (low on a CPU renderer, full on a GPU, balanced when
  unknown; one tier lower for the pulse drawer, a small pane or an unfocused window). In `full`,
  motion is native and runs only while drawn: halos and rings `loop` opacity; marching dots are
  `frames` glyph lists rotated per dot so the bright head moves source to target, faster with
  `rate_per_h`; a broadcast ripples out of its sender (`loop` on left, top, width, height,
  opacity); each new event (by `seq`) sends one packet along its edge (`transition` on left/top),
  a `done` flashes a mint ring.
- In `balanced` the pulses are a ring glyph stepping on one shared 220 ms interval and dots run
  on the six busiest edges; `low` and `off` draw nothing that moves (edge width and brightness
  carry the rate), and a data poll redraws only when what is shown changed. Every role that needs you
  or asks you pulses at `full` and `balanced` in flat and depth alike: only a hover's fading stops a
  pulse, a far node's depth fog makes it fainter (gated on both, almost none pulsed in depth).
- Hover a node: everything not connected fades, a card shows its state and age, ecosystem, model,
  host, CI job host, grants, done and messages, context size, asks, items with blockers (stale in
  red) and the `shoal view` command. Click pins the card; item lines become links; the command
  copies. Every card line is one line: a long item or blocker is cut with an ellipsis and its
  full text is on the line's tooltip. The card stays inside the canvas (right of the node, else
  left; flipped above it near the bottom; capped at the canvas height, dropping item lines with a
  `+N more` note). Drag pans, wheel up zooms in at the pointer and wheel down zooms out, click a
  cluster drills in, Escape backs out (to the home view), `g` toggles the global view, `l`
  toggles a legend of every encoding, `f` lays the view out afresh and fits it.
- Right-click a role node: the `shoal-actions` dialog opens on that role
  (`openOverlay({mod: "shoal-actions", kind: "dialog", props: {shoal, role, machine}})`, machine
  `source.on`, null here); on a shoal's writer node it opens on the shoal alone. A fixture copy
  passes `fixture: true`, so the dialog answers from its own fixtures. The view writes nothing.
- Layout stability: positions are kept by node id across polls. A poll with the same node set
  moves nothing and does not refit (metrics, states, ages and edges never shift a node); a changed
  node set refines only the new nodes and those whose target moved, the rest stay pinned. `f` is
  the only full relayout after the first.
- Fit (`f`, a drill, a resize, a node set change while fitted) measures the drawn scene, labels,
  chips, halos and cluster titles included, and solves for the zoom that keeps all of it 24 px
  inside the canvas and below the header, centered.
- **Home shoal** (`props.shoal`): the copy opens drilled into that shoal and treats it as home:
  Escape (after unpinning and closing the legend) returns there from any other view, the header
  gets an `all shoals ›` / `‹ <tag>` chip, and `g` toggles the global view. A shoal the feed does
  not have shows the global view with an amber `<tag> is not in the feed`.
- **Pulse mode** (`props.mode: "pulse"`, a floating `peek` drawer): Geist Mono 10.5 px in the
  theme's colors. One row per shoal: the loudest state's dot (9 px, its state color), the
  needs-you count in magenta (`·` when none), the tag in its pastel, msg/h. A rule, then up to five
  needs-you roles, loudest first: a 14 px badge disc in the state color with its glyph (`⚷`, `?`,
  `⏎`, `✎`, ...), the role, the wait event in the state color, the host; `+N more` below. Dots,
  counts and badges sit on the edge side (rows mirror on the right edge; a top or bottom edge lays
  the rows side by side). Still unless the tier is `full` (an explicit `motion: "full"`; `auto`
  drops a tier for the drawer), where a needs-you shoal dot breathes. A poll redraws the drawer
  only when its rows changed; it builds no layout or scene. With `props.shoal` it lists that
  shoal only.
- **Focus from the drawer:** a role row sets shared `lattice.focus` to `{node, shoal, t}`, a shoal
  row to `{shoal, node: its loudest needs-you role or null, t}`. Every full copy honors a new
  value (a plain node id string works too) by drilling into the role's shoal and pinning its card;
  a value older than two minutes, or the one already there when a copy starts, is ignored.

## Built from

- The mod runs whatever argv its `source` props name, on any herdr-core machine that `on`
  names. Keep it a read-only shoal command (`shoal ui graph --json`, or an ssh wrapper around
  it); never point it at anything that writes, wakes a role or sends a message.

- A pane mod, `mod.json` `{"place": "none", "focusable": true}`; no layout of its own. Pulse is the
  same mod as a floating `peek` block with `props.mode: "pulse"`.
- `main.js` (View): `cx.timer.every` for data only, `cx.spawn` + `exec` / `readTextFile`,
  `nextCommand`, `shared` / `setShared`, `placement`, `theme` (rail border), `appHome`,
  `openOverlay` (the actions dialog on a right-click, `model.js` `actionsOverlay`).
- Pure modules (no Herdr imports, tested under Node): `model.js` (doc to view model, encodings,
  needs-you order, edge buckets, host palette, event diff, the visual signature), `motion.js`
  (tiers, budgets, auto resolution, the renderer probe), `layout.js` (targets plus a compact
  Fruchterman-Reingold refinement, seeded by a hash of each node id, positions kept by node id),
  `geom.js` (curves, arrowheads, arc-length samples, hit tests, pan/zoom, the wheel factor, card
  placement), `scene.js` (the display list, its screen extent, and the fit).
- Drawing: one `relative()` canvas; every path layer is `window.paint_path(path, Background)`
  absolutely positioned over the whole canvas; edges batched one glow and one core path per
  color x tier bucket; hatch with `Background.pattern_slash`; dashes with `dash_array`. Nodes,
  halos, badges and labels are absolute divs. `arc.svg` is the spinner.
- Runs whatever `source.argv` says, on `source.on`; reads `source.path`; nothing else.

## Watch out

- `PathBuilder.cubic_bezier_to` takes the end point first, then both controls.
- One Background per path: batch by color, or the element count explodes. Paths are drawn in
  screen space, so pan and zoom re-render (cheap at this size).
- Globe lighting uses at most six cubic arcs per contour. Expanding each
  contour into 80 line calls exceeds the native script render deadline. A native check on the
  async loading screen misses this: accept the fixture synchronously when checking depth renders.
  `node plugins/shoal-herdr-app/preview/check_depth.mjs --runs 12` checks the loaded scene;
  `--depth tilt`, `--yaw 60` and `--doc graph.json` cover other cameras and read-only snapshots,
  `--shoal <id>` the per-shoal cutout sphere and `--hover globe:<id>` a globe in focus. It fails
  if the copy drew another view or fell back to flat (the mod's `init` reads `props()`, so a
  wrapper sets the home shoal itself).
- Transparency on nested fills stacks: every contour covers the ones inside it, so a constant
  alpha on all of them turns the lit cap opaque. Each glass contour's alpha is solved from the
  total opacity wanted at its level, and alpha is 8-bit, so fewer, slightly stronger layers (48,
  1 to 6 of 255 each) beat 128 layers rounded to 0 or 1. Transparency comes only from alpha on
  these fills, never from more layers. The fixture's two globes now take about 1,940 path
  commands, down from about 3,030; their shadows about 260; the bowl about 490 (its glow 264 of them). The global depth
  scene was about 2,790 in all; a trial with 24-layer shadows (about 2,980) failed about one native
  check in 50, so keep a globe's shadow at 16 layers. A globe's grid was six 96-point polylines, about 600
  commands a globe; each circle's image is an ellipse, so a cubic per quarter turn draws it (about 40), and the global
  depth scene is about 1,620 in the fixture (2,750 before) and 2,000 on a two-shoal live feed (3,130 before). The
  per-shoal scene stays at or under its count before the glass you and the tapered edges (the fixture's first shoal
  1,085 at the opening view, now about 1,040): the glow gave 168 commands for them. With one light, the fine depth caps
  and the edges' shadows (2026-11-13) the fixture's bowl is at most about 1,160 and its global scene about 1,720; a
  live two-host bowl about 1,190 and its global scene about 1,910. With smooth shadows, one for every role on a globe
  (2026-11-13), the fixture's bowls are at most 1,358 and 967 and its global scene 2,054; with physical edge shadows and
  the taller near wall, the worst at the canvas fit and 1.6x zoom is 1,428 and 1,005 for the bowls (1,365 and 969
  before) and 2,284 for the global scene (2,278). The tests hold a bowl to 1,450,
  the global scene to 2,300 and every scene, at the fit and zoomed in, to 2,800: about 2,800 never failed a native
  render and 3,500 sometimes did.
- A shadow on the dark floor cannot show more than about 30 to 50 distinct 8-bit levels, so layers finer than one or two
  levels apart buy nothing, and finer than half a level they stall: GPUI rounds every channel per layer, so a 1 percent
  layer over a dark floor changes nothing. Each layer's alpha is solved by blending the rounded layers outside it over
  the color under the shadow (`blend8`), and one that would change nothing is dropped.
- The bowl's circles are circles on a sphere seen in perspective, so each projects to an ellipse,
  but not at an even pace: eighth-turn cubics fitted to the projection stay within a pixel. The
  rim's ellipse is exact (`bowlFrame` `rim`, the conic image of the cut circle, turned), the
  visible inside is exactly its inside, and a screen point maps back to the inner surface along
  the line of sight (`bowlSurfaceAt`). The cut is set from the camera: a point of the unit sphere
  faces a camera d radii away when u.z > 1 / d, so a cut whose normal is b off the line of sight
  keeps none when acos(h) >= b + acos(1 / d).
- The bowl's glow was 20 layers at 5 of 255 alpha: each edge a step of three or so color
  levels, which showed as rings. 44 layers at 2 of 255 give a little less light in steps under
  one and a half levels; 1 of 255 would need about 90 layers for the same light. A linear gradient has
  no steps at all, so a sweep over the rim toward the lit spot now gives half that light and 16
  layers at 2 of 255 the pooling (six commands a layer, the cheapest smooth light there is).
- You in the bowl read as drawn on its wall whatever the depth math said: a flat ring with a
  two-step halo showed bands and no depth. What separates a thing from a wall behind it is a
  shadow attached to it, offset by how far in front it is: lit from the opening the shadow fell
  near the bottom, two or three of your widths away, and read as the bowl's, not yours. A key light by
  the camera for your shadow alone put it just down and right of you, but no other shadow agreed with it
  (2026-11-13): now one light, a little off the line of sight at the opening view, casts yours and the
  edges', so the edges from you start at your shadow and say whose it is. A filled ribbon tapers smoothly for five
  commands, where a stroke can only step its width per segment. Four depth caps of 14 of 255 under the
  rings still read as topographic rings: 18 caps of 6 of 255 step under three color levels, which no eye
  sees, and leave the rings as faint guides.
- A bowl with one bright outline reads as a ball seen from outside. What says opening is a lip
  with two edges, the near wall's outside under it, and shade under the lip on the light's side;
  what says depth is the inside darkening toward the bottom and shadows that fall on it. Meridians whose planes pass near the camera
  project nearly straight, so they show little curvature on their own.
- Any `loop` makes the window redraw at about 30 fps; `frames` and `spin` redraw at their own
  interval. The cost is per window frame, not per element: on lavapipe (two threads) a frame of
  this window cost about a core-second whatever the mod drew, so one 700 ms blink cost over a
  core. Hence tiers by frame rate, and nothing animated in `low` and `off`.
- Skip `cx.notify` when a poll changed nothing shown (`visualSignature`); on a CPU renderer an
  unneeded redraw is the expensive part.
- No renderer API exists in Herdr app 0.1.12; `readTextFile("/proc/self/environ")` from the mod
  reads the app process's own environment (the Vulkan ICD), which is the cheap probe. Then
  `/proc/self/maps`, but read it hardware first: the Vulkan loader maps every installed ICD while
  it enumerates (lavapipe, intel, radeon, nouveau, asahi, gfxstream, virtio, and NVIDIA's
  `libGLX_nvidia`), so `libvulkan_lvp` is mapped on a GPU laptop too. An NVIDIA library or a mapped
  `/dev/nvidia*` or `/dev/dri/renderD*` means GPU; only a software driver with no hardware driver
  library means CPU. An environment that forces or selects a driver always wins.
- Wheel direction: GPUI reports a wheel turned up (X button 4) as a positive `delta.y`, and
  trackpad pixel deltas use the same sign. Zoom by `exp(+dy * 0.004)` so up zooms in; one event
  is capped at 2x so a burst of line deltas cannot jump.
- Labels, halos and cluster titles keep their screen size at any zoom, so a fit to world bounds
  clips them at the edges; fit to the scene's measured extent instead (`fitScene`).
- Refitting on every poll shifts the whole picture when satellites or label placement change;
  refit only when the layout moved, the view or size changed, or on `f`.
- A pinned `Link` wraps a long label and grows the card past its estimate: cut the text to one
  line first. `tooltip` is wired on `div`, not on `Link`, so wrap the link in a div with an id.
- `transition` animates a change between two renders, so a packet needs two: drawn at the source,
  then (a 60 ms `timer.after`) given the target. A pure timer animation would redraw everything.
- `on_size_change` arrives after the first layout: make the canvas `flex_1`, not a fixed width.
- The first doc read is a baseline; only later events animate.
- Glyph badges (`⚷ ☁ ⧖ ✎ ⏎ ⊘`) rely on font fallback beyond Geist Mono.
- Unknown wait events render as "other", unknown classes as "unknown"; never throw on new values.
- Feed details to honor: `blocked-on` and `alias` edges have null `last_ts`/`last_age_s`; a
  `stale` blocker draws fainter; the feed's `cross_host` wins over comparing hosts; `writer`
  nodes (unknown or removed writers) draw as small dashed nodes; a `state.source` of `status`
  gets a `~` in the card; `state.job.host` counts as a job host; `hosts[]` counts win in the
  rail; a `cursor_reset` doc is a new event baseline.
- In Herdr app 0.1.12 an edit to a mod folder that is a symlink did not always refresh copies
  already open; reopening the tab picks up the new code. `herdr-app check` always reads it fresh.

## Done when

- `herdr-app check` passes and `herdr-app status` shows no errors.
- `f` shows every node, label and ring inside the canvas; two polls with the same nodes draw them
  at the same pixels; a pinned card near the bottom or right edge stays inside the canvas; wheel up
  zooms in.
- With the fixture: two clusters around `you`, `integration` pulsing orange-red with `⚷` and a
  `⚑`, `tune` magenta with `⏎`, `dist-ci` and `bench` hatched blue with different badges, a dotted
  `remote-ci -> host-b` chip at `bench`, three rows under NEEDS YOU (integration, tune,
  registry), two host rows.
- In `full`, every 4 s a packet crosses one edge, dots march along hot edges and `core` ripples;
  in `low` and `off` nothing moves and the header says so.
- Clicking the alpha disc drills in to host bands; Escape returns.
- `node tests/herdr_app/test_lattice_view.mjs` passes.
