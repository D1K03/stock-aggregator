"use client";

import { motion } from "framer-motion";
import Sparkline from "@/components/Sparkline";
import { PAGE_SIZE, PILLAR_KEYS, PILLAR_NAMES, type ScreenRow, pageRange } from "@/lib/screen";

function DeltaCell({ delta }: { delta: string | null }) {
  // Null when there is no previous night under the same weights, or this
  // security was not scored on it (D9).
  if (delta === null) return <td className="num delta flat">new</td>;
  const d = Number(delta);
  const cls = d > 0 ? "up" : d < 0 ? "down" : "flat";
  const sym = d > 0 ? "▲" : d < 0 ? "▼" : "·";
  return <td className={`num delta ${cls}`}>{sym} {d > 0 ? "+" : ""}{delta}</td>;
}

export default function UniverseTable({
  rows, total, offset, selected, onSelect, onPage,
}: {
  /** Null while a page is being read: never the previous page's rows (D15). */
  rows: ScreenRow[] | null;
  total: number;
  offset: number;
  selected: string | null;
  onSelect: (symbol: string) => void;
  onPage: (offset: number) => void;
}) {
  return (
    <section className="card">
      <h2>Screen by blended score</h2>
      <div className="sub">
        Percentiles within sector, averaged within pillar. The blend is derivable — pillar
        scores and their raw inputs are the record.
      </div>
      <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Security</th><th>Sector</th><th className="num">Score</th><th className="num">Δ 1d</th>
            <th>Pillars · V Q M</th><th>Agree</th><th>30d</th>
          </tr>
        </thead>
        <tbody>
          {rows === null ? (
            <tr className="empty-row"><td colSpan={7}>Reading the screen…</td></tr>
          ) : rows.length === 0 ? (
            <tr className="empty-row"><td colSpan={7}>No securities match these filters.</td></tr>
          ) : (
            rows.map((r, ri) => (
              <motion.tr
                key={r.symbol}
                className={r.symbol === selected ? "sel" : undefined}
                onClick={() => onSelect(r.symbol)}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.12 + Math.min(ri, 20) * 0.035, duration: 0.4, ease: [0, 0, 0.2, 1] }}
              >
                <td><span className="tick">{r.symbol}<small>{r.name}</small></span></td>
                <td className="sector">{r.sector.name}</td>
                <td className="num score">
                  {r.score}
                  {r.partial ? (
                    <span className="partial-mark" title="Partial: a weighted pillar is below full coverage">◐</span>
                  ) : null}
                </td>
                <DeltaCell delta={r.delta} />
                <td>
                  <div className="pillars">
                    {PILLAR_KEYS.map((key) => {
                      const cell = r.pillars[key];
                      const v = cell ? Number(cell.score) : null;
                      return (
                        <div
                          key={key}
                          className={`pl${v !== null && v >= 75 ? " top" : ""}`}
                          title={cell ? `${PILLAR_NAMES[key]} ${cell.score}` : `${PILLAR_NAMES[key]}: not scored`}
                        >
                          <b>{key}</b>
                          {v === null ? (
                            <span className="pl-dash">—</span>
                          ) : (
                            <div className="bar">
                              <motion.div
                                className="fill"
                                initial={{ width: 0 }}
                                animate={{ width: `${v}%` }}
                                transition={{ delay: 0.25 + Math.min(ri, 20) * 0.035, duration: 0.6, ease: [0, 0, 0.2, 1] }}
                              />
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </td>
                <td>
                  <span className="agree" title={`${r.agreement} of 3 pillars top-quartile`}>
                    {"●".repeat(r.agreement)}
                    <span>{"●".repeat(3 - r.agreement)}</span>
                  </span>
                </td>
                <td><Sparkline h={r.closes.map(([, close]) => Number(close))} /></td>
              </motion.tr>
            ))
          )}
        </tbody>
      </table>
      </div>
      {rows !== null && total > PAGE_SIZE ? (
        <div className="pager">
          <button onClick={() => onPage(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}>
            Previous
          </button>
          <span>{pageRange(offset, rows.length, total)}</span>
          <button onClick={() => onPage(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= total}>
            Next
          </button>
        </div>
      ) : null}
    </section>
  );
}
