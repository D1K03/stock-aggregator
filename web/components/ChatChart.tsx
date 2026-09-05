"use client";

import { motion } from "framer-motion";
import { useRef, useState } from "react";
import type { ChartSpec, Mark } from "@/lib/threads";

/* A chart Steven drew, rendered inside a chat message.
 *
 * The same visual language as the dashboard's ScoreChart — copper line, amber
 * threshold, blue sector median — because a chart that answered a question
 * should be recognisably the same chart as the one on the page, and it is: the
 * agent computes it from a byte-identical copy of the same series.
 *
 * The annotation answers the question on its own — the tool that found the
 * point labelled and dated it — but the rest of the line is still worth
 * reading, so hovering gives the date and value under the cursor. The readout
 * sits at the bottom of the card rather than following the pointer: at sixty
 * points across a narrow panel a floating tooltip spends most of its time
 * covering the annotation it is meant to complement.
 *
 * Nothing here trusts the model. Marks arrive as indices into the series,
 * computed from the numbers; the model chose which question to ask, not where
 * the marker goes. */

const W = 340, H = 168;
const m = { t: 22, r: 38, b: 30, l: 26 };
const EASE = [0, 0, 0.2, 1] as const;

const TONE: Record<Mark["tone"], string> = {
  copper: "var(--copper)",
  blue: "var(--blue)",
  amber: "var(--amber-5)",
};

function shortDate(iso: string) {
  const d = new Date(`${iso}T00:00:00`);
  // Sliced to three letters because en-GB renders September as "Sept", and the
  // reply beside the chart says "5 Sep" — the same date spelled two ways in
  // one message reads as two different dates.
  const month = d.toLocaleDateString("en-GB", { month: "short" }).slice(0, 3);
  return `${d.getDate()} ${month}`;
}

export default function ChatChart({ spec }: { spec: ChartSpec }) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const s = spec.series;
  if (s.length < 2) return null;

  const lo = Math.min(20, ...s) - 4;
  const hi = Math.max(85, ...s) + 4;
  const x = (i: number) => m.l + (i / (s.length - 1)) * (W - m.l - m.r);
  const y = (v: number) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const line = s.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const last = s[s.length - 1];

  // The line finishes drawing before anything is marked on it, so the eye
  // follows the shape first and then lands on the answer.
  const afterLine = 0.85;

  /* Nearest point to the pointer, in viewBox units: the SVG scales to the
     panel, so client pixels have to be mapped back through its own box rather
     than assumed to match. */
  const onMove = (event: React.PointerEvent) => {
    const box = svgRef.current?.getBoundingClientRect();
    if (!box) return;
    const px = ((event.clientX - box.left) / box.width) * W;
    const at = Math.round(((px - m.l) / (W - m.l - m.r)) * (s.length - 1));
    setHover(Math.max(0, Math.min(s.length - 1, at)));
  };

  return (
    <figure className="cchart">
      <figcaption className="cchart-title">{spec.title}</figcaption>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label={`${spec.ticker} score history. ${spec.marks.map((k) => k.label).join(". ")}`}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
        style={{ touchAction: "pan-y" }}
      >
        {[25, 50, 75].map((v) => (
          <g key={v}>
            <line x1={m.l} x2={W - m.r} y1={y(v)} y2={y(v)} stroke="var(--paper)" strokeWidth={1} />
            <text x={m.l - 5} y={y(v) + 3} textAnchor="end" fontSize={8} fill="var(--ink-muted)">{v}</text>
          </g>
        ))}

        <line x1={m.l} x2={W - m.r} y1={y(spec.threshold)} y2={y(spec.threshold)}
              stroke="var(--amber-4)" strokeWidth={1.2} />
        <line x1={m.l} x2={W - m.r} y1={y(spec.median)} y2={y(spec.median)}
              stroke="var(--blue)" strokeWidth={1.6} strokeLinecap="round" opacity={0.8} />

        {/* Spans sit under the line so the shading never dims the data. */}
        {spec.marks.filter((k) => k.kind === "span" && k.end !== null).map((k, i) => (
          <motion.rect
            key={`band${i}`}
            x={x(k.index)} width={Math.max(2, x(k.end!) - x(k.index))}
            y={m.t} height={H - m.t - m.b}
            fill={TONE[k.tone]} opacity={0.1}
            initial={{ opacity: 0 }} animate={{ opacity: 0.1 }}
            transition={{ delay: afterLine, duration: 0.4 }}
          />
        ))}

        <motion.path
          d={line} fill="none" stroke="var(--copper)" strokeWidth={1.8}
          strokeLinejoin="round" strokeLinecap="round"
          initial={{ pathLength: 0 }} animate={{ pathLength: 1 }}
          transition={{ duration: 1, ease: EASE }}
        />

        <text x={W - m.r + 5} y={y(last) + 3} fontSize={9} fontWeight={600} fill="var(--copper)">
          {last.toFixed(0)}
        </text>
        <text x={W - m.r + 5} y={y(spec.median) + 3} fontSize={9} fill="var(--blue)">
          {spec.median.toFixed(0)}
        </text>

        {spec.marks.map((k, i) => (
          <Annotation key={`mark${i}`} mark={k} spec={spec} x={x} y={y} delay={afterLine + 0.15} />
        ))}

        {hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={m.t} y2={H - m.b}
                  stroke="var(--ink-muted)" strokeWidth={1} opacity={0.3} />
            <circle cx={x(hover)} cy={y(s[hover])} r={3} fill="var(--ink)"
                    stroke="var(--card)" strokeWidth={1.5} />
          </g>
        )}

        {/* Only the ends are dated on the axis; anything marked dates itself. */}
        <text x={m.l} y={H - 6} fontSize={8} fill="var(--ink-muted)">{shortDate(spec.dates[0])}</text>
        <text x={W - m.r} y={H - 6} fontSize={8} textAnchor="end" fill="var(--ink-muted)">
          {shortDate(spec.dates[spec.dates.length - 1])}
        </text>
      </svg>
      {/* Swapped for the readout while hovering, so the card does not change
          height as the pointer moves across it. */}
      <div className="cchart-foot">
        {hover === null ? (
          spec.subtitle
        ) : (
          <span className="cchart-read">
            {shortDate(spec.dates[hover])} · score <b>{s[hover].toFixed(1)}</b>
            {" · median "}
            <b>{spec.median.toFixed(0)}</b>
            {s[hover] >= spec.threshold ? " · above the alert threshold" : ""}
          </span>
        )}
      </div>
    </figure>
  );
}

