// layout.js: where the logo, the five creatures and the host cards go for a window size.
// Pure: no Herdr or GPUI imports, so it runs under Node.
//
// The composition follows the terminal splash (shoal/ui/ui_launch.py `image_rectangles`): the
// coral fish centered above the SHOAL logo, the seahorse and the blowfish either side above it,
// the teal fish left of the logo and the violet one right of it; the tagline, the question and the
// host cards below. Everything stacks top to bottom, so no card can sit under a creature. The
// largest scale whose whole composition fits is used; below the art floor, or when the images are
// missing, the same screen is drawn as text (logo and host list, no creatures).

export const PAD = 24;
export const GAP = 16;
/** Below this window size the home screen is text only. */
export const ART_FLOOR = { width: 640, height: 460 };
/** Composition scales, largest first. */
export const SCALES = [1.5, 1.3, 1.15, 1, 0.85, 0.72];

/** Card geometry: every line below the header is exactly `line` px, so a card's height is known. */
export const CARD = { pad: 12, head: 24, line: 18, minW: 248, maxW: 340, maxCols: 4 };
export const cardHeight = (nLines) => CARD.pad * 2 + CARD.head + 4 + Math.max(0, nLines) * CARD.line;

/**
 * The creatures, in the terminal splash's asset order (coral, teal, violet, seahorse, blowfish
 * there; listed here by slot). `k`: size in units of the creature base B; `dx`, `dy`: offset of a
 * top-band creature's center from the window center and of its top from the band, in B.
 * `period`: the slow drift's period at the full motion tier (each its own, so they never sync).
 */
export const CREATURES = [
  { name: "coral", file: "fish-coral.png", slot: "top", k: 1.1, dx: 0, dy: 0, period: 5200 },
  { name: "seahorse", file: "fish-seahorse.png", slot: "top", k: 0.85, dx: -2.15, dy: 0.2, period: 6100 },
  { name: "blowfish", file: "fish-blowfish.png", slot: "top", k: 0.95, dx: 2.0, dy: 0.1, period: 4700 },
  { name: "teal", file: "fish-teal.png", slot: "left", k: 1.35, period: 5600 },
  { name: "violet", file: "fish-violet.png", slot: "right", k: 1.35, period: 6400 },
];

/** The refresh control, top right. */
export const REFRESH = { y: 14, w: 96, h: 24 };
const refreshBox = (W) => ({ x: W - PAD - REFRESH.w, y: REFRESH.y, w: REFRESH.w, h: REFRESH.h });

/** Logo text size estimate: SHOAL in a semibold sans is about 3.5 em wide, 1.2 em tall. */
// wide enough for the wavy letters (ocean.js logoLetters: 3.8 em of glyph boxes)
const logoBox = (font) => ({ w: font * 3.9, h: font * 1.25 });

/**
 * Card grid for `heights` (one per host, in order) in `avail` px of width: rows as even as the
 * width allows, each row centered, each row as tall as its tallest card. Positions are relative
 * to the grid's top left at x = 0 of the window.
 */
export function cardGrid(width, heights) {
  const n = heights.length;
  if (!n) return { cols: 0, cardW: 0, h: 0, cards: [] };
  const avail = Math.max(120, width - 2 * PAD);
  const fit = Math.max(1, Math.floor((avail + GAP) / (CARD.minW + GAP)));
  let cols = Math.max(1, Math.min(n, fit, CARD.maxCols));
  const rows = Math.ceil(n / cols);
  cols = Math.ceil(n / rows);
  const cardW = Math.floor(Math.min(CARD.maxW, (avail - GAP * (cols - 1)) / cols));
  const cards = [];
  let y = 0;
  for (let r = 0; r < rows; r++) {
    const row = heights.slice(r * cols, r * cols + cols);
    const rowW = row.length * cardW + (row.length - 1) * GAP;
    const x0 = Math.round((width - rowW) / 2);
    const rowH = Math.max(...row);
    row.forEach((_h, i) => cards.push({ x: x0 + i * (cardW + GAP), y, w: cardW, h: rowH }));
    y += rowH + GAP;
  }
  return { cols, cardW, h: y - GAP, cards };
}

