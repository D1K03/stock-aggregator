"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useMemo, useRef, useState } from "react";
import { MEDIANS, PEERS, ROWS, THRESHOLD, history } from "@/lib/data";

const W = 356, H = 190;
const m = { t: 14, r: 44, b: 20, l: 30 };

export default function ScoreChart({ sym }: { sym: string }) {
  const row = ROWS.find((r) => r.sym === sym) ?? ROWS[0];
  const h = useMemo(() => history(row), [row]);
  const med = MEDIANS[row.sector];
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const lo = Math.min(20, ...h) - 4;
  const hi = Math.max(85, ...h) + 4;
  const x = (i: number) => m.l + (i / (h.length - 1)) * (W - m.l - m.r);
  const y = (v: number) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const line = h.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");

  let cross = -1;
  for (let i = 1; i < h.length; i++) {
    if (h[i - 1] < THRESHOLD !== h[i] < THRESHOLD) cross = i;
  }

  const onMove = (e: React.MouseEvent) => {
    const b = svgRef.current!.getBoundingClientRect();
    const px = ((e.clientX - b.left) / b.width) * W;
    const i = Math.max(0, Math.min(h.length - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * (h.length - 1))));
    setHover(i);
  };
  const day = (i: number) => {
    // Counted back from today, matching what the agent sends with a chart it
    // draws. A fixed date here would have the same point labelled differently
    // on the page and in the chat.
    const d = new Date();
    d.setDate(d.getDate() - (h.length - 1 - i));
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  };

  return (
    <section className="card">
      <h2>{row.sym} — blended score, 60d</h2>
      <div className="legend">
        <span><i style={{ background: "var(--copper)" }} />Blended score</span>
        <span><i style={{ background: "var(--blue)" }} />Sector median</span>
      </div>
      <div className="chart-wrap">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          role="img"
          aria-label={`${row.sym} blended score history with sector median`}
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        >
          {[25, 50, 75].map((v) => (
            <g key={v}>
              <line x1={m.l} x2={W - m.r} y1={y(v)} y2={y(v)} stroke="var(--paper)" strokeWidth={1} />
              <text x={m.l - 7} y={y(v) + 3} textAnchor="end" fontSize={9} fill="var(--ink-muted)">{v}</text>
            </g>
          ))}
          <line x1={m.l} x2={W - m.r} y1={y(THRESHOLD)} y2={y(THRESHOLD)} stroke="var(--amber-4)" strokeWidth={1.5} />
          <text x={m.l + 4} y={y(THRESHOLD) - 5} fontSize={9} fill="var(--amber-5)">alert {THRESHOLD}</text>
          <line x1={m.l} x2={W - m.r} y1={y(med)} y2={y(med)} stroke="var(--blue)" strokeWidth={2} strokeLinecap="round" opacity={0.85} />

          <AnimatePresence mode="wait">
            <motion.g key={row.sym} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.18 }}>
              <motion.path
                d={line}
                fill="none"
                stroke="var(--copper)"
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 1.2, ease: [0, 0, 0.2, 1] }}
              />
              {cross >= 0 && (
                <motion.circle
                  cx={x(cross)}
                  cy={y(h[cross])}
                  r={5}
                  fill="var(--copper)"
                  stroke="var(--card)"
                  strokeWidth={2}
                  initial={{ scale: 0, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  transition={{ delay: 1.1, type: "spring", stiffness: 320, damping: 16 }}
                />
              )}
            </motion.g>
          </AnimatePresence>

          <text x={W - m.r + 6} y={y(h[h.length - 1]) + 3} fontSize={10} fontWeight={600} fill="var(--copper)">{row.score}</text>
          <text x={W - m.r + 6} y={y(med) + 3} fontSize={10} fill="var(--blue)">{med}</text>

          {hover !== null && (
            <line x1={x(hover)} x2={x(hover)} y1={m.t} y2={H - m.b} stroke="var(--ink-muted)" strokeWidth={1} opacity={0.35} />
          )}
        </svg>
        {hover !== null && (
          <div
            className="tt"
            style={{ left: `${(x(hover) / W) * 100}%`, top: (y(h[hover]) / H) * 100 + "%" }}
          >
            {day(hover)} · score <span className="mono">{h[hover].toFixed(0)}</span> · median <span className="mono">{med}</span>
          </div>
        )}
      </div>
      <div className="chart-foot">
        {row.sector} · {PEERS[row.sector]} peers · alert threshold {THRESHOLD} · a score can move because
        peers moved — raw metrics are kept beside every percentile
      </div>
    </section>
  );
}