function Annotation({
  mark, spec, x, y, delay,
}: {
  mark: Mark;
  spec: ChartSpec;
  x: (i: number) => number;
  y: (v: number) => number;
  delay: number;
}) {
  const colour = TONE[mark.tone];
  const isSpan = mark.kind === "span" && mark.end !== null;
  const from = mark.index;
  const to = isSpan ? mark.end! : mark.index;

  /* The label goes in the top margin rather than beside the point it marks.
     Next to the point it lands on the line itself wherever the series is busy,
     which is exactly where anything worth marking tends to be. Up here it is
     always legible, and the dashed rule or the shaded band carries the eye
     down to the place it refers to. */
  const anchor = isSpan ? (x(from) + x(to)) / 2 : x(to);
  const width = mark.label.length * 4.9 + 11;
  // Clamped to the viewBox, so a mark near either end is not half cut off.
  const cx = Math.min(W - width / 2 - 2, Math.max(width / 2 + 2, anchor));

  const when = isSpan
    ? `${shortDate(spec.dates[from])} – ${shortDate(spec.dates[to])}`
    : shortDate(spec.dates[to]);

  return (
    <motion.g
      initial={{ opacity: 0, y: -3 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.3, ease: EASE }}
    >
      {/* A point gets a rule through it; a span already has its band. */}
      {!isSpan && (
        <line x1={x(to)} x2={x(to)} y1={m.t} y2={H - m.b + 4} stroke={colour}
              strokeWidth={1} strokeDasharray="2 3" opacity={0.55} />
      )}
      {isSpan && <circle cx={x(from)} cy={y(spec.series[from])} r={2.6} fill={colour} opacity={0.7} />}
      <circle cx={x(to)} cy={y(spec.series[to])} r={4} fill={colour} stroke="var(--card)" strokeWidth={2} />

      <rect x={cx - width / 2} y={2} width={width} height={13} rx={6.5} fill={colour} />
      <text x={cx} y={11.5} textAnchor="middle" fontSize={8.5} fontWeight={600} fill="#fff">
        {mark.label}
      </text>
      <text x={cx} y={H - m.b + 14} textAnchor="middle" fontSize={8} fill={colour} fontWeight={600}>
        {when}
      </text>
    </motion.g>
  );
}
