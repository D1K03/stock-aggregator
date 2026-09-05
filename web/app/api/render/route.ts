import { existsSync } from "node:fs";
import path from "node:path";
import { NextResponse } from "next/server";
import { CARD_W, cardHeight, chartSvg } from "@/lib/chart-svg";
import type { ChartSpec } from "@/lib/threads";

/* Rasterises a chart to PNG, for Discord.
 *
 * Steven can attach an image there but not render one, and the Python side has
 * no drawing library — adding one would mean a second implementation of the
 * chart, which is exactly the thing that ends up subtly different from the one
 * on screen. Instead the bot posts the chart spec here and gets back a PNG of
 * the same SVG string the browser displays, in the same typeface.
 *
 * Unreachable from outside, by construction rather than by a check: Caddy
 * routes every /api/* path to the status service, so nothing on the internet
 * resolves to this handler. The bot reaches it at http://web:3000 inside the
 * compose network.
 *
 * resvg rather than a headless browser. Chrome would be exact too, and would
 * also be three hundred megabytes and a second per render on a VPS chosen for
 * being cheap. resvg is a rendering library that draws the SVG we already have.
 */

export const runtime = "nodejs";

// Twice the card's own size. Discord displays attachments at up to about 400px
// wide and on a retina screen renders them at two, so this is sharp there
// without paying for a size nobody sees.
const SCALE = 2;

// A spec is around 1.5KB. Anything an order of magnitude past that is not one.
const MAX_BODY = 32_000;

/* The same family the dashboard renders in, so the PNG is not merely a similar
   chart in a different typeface. Loaded from the package rather than a copy in
   this repository, and kept in the standalone build by
   `outputFileTracingIncludes` — a file only read at runtime is otherwise not
   traced and the deployed container would fall back to a default face. */
const FONT_DIR = "node_modules/geist/dist/fonts/geist-sans";
const FACES = ["Geist-Regular.ttf", "Geist-Medium.ttf", "Geist-SemiBold.ttf", "Geist-Bold.ttf"];

let fonts: string[] | null = null;
function faces(): string[] {
  // Resolved once. Every reply with a chart would otherwise stat four files.
  if (!fonts) {
    // A missing weight is survivable — resvg picks the nearest it has — so
    // absent files are dropped rather than allowed to fail the render.
    fonts = FACES.map((face) => path.join(process.cwd(), FONT_DIR, face)).filter(existsSync);
    if (fonts.length === 0) {
      console.warn("no Geist faces found; charts will rasterise in a fallback font");
    }
  }
  return fonts;
}

export async function POST(request: Request) {
  const raw = await request.text();
  if (raw.length > MAX_BODY) {
    return NextResponse.json({ error: "spec too large" }, { status: 413 });
  }

  let spec: ChartSpec;
  try {
    spec = JSON.parse(raw);
    if (!Array.isArray(spec?.series) || spec.series.length < 2) throw new Error("no series");
  } catch {
    return NextResponse.json({ error: "not a chart spec" }, { status: 400 });
  }

  const { Resvg } = await import("@resvg/resvg-js");
  const png = new Resvg(chartSvg(spec), {
    font: {
      fontFiles: faces(),
      defaultFontFamily: "Geist",
      // Nothing but Geist is wanted, and a slim container has no system fonts
      // to find anyway.
      loadSystemFonts: false,
    },
    fitTo: { mode: "width", value: CARD_W * SCALE },
    background: "transparent",
  })
    .render()
    .asPng();

  return new NextResponse(new Uint8Array(png), {
    headers: {
      "content-type": "image/png",
      "content-length": String(png.length),
      "x-chart-height": String(cardHeight(spec) * SCALE),
    },
  });
}