function artAt(W, H, heights, u) {
  const B = 92 * u;
  const font = Math.round(84 * u);
  const logo = logoBox(font);
  const drift = Math.round(10 * u);
  const cx = W / 2;
  const sideGap = 0.3 * B;
  const sideSize = 1.35 * B;
  // width: the logo row (teal, logo, violet) and the top band must clear the padding
  const rowHalf = logo.w / 2 + sideGap + sideSize;
  const bandHalf = Math.max(...CREATURES.filter((c) => c.slot === "top").map((c) => Math.abs(c.dx) * B + (c.k * B) / 2));
  if (Math.max(rowHalf, bandHalf) > cx - PAD) return null;
  const grid = cardGrid(W, heights);
  const bandH = Math.max(...CREATURES.filter((c) => c.slot === "top").map((c) => c.dy * B + c.k * B)) + drift;
  const rowH = Math.max(logo.h, sideSize + drift);
  const tagSize = Math.round(15.5 * Math.min(1.25, Math.max(0.9, u)));
  // offsets from the composition top
  const rowY = bandH + 0.1 * B;
  const tagY = rowY + rowH + 6;
  const askY = tagY + tagSize + 12;
  const gridY = askY + 22 + 18;
  const footY = gridY + grid.h + 14;
  const total = footY + 16;
  if (total + 2 * PAD > H) return null;
  // the refresh control sits in the top right corner, above the composition
  const top = Math.round(Math.max(REFRESH.y + REFRESH.h + 8, (H - total) / 2));
  if (top + total + PAD > H) return null;
  const creatures = CREATURES.map((c) => {
    const size = Math.round(c.k * B);
    let x, y;
    if (c.slot === "top") {
      x = cx + c.dx * B - size / 2;
      y = top + c.dy * B;
    } else {
      y = top + rowY + (rowH - drift - size) / 2;
      x = c.slot === "left" ? cx - logo.w / 2 - sideGap - size : cx + logo.w / 2 + sideGap;
    }
    return { name: c.name, file: c.file, x: Math.round(x), y: Math.round(y), size, drift, period: c.period };
  });
  return {
    refresh: refreshBox(W),
    mode: "art",
    scale: u,
    width: W,
    height: H,
    contentH: top + total + PAD,
    logo: { x: Math.round(cx - logo.w / 2), y: Math.round(top + rowY + (rowH - logo.h) / 2), w: Math.round(logo.w), h: Math.round(logo.h), font },
    tagline: { y: Math.round(top + tagY), h: tagSize + 4, size: tagSize },
    ask: { y: Math.round(top + askY), h: 22, size: Math.round(14 * Math.min(1.2, Math.max(0.9, u))) },
    creatures,
    cards: grid.cards.map((c) => ({ ...c, y: Math.round(top + gridY + c.y) })),
    cols: grid.cols,
    foot: { y: Math.round(top + footY), h: 16 },
  };
}

function textAt(W, H, heights) {
  const font = W < 420 ? 34 : 48;
  const logo = logoBox(font);
  const grid = cardGrid(W, heights);
  const tag = W >= 520;
  // a narrow window keeps the refresh control above the logo, not beside it
  const minTop = W < 560 ? REFRESH.y + REFRESH.h + 8 : PAD;
  const body = logo.h + 4 + (tag ? 25 : 0) + 6 + 22 + 14 + grid.h + 12 + 16;
  const logoY = Math.round(Math.max(minTop, (H - body) / 2));
  const tagY = logoY + logo.h + 4;
  const askY = tagY + (tag ? 25 : 0) + 6;
  const gridY = askY + 22 + 14;
  const footY = gridY + grid.h + 12;
  const total = footY + 16 + PAD;
  return {
    refresh: refreshBox(W),
    mode: "text",
    scale: 0,
    width: W,
    height: H,
    contentH: total,
    logo: { x: Math.round(W / 2 - logo.w / 2), y: logoY, w: Math.round(logo.w), h: Math.round(logo.h), font },
    tagline: tag ? { y: Math.round(tagY), h: 19, size: 14 } : null,
    ask: { y: Math.round(askY), h: 22, size: 13 },
    creatures: [],
    cards: grid.cards.map((c) => ({ ...c, y: Math.round(gridY + c.y) })),
    cols: grid.cols,
    foot: { y: Math.round(footY), h: 16 },
  };
}

/**
 * The home screen for a window `size` ({width, height}, logical px) and the card `heights` (one
 * per host, from cardHeight). `opts.art: false` (images missing, or turned off) forces text.
 * `contentH` above the window height means the text screen scrolls.
 */
export function homeLayout(size, heights, opts = {}) {
  const W = Math.max(200, Math.floor(size?.width ?? 1000));
  const H = Math.max(160, Math.floor(size?.height ?? 700));
  const hs = Array.isArray(heights) ? heights : [];
  if (opts.art !== false && W >= ART_FLOOR.width && H >= ART_FLOOR.height) {
    for (const u of SCALES) {
      const l = artAt(W, H, hs, u);
      if (l) return l;
    }
  }
  return textAt(W, H, hs);
}

/** Rectangles that must not overlap one another: cards, creatures (with their drift) and the logo. */
export function occupied(l) {
  const out = [];
  out.push({ kind: "refresh", ...l.refresh });
  out.push({ kind: "logo", x: l.logo.x, y: l.logo.y, w: l.logo.w, h: l.logo.h });
  for (const c of l.creatures) out.push({ kind: `creature:${c.name}`, x: c.x, y: c.y, w: c.size, h: c.size + c.drift });
  l.cards.forEach((c, i) => out.push({ kind: `card:${i}`, ...c }));
  return out;
}

export const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
