"use client";

import { animate, motion } from "framer-motion";
import { useEffect, useRef } from "react";

function CountUp({ to, delay = 0 }: { to: number; delay?: number }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const controls = animate(0, to, {
      delay,
      duration: 0.9,
      ease: [0, 0, 0.2, 1],
      onUpdate: (v) => {
        if (ref.current) ref.current.textContent = String(Math.round(v));
      },
    });
    return () => controls.stop();
  }, [to, delay]);
  return <span ref={ref}>0</span>;
}

const TILES = [
  { k: "Universe scored", v: 487, suffix: " / 500", s: "13 below the coverage floor, skipped not imputed" },
  { k: "Threshold crossings", v: 3, alerted: true, s: "1 suppressed by cooldown · fires on the crossing, not the state" },
  { k: "Agreement ≥ 4 pillars", v: 12, s: "top-quartile in four or more pillars at once" },
  { k: "Thin sectors", v: 2, s: "fell back to industry group · min 20 peers enforced" },
];

export default function StatTiles() {
  return (
    <div className="tiles">
      {TILES.map((t, i) => (
        <motion.div
          key={t.k}
          className={`tile${t.alerted ? " alerted" : ""}`}
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 + i * 0.07, duration: 0.5, ease: [0, 0, 0.2, 1] }}
        >
          <div className="k">{t.k}</div>
          <div className="v">
            <CountUp to={t.v} delay={0.15 + i * 0.07} />
            {t.suffix ? <small>{t.suffix}</small> : null}
          </div>
          <div className="s">{t.s}</div>
        </motion.div>
      ))}
    </div>
  );
}
