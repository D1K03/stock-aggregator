"use client";

import { motion } from "framer-motion";
import { PILLAR_KEYS, PILLAR_NAMES, Row, agree, history } from "@/lib/data";
import Sparkline from "@/components/Sparkline";

function DeltaCell({ d }: { d: number }) {
  const cls = d > 0 ? "up" : d < 0 ? "down" : "flat";
  const sym = d > 0 ? "▲" : d < 0 ? "▼" : "·";
  return <td className={`num delta ${cls}`}>{sym} {d > 0 ? "+" : ""}{d}</td>;
}

export default function UniverseTable({
  rows, selected, onSelect,
}: {
  rows: Row[]; selected: string; onSelect: (sym: string) => void;
}) {
  return (
    <section className="card">
      <h2>Universe by blended score</h2>
      <div className="sub">
        Percentiles are computed within sector, then averaged within pillar. The blend is
        derivable — pillar scores are the record.
      </div>
      <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Security</th><th>Sector</th><th className="num">Score</th><th className="num">Δ 1d</th>
            <th>Pillars · V Q M S I</th><th>Agree</th><th>Flags</th><th>30d</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => (
            <motion.tr
              key={r.sym}
              className={r.sym === selected ? "sel" : undefined}
              onClick={() => onSelect(r.sym)}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.12 + ri * 0.035, duration: 0.4, ease: [0, 0, 0.2, 1] }}
            >
              <td><span className="tick">{r.sym}<small>{r.name}</small></span></td>
              <td className="sector">{r.sector}</td>
              <td className="num score">{r.score}</td>
              <DeltaCell d={r.score - r.prev} />
              <td>
                <div className="pillars">
                  {r.p.map((v, i) => (
                    <div
                      key={PILLAR_KEYS[i]}
                      className={`pl${v >= 75 ? " top" : ""}`}
                      title={`${PILLAR_NAMES[PILLAR_KEYS[i]]} ${v}th percentile in ${r.sector}`}
                    >
                      <b>{PILLAR_KEYS[i]}</b>
                      <div className="bar">
                        <motion.div
                          className="fill"
                          initial={{ width: 0 }}
                          animate={{ width: `${v}%` }}
                          transition={{ delay: 0.25 + ri * 0.035, duration: 0.6, ease: [0, 0, 0.2, 1] }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </td>
              <td>
                <span className="agree">
                  {"●".repeat(agree(r))}
                  <span>{"●".repeat(5 - agree(r))}</span>
                </span>
              </td>
              <td>{r.flags.map((f) => <span key={f} className="flag">⚑ {f}</span>)}</td>
              <td><Sparkline h={history(r)} /></td>
            </motion.tr>
          ))}
        </tbody>
      </table>
      </div>
    </section>
  );
}
