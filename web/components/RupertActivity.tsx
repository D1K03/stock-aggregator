"use client";

import { motion } from "framer-motion";
import { useState } from "react";
import { type RupertDay, shortDay, usd } from "@/lib/rupert";

/* What Rupert did each day, and what it cost.
 *
 * **Not a second copy of `lib/chart-svg.ts`, and worth saying why.** That file
 * exists because Steven draws the same chart in two places — the dashboard and
 * Discord — and it is one string so the two surfaces cannot drift. It takes a
 * `ChartSpec`: one security, one series, price or score. This is a different
 * shape (two series, two units, thirty days of the resolver's own work) with
 * one surface and no rasteriser, so there is nothing here to drift from. It is
 * `Sparkline`'s situation rather than the chart card's.
 *
 * Two units on one plot, which normally is a bad idea and here is the point:
 * the question is whether cost tracks work, and reading that off two charts
 * stacked vertically is guesswork. Bars are decisions and carry their own left
 * scale; the line is dollars on the right. The bars are split so the resolved
 * share is visible inside the total — the gap between them is the corpus
 * talking about nothing, which is most of it and should look like most of it. */

const W = 760;
const H = 190;
/* The right margin has to hold the widest label `usd()` can produce. At these
   prices that is a five-decimal figure like "$0.00129" — eight characters at
   9.5px, which is about 44px. 46 was measured against "$0.02" and clipped the
   moment a day cost less than a cent. */
const M = { t: 14, r: 58, b: 26, l: 34 };

export default function RupertActivity({ days }: { days: RupertDay[] }) {
  const [hover, setHover] = useState<number | null>(null);

  if (days.length === 0) {
    return <p className="rup-empty">Nothing decided yet.</p>;
  }

  const plotW = W - M.l - M.r;
  const plotH = H - M.t - M.b;

  // A day with no work still needs a scale, or every bar is full height.
  const maxDecisions = Math.max(1, ...days.map((d) => d.decisions));
  const maxCost = Math.max(1e-6, ...days.map((d) => d.cost_usd));

  const band = plotW / days.length;
  // A one-pixel gutter each side, and never thinner than a hairline: thirty
  // days across 680px is a 22px band, and a phone is a good deal less.
  const barW = Math.max(1.5, band - 2);

  const x = (i: number) => M.l + i * band + band / 2;
  const yBar = (v: number) => M.t + plotH - (v / maxDecisions) * plotH;
  const yCost = (v: number) => M.t + plotH - (v / maxCost) * plotH;

  const costPath = days
    .map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${yCost(d.cost_usd).toFixed(1)}`)
    .join("");

  // Three ticks including zero, so the left axis says what the bars mean.
  const ticks = [0, Math.round(maxDecisions / 2), maxDecisions];
  const shown = hover !== null ? days[hover] : null;

  return (
    <div className="rup-chart">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label="Decisions and cost per day over the last thirty days">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={M.l} x2={W - M.r} y1={yBar(t)} y2={yBar(t)}
                  stroke="var(--line)" strokeWidth={1} />
            <text x={M.l - 6} y={yBar(t) + 3.5} textAnchor="end"
                  fontSize={9.5} fill="var(--ink-muted)">{t}</text>
          </g>
        ))}

        {days.map((d, i) => (
          <g key={d.day}
             onMouseEnter={() => setHover(i)}
             onMouseLeave={() => setHover(null)}>
            {/* A full-height invisible target, so hovering a day with two
                decisions does not mean hitting a 3px bar. */}
            <rect x={M.l + i * band} y={M.t} width={band} height={plotH} fill="transparent" />
            <motion.rect
              x={x(i) - barW / 2}
              width={barW}
              y={yBar(d.decisions)}
              height={Math.max(0, M.t + plotH - yBar(d.decisions))}
              rx={1.5}
              fill="var(--line)"
              initial={{ opacity: 0 }}
              animate={{ opacity: hover === null || hover === i ? 1 : 0.45 }}
              transition={{ duration: 0.18 }}
            />
            <motion.rect
              x={x(i) - barW / 2}
              width={barW}
              y={yBar(d.resolved)}
              height={Math.max(0, M.t + plotH - yBar(d.resolved))}
              rx={1.5}
              fill="var(--copper)"
              initial={{ opacity: 0 }}
              animate={{ opacity: hover === null || hover === i ? 1 : 0.45 }}
              transition={{ duration: 0.18 }}
            />
          </g>
        ))}

        <motion.path
          d={costPath}
          fill="none"
          stroke="var(--blue)"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 0.9, ease: [0, 0, 0.2, 1] }}
        />

        {/* Anchored to the right edge rather than started at the plot's, so a
            longer figure grows inwards into the margin instead of off the
            viewBox and out of the picture. */}
        <text x={W - 4} y={M.t + 4} textAnchor="end" fontSize={9.5} fill="var(--blue)">
          {usd(maxCost)}
        </text>
        <text x={W - 4} y={M.t + plotH + 3.5} textAnchor="end" fontSize={9.5}
              fill="var(--ink-muted)">
          $0
        </text>

        {/* First and last only. Thirty dates along this axis is a smear. */}
        <text x={M.l} y={H - 8} fontSize={9.5} fill="var(--ink-muted)">
          {shortDay(days[0].day)}
        </text>
        <text x={W - M.r} y={H - 8} textAnchor="end" fontSize={9.5} fill="var(--ink-muted)">
          {shortDay(days[days.length - 1].day)}
        </text>

        {hover !== null && (
          <line x1={x(hover)} x2={x(hover)} y1={M.t} y2={M.t + plotH}
                stroke="var(--ink-muted)" strokeWidth={0.75} strokeDasharray="2 2" />
        )}
      </svg>

      {/* Below the plot rather than floating over it: a tooltip that follows the
          cursor covers the bars either side of the one being read. */}
      <div className="rup-readout">
        {shown ? (
          <>
            <b>{shortDay(shown.day)}</b>
            <span>{shown.decisions.toLocaleString()} decisions</span>
            <span className="rup-key-resolved">{shown.resolved.toLocaleString()} resolved</span>
            <span className="rup-key-cost">{usd(shown.cost_usd)}</span>
          </>
        ) : (
          <>
            <span className="rup-key-resolved">resolved</span>
            <span className="rup-key-other">decided, about nothing</span>
            <span className="rup-key-cost">cost</span>
          </>
        )}
      </div>
    </div>
  );
}
