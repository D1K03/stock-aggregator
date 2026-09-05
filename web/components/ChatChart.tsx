"use client";

import { useMemo, useRef, useState } from "react";
import {
  CARD, CARD_W, INK, INK_MUTED, cardHeight, chartSvg, footLines, scales, shortDate,
} from "@/lib/chart-svg";
import type { ChartSpec } from "@/lib/threads";

/* A chart Steven drew, in a chat message.
 *
 * The drawing itself is `chartSvg`, injected as markup rather than rebuilt as
 * React elements. That is the point: Discord gets a PNG rasterised from the
 * very same string, so the two surfaces cannot look different — there is one
 * renderer, not two that agree for now.
 *
 * Injecting markup is safe here in a way it would not be for model output: the
 * string is built by our own code from numbers a tool computed, and every text
 * node in it is escaped on the way in. Nothing the model wrote reaches it.
 *
 * What React still owns is everything static SVG cannot do — the hover
 * readout, and the entrance animation, which lives in `globals.css` so the
 * rasteriser is handed a finished line rather than the first frame of one.
 *
 * Marks arrive as indices into the series, computed from the data. The model
 * chose which question to ask, not where the marker goes. */
export default function ChatChart({ spec }: { spec: ChartSpec }) {
  const [hover, setHover] = useState<number | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const svg = useMemo(() => chartSvg(spec), [spec]);
  const height = cardHeight(spec);
  const sc = scales(spec);
  const s = spec.series;

  /* Nearest point to the pointer, in card units: the SVG scales to whatever
     width the panel gives it, so client pixels are mapped back through its own
     box rather than assumed to match. */
  const onMove = (event: React.PointerEvent) => {
    const box = boxRef.current?.getBoundingClientRect();
    if (!box) return;
    const px = ((event.clientX - box.left) / box.width) * CARD_W - sc.plotX;
    const first = sc.x(0);
    const at = Math.round(((px - first) / (sc.x(s.length - 1) - first)) * (s.length - 1));
    setHover(Math.max(0, Math.min(s.length - 1, at)));
  };

  const readout =
    hover === null
      ? null
      : `${shortDate(spec.dates[hover])} · score ${s[hover].toFixed(1)} · median ${spec.median.toFixed(0)}` +
        (s[hover] >= spec.threshold ? " · above the alert threshold" : "");

  return (
    <figure
      className="cchart"
      ref={boxRef}
      onPointerMove={onMove}
      onPointerLeave={() => setHover(null)}
      style={{ aspectRatio: `${CARD_W} / ${height}` }}
    >
      <div className="cchart-svg" dangerouslySetInnerHTML={{ __html: svg }} />

      {/* Laid over the top at the same scale, so the cursor lands on the line
          and the readout can cover the footer without the card changing
          height as the pointer moves. */}
      {hover !== null && (
        <svg
          className="cchart-hover"
          viewBox={`0 0 ${CARD_W} ${height}`}
          aria-hidden="true"
        >
          <g transform={`translate(${sc.plotX} ${sc.plotY})`}>
            <line x1={sc.x(hover)} x2={sc.x(hover)} y1={22} y2={138}
                  stroke={INK_MUTED} strokeWidth={1} opacity={0.3} />
            <circle cx={sc.x(hover)} cy={sc.y(s[hover])} r={3} fill={INK}
                    stroke={CARD} strokeWidth={1.5} />
          </g>
          <rect x={1} y={height - footLines(spec).length * 13 - 8}
                width={CARD_W - 2} height={footLines(spec).length * 13 + 7} fill={CARD} />
          <text x={10} y={height - 11} fontSize={10} fill={INK}>{readout}</text>
        </svg>
      )}
    </figure>
  );
}
