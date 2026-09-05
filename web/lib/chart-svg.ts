import type { ChartSpec, Mark } from "@/lib/threads";

/* The chart, as one SVG string. The single source of truth for how it looks.
 *
 * It exists because Steven draws charts in two places — the dashboard and
 * Discord — and "close enough" between them is not good enough: a chart that
 * differs between surfaces makes you doubt which one is right. The browser
 * renders this string; `/api/render` rasterises the same string to PNG with
 * resvg and the same Geist typeface. There is no second implementation to
 * drift, because there is no second implementation.
 *
 * Two consequences worth knowing:
 *
 * Colours are literal hex, not `var(--copper)`. A CSS custom property has no
 * value outside a document, so the rasteriser would draw the chart in black.
 * They are the same values `globals.css` defines and are checked against it by
 * a test.
 *
 * Nothing here animates. Motion is added by CSS in the browser, where an
 * external stylesheet can reach in; keeping it out of the string means the
 * rasteriser gets the finished state rather than the first frame — an
 * undrawn line. */

export const INK = "#1a1817";
export const INK_MUTED = "#77716c";
export const PAPER = "#f0efeb";
export const LINE = "#e5e7eb";
export const CARD = "#ffffff";
export const COPPER = "#e75532";
export const BLUE = "#3080ff";
export const AMBER_4 = "#f99c00";
export const AMBER_5 = "#dd7400";

const TONE: Record<Mark["tone"], string> = { copper: COPPER, blue: BLUE, amber: AMBER_5 };

/* Card and plot geometry, in one place so both renderers cannot disagree about
   where anything is. The plot keeps the coordinates it always had and is
   translated into the card, rather than every number being rewritten. */
export const CARD_W = 360;
const PAD = 10;
const TITLE_BASELINE = 21;
const PLOT_X = PAD;
const PLOT_Y = 28;
export const W = 340, H = 168;
const m = { t: 22, r: 38, b: 30, l: 26 };

const TITLE_SIZE = 12.5;
const FOOT_SIZE = 10;
const FOOT_LEAD = 13;
const FOOT_GAP = 8;

const FONT = "Geist, ui-sans-serif, system-ui, sans-serif";

/** XML-safe. Ticker names are ours, but a string built into markup gets escaped. */
function esc(text: string): string {
  return text.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" }[c]!)
  );
}

export function shortDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  // Three letters, because en-GB renders September as "Sept" while the reply
  // beside the chart says "Sep", and one date spelled two ways reads as two.
  const month = d.toLocaleDateString("en-GB", { month: "short" }).slice(0, 3);
  return `${d.getDate()} ${month}`;
}

/* Wrapped on an estimate rather than measured, because measurement needs a
   document and this has to produce the same bytes on a server with no DOM.
   Both renderers read the same estimate, so a slightly early break is
   identical in the browser and in the PNG rather than a difference. */
function wrap(text: string, width: number, size: number): string[] {
  const per = size * 0.505;
  const max = Math.max(8, Math.floor(width / per));
  const lines: string[] = [];
  let line = "";
  for (const word of text.split(" ")) {
    const next = line ? `${line} ${word}` : word;
    if (next.length > max && line) {
      lines.push(line);
      line = word;
    } else {
      line = next;
    }
  }
  if (line) lines.push(line);
  return lines;
}

export function footLines(spec: ChartSpec): string[] {
  return wrap(spec.subtitle, W, FOOT_SIZE);
}

/** Total card height, which depends on how many lines the footer took. */
export function cardHeight(spec: ChartSpec): number {
  return PLOT_Y + H + 6 + footLines(spec).length * FOOT_LEAD + FOOT_GAP;
}

export type Scales = {
  x: (i: number) => number;
  y: (v: number) => number;
  plotX: number;
  plotY: number;
};

/** Plot-local coordinates. Shared with the hover overlay so it lands on the line. */
export function scales(spec: ChartSpec): Scales {
  const s = spec.series;
  const lo = Math.min(20, ...s) - 4;
  const hi = Math.max(85, ...s) + 4;
  return {
    x: (i) => m.l + (i / (s.length - 1)) * (W - m.l - m.r),
    y: (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b),
    plotX: PLOT_X,
    plotY: PLOT_Y,
  };
}

