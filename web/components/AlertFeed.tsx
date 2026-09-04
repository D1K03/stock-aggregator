"use client";

import { motion } from "framer-motion";
import { ALERTS } from "@/lib/data";

export default function AlertFeed({ onSelect }: { onSelect: (sym: string) => void }) {
  return (
    <section className="card">
      <h2>Alerts</h2>
      <div className="sub">Wording is “score crossed threshold”, never a recommendation.</div>
      <div className="feed">
        {ALERTS.map((a, i) => (
          <motion.div
            key={a.sym}
            className={`alert${a.muted ? " muted" : ""}`}
            style={{ cursor: "pointer" }}
            onClick={() => onSelect(a.sym)}
            initial={{ opacity: 0, x: 16 }}
            animate={{ opacity: a.muted ? 0.62 : 1, x: 0 }}
            transition={{ delay: 0.25 + i * 0.09, duration: 0.45, ease: [0, 0, 0.2, 1] }}
          >
            <div className="head">
              <b>{a.sym}</b>
              <span className="move">
                {a.from} → <span className="to">{a.to}</span>
              </span>
              <span className="rule">
                {a.muted ? <span className="cooldown">{a.cooldown}</span> : a.rule}
              </span>
            </div>
            <div className="why">{a.why}</div>
            <div className="chips">
              {a.chips.map(([p, v]) => (
                <span key={p} className={`pchip${v >= 75 ? " hi" : ""}`}>{p} {v}</span>
              ))}
              {a.flag ? <span className="flag">⚑ {a.flag}</span> : null}
            </div>
          </motion.div>
        ))}
      </div>
    </section>
  );
}
