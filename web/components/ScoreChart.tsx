"use client";

import { motion } from "framer-motion";
import { useState } from "react";
import { type RupertScored, shortDay } from "@/lib/rupert";

/* What Rupert scored one security, day by day.
 *
 * The line is tone — FinBERT's `positive - negative`, trimmed by `reduce.mood`
 * — on a scale that is fixed at -1..+1 rather than fitted to the data. A tone
 * axis that rescaled itself would make a quiet week of +0.02 to +0.05 look
 * identical to a week that ran from despair to euphoria, which is the one thing
 * a sentiment chart must never do.
 *
 * Bars behind it are how many mentions that day, on their own scale. They are
 * there because the line is meaningless without them: a tone computed from
 * eleven comments and one computed from four hundred are drawn the same width
 * and are not the same evidence.
 *
 * **Gaps are real.** A day whose trailing window did not reach the floor gets no
 * point and the line breaks, rather than being interpolated across or drawn at
 * zero — zero is what a genuinely balanced week looks like and must stay
 * distinguishable from "not enough was said to tell". */

const W = 760;
const H = 210;
const M = { t: 16, r: 34, b: 26, l: 34 };

export default function ScoreChart({
  series,
  rollingDays,
}: {
  series: RupertScored[];
  rollingDays: number;
}) {
  const [hover, setHover] = useState<number | null>(null);

  if (series.length === 0) return <p className="rup-empty">Nothing scored yet.</p>;

  const plotW = W - M.l - M.r;
  const plotH = H - M.t - M.b;
  const band = plotW / series.length;
  const barW = Math.max(1.5, band - 2);

  const maxMentions = Math.max(1, ...series.map((d) => d.mentions));
  const x = (i: number) => M.l + i * band + band / 2;
  // Fixed domain. See the note above: this axis never rescales.
  const yTone = (v: number) => M.t + plotH / 2 - (v * plotH) / 2;
  const yBar = (v: number) => M.t + plotH - (v / maxMentions) * (plotH * 0.42);

  /* One path per unbroken run, so a gap is a gap rather than a straight line
     drawn through the days nobody said enough on. */
  const runs: string[] = [];
  let current: string[] = [];
  series.forEach((d, i) => {
    if (d.tone === null) {
      if (current.length > 1) runs.push(current.join(""));
      current = [];
      return;
    }
    current.push(`${current.length ? "L" : "M"}${x(i).toFixed(1)},${yTone(d.tone).toFixed(1)}`);
  });
  if (current.length > 1) runs.push(current.join(""));

  const dots = series
    .map((d, i) => ({ d, i }))
    .filter(({ d }) => d.tone !== null);

  const shown = hover !== null ? series[hover] : null;
  const scored = series.filter((d) => d.tone !== null).length;

  return (
    <div className="rup-chart">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label="Tone and mention volume per day">
        {/* Zero, and the two quarter lines. Tone is signed, so the middle of
            the plot is a real value rather than the bottom of a range. */}
        {[1, 0.5, 0, -0.5, -1].map((v) => (
          <g key={v}>
            <line x1={M.l} x2={W - M.r} y1={yTone(v)} y2={yTone(v)}
                  stroke={v === 0 ? "var(--ink-muted)" : "var(--line)"}
                  strokeWidth={v === 0 ? 1 : 0.75}
                  strokeDasharray={v === 0 ? undefined : "2 3"} />
            <text x={M.l - 6} y={yTone(v) + 3.5} textAnchor="end"
                  fontSize={9.5} fill="var(--ink-muted)">
              {v > 0 ? `+${v}` : v}
            </text>
          </g>
        ))}

        {series.map((d, i) => (
          <g key={d.day}
             onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
            <rect x={M.l + i * band} y={M.t} width={band} height={plotH} fill="transparent" />
            <motion.rect
              x={x(i) - barW / 2} width={barW}
              y={yBar(d.mentions)}
              height={Math.max(0, M.t + plotH - yBar(d.mentions))}
              rx={1.5} fill="var(--line)"
              initial={{ opacity: 0 }}
              animate={{ opacity: hover === null || hover === i ? 1 : 0.45 }}
              transition={{ duration: 0.18 }}
            />
          </g>
        ))}

        {runs.map((d, i) => (
          <motion.path
            key={i} d={d} fill="none" stroke="var(--copper)" strokeWidth={1.9}
            strokeLinecap="round" strokeLinejoin="round"
            initial={{ pathLength: 0 }} animate={{ pathLength: 1 }}
            transition={{ duration: 0.85, ease: [0, 0, 0.2, 1] }}
          />
        ))}

        {dots.map(({ d, i }) => (
          <circle key={d.day} cx={x(i)} cy={yTone(d.tone as number)}
                  r={hover === i ? 3.4 : 1.9}
                  fill={hover === i ? "var(--copper)" : "var(--card)"}
                  stroke="var(--copper)" strokeWidth={1.4} />
        ))}

        <text x={M.l} y={H - 8} fontSize={9.5} fill="var(--ink-muted)">
          {shortDay(series[0].day)}
        </text>
        <text x={W - M.r} y={H - 8} textAnchor="end" fontSize={9.5} fill="var(--ink-muted)">
          {shortDay(series[series.length - 1].day)}
        </text>

        {hover !== null && (
          <line x1={x(hover)} x2={x(hover)} y1={M.t} y2={M.t + plotH}
                stroke="var(--ink-muted)" strokeWidth={0.75} strokeDasharray="2 2" />
        )}
      </svg>

      <div className="rup-readout">
        {shown ? (
          <>
            <b>{shortDay(shown.day)}</b>
            <span>{shown.mentions.toLocaleString()} mentions that day</span>
            {shown.tone === null ? (
              <span>
                no score — only {shown.window_mentions} in the {rollingDays}-day window
              </span>
            ) : (
              <span className={shown.tone > 0 ? "rup-pos" : shown.tone < 0 ? "rup-neg" : undefined}>
                scored {shown.tone > 0 ? "+" : ""}{shown.tone.toFixed(2)} on{" "}
                {shown.window_mentions} mentions
              </span>
            )}
          </>
        ) : (
          <>
            <span className="rup-key-tone">tone, trailing {rollingDays} days</span>
            <span className="rup-key-other">mentions per day</span>
            <span>{scored} of {series.length} days had enough to score</span>
          </>
        )}
      </div>
    </div>
  );
}