function annotation(mark: Mark, spec: ChartSpec, sc: Scales): string {
  const colour = TONE[mark.tone];
  const isSpan = mark.kind === "span" && mark.end !== null;
  const from = mark.index;
  const to = isSpan ? mark.end! : mark.index;

  /* The label goes in the top margin rather than beside the point it marks.
     Beside the point it lands on the line itself wherever the series is busy,
     which is exactly where anything worth marking tends to be. */
  const anchor = isSpan ? (sc.x(from) + sc.x(to)) / 2 : sc.x(to);
  const width = mark.label.length * 4.9 + 11;
  const cx = Math.min(W - width / 2 - 2, Math.max(width / 2 + 2, anchor));
  const when = isSpan
    ? `${shortDate(spec.dates[from])} – ${shortDate(spec.dates[to])}`
    : shortDate(spec.dates[to]);

  // A point gets a rule through it; a span already has its band.
  const rule = isSpan
    ? ""
    : `<line x1="${sc.x(to)}" x2="${sc.x(to)}" y1="${m.t}" y2="${H - m.b + 4}" stroke="${colour}" stroke-width="1" stroke-dasharray="2 3" opacity="0.55"/>`;
  const start = isSpan
    ? `<circle cx="${sc.x(from)}" cy="${sc.y(spec.series[from])}" r="2.6" fill="${colour}" opacity="0.7"/>`
    : "";

  return `<g class="cc-mark">${rule}${start}` +
    `<circle cx="${sc.x(to)}" cy="${sc.y(spec.series[to])}" r="4" fill="${colour}" stroke="${CARD}" stroke-width="2"/>` +
    `<rect x="${cx - width / 2}" y="2" width="${width}" height="13" rx="6.5" fill="${colour}"/>` +
    `<text x="${cx}" y="11.5" text-anchor="middle" font-size="8.5" font-weight="600" fill="#fff">${esc(mark.label)}</text>` +
    `<text x="${cx}" y="${H - m.b + 14}" text-anchor="middle" font-size="8" font-weight="600" fill="${colour}">${esc(when)}</text>` +
    `</g>`;
}

export function chartSvg(spec: ChartSpec): string {
  const s = spec.series;
  const sc = scales(spec);
  const height = cardHeight(spec);
  const path = s
    .map((v, i) => `${i ? "L" : "M"}${sc.x(i).toFixed(1)},${sc.y(v).toFixed(1)}`)
    .join("");
  const last = s[s.length - 1];

  const grid = [25, 50, 75]
    .map(
      (v) =>
        `<line x1="${m.l}" x2="${W - m.r}" y1="${sc.y(v)}" y2="${sc.y(v)}" stroke="${PAPER}" stroke-width="1"/>` +
        `<text x="${m.l - 5}" y="${sc.y(v) + 3}" text-anchor="end" font-size="8" fill="${INK_MUTED}">${v}</text>`
    )
    .join("");

  // Bands sit under the line so the shading never dims the data.
  const bands = spec.marks
    .filter((k) => k.kind === "span" && k.end !== null)
    .map(
      (k) =>
        `<rect x="${sc.x(k.index)}" width="${Math.max(2, sc.x(k.end!) - sc.x(k.index))}" y="${m.t}" height="${H - m.t - m.b}" fill="${TONE[k.tone]}" opacity="0.1"/>`
    )
    .join("");

  const foot = footLines(spec)
    .map(
      (text, i) =>
        `<text x="${PAD}" y="${PLOT_Y + H + 6 + FOOT_LEAD * (i + 1) - 3}" font-size="${FOOT_SIZE}" fill="${INK_MUTED}">${esc(text)}</text>`
    )
    .join("");

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${CARD_W} ${height}" width="${CARD_W}" height="${height}" font-family="${FONT}">` +
    `<rect x="0.5" y="0.5" width="${CARD_W - 1}" height="${height - 1}" rx="13" fill="${CARD}" stroke="${LINE}"/>` +
    `<text x="${PAD}" y="${TITLE_BASELINE}" font-size="${TITLE_SIZE}" font-weight="600" fill="${INK}">${esc(spec.title)}</text>` +
    `<g transform="translate(${PLOT_X} ${PLOT_Y})">` +
    grid +
    `<line x1="${m.l}" x2="${W - m.r}" y1="${sc.y(spec.threshold)}" y2="${sc.y(spec.threshold)}" stroke="${AMBER_4}" stroke-width="1.2"/>` +
    `<line x1="${m.l}" x2="${W - m.r}" y1="${sc.y(spec.median)}" y2="${sc.y(spec.median)}" stroke="${BLUE}" stroke-width="1.6" stroke-linecap="round" opacity="0.8"/>` +
    bands +
    // pathLength normalises the dash animation the stylesheet applies, so it
    // does not have to know how long the line happens to be.
    `<path class="cc-line" d="${path}" pathLength="1" fill="none" stroke="${COPPER}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>` +
    `<text x="${W - m.r + 5}" y="${sc.y(last) + 3}" font-size="9" font-weight="600" fill="${COPPER}">${last.toFixed(0)}</text>` +
    `<text x="${W - m.r + 5}" y="${sc.y(spec.median) + 3}" font-size="9" fill="${BLUE}">${spec.median.toFixed(0)}</text>` +
    spec.marks.map((k) => annotation(k, spec, sc)).join("") +
    `<text x="${m.l}" y="${H - 6}" font-size="8" fill="${INK_MUTED}">${esc(shortDate(spec.dates[0]))}</text>` +
    `<text x="${W - m.r}" y="${H - 6}" text-anchor="end" font-size="8" fill="${INK_MUTED}">${esc(shortDate(spec.dates[spec.dates.length - 1]))}</text>` +
    `</g>` +
    `<line x1="${PAD}" x2="${CARD_W - PAD}" y1="${PLOT_Y + H + 6}" y2="${PLOT_Y + H + 6}" stroke="${PAPER}"/>` +
    foot +
    `</svg>`
  );
}
