"use client";

import { animate, motion } from "framer-motion";
import { useEffect, useRef } from "react";
import { type Tiles, count } from "@/lib/screen";

function CountUp({ to, delay = 0 }: { to: number; delay?: number }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const controls = animate(0, to, {
      delay,
      duration: 0.9,
      ease: [0, 0, 0.2, 1],
      onUpdate: (v) => {
        if (ref.current) ref.current.textContent = count(Math.round(v));
      },
    });
    return () => controls.stop();
  }, [to, delay]);
  return <span ref={ref}>0</span>;
}

/* The night, not the page: these do not change with the filters (D9). */
export default function StatTiles({ tiles }: { tiles: Tiles }) {
  const items = [
    {
      k: "Scored", v: tiles.scored, suffix: ` / ${count(tiles.active_now)} active now`,
      s: "a snapshot on this night, against the universe as it stands today",
    },
    {
      k: "Partial scores", v: tiles.partial,
      s: "a weighted pillar below full coverage; marked ◐, not hidden",
    },
    {
      k: "All three pillars top-quartile", v: tiles.agreement_3,
      s: "valuation, quality and momentum in their top quarter at once",
    },
    {
      k: "Metric values ranked against the market", v: tiles.market_ranked_values,
      s: "a sector too thin to rank within, so the whole market stood in",
    },
  ];
  return (
    <div className="tiles">
      {items.map((t, i) => (
        <motion.div
          key={t.k}
          className="tile"
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
