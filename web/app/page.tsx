"use client";

import { motion } from "framer-motion";
import { useCallback, useMemo, useRef, useState } from "react";
import Sidebar from "@/components/Sidebar";
import StatTiles from "@/components/StatTiles";
import UniverseTable from "@/components/UniverseTable";
import ScoreChart from "@/components/ScoreChart";
import AlertFeed from "@/components/AlertFeed";
import { MEDIANS, PILLAR_KEYS, ROWS, agree } from "@/lib/data";
import { usePublishScreen } from "@/lib/screen-context";

export default function Page() {
  const [selected, setSelected] = useState("NVDA");
  const chartRef = useRef<HTMLDivElement>(null);
  // On one-column layouts the chart lives above the table, so a tapped row
  // would otherwise change something offscreen. Bring it into view instead.
  const select = useCallback((sym: string) => {
    setSelected(sym);
    if (typeof window !== "undefined" && window.innerWidth <= 1100) {
      chartRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, []);
  const [sector, setSector] = useState("");
  const [minAgree, setMinAgree] = useState(false);
  const [flagged, setFlagged] = useState(false);

  const sectors = useMemo(() => [...new Set(ROWS.map((r) => r.sector))].sort(), []);

  /* What the palette will describe if asked. Built from the same state the
     table renders from, so it cannot describe a row that is not selected. */
  const row = ROWS.find((r) => r.sym === selected);
  const summary = useMemo(() => {
    if (!row) return "the universe table";
    const pillars = row.p.map((v, i) => `${PILLAR_KEYS[i]} ${v}`).join(", ");
    const delta = row.score - row.prev;
    const filters = [
      sector ? `sector ${sector}` : null,
      minAgree ? "agreement at least 3" : null,
      flagged ? "flagged only" : null,
    ].filter(Boolean).join(", ");
    return (
      `${row.sym} (${row.name}), ${row.sector}, blended score ${row.score}, ` +
      `${delta >= 0 ? "up" : "down"} ${Math.abs(delta)} on the day, ` +
      `pillar percentiles ${pillars}, ${agree(row)} of 5 pillars top-quartile, ` +
      `sector median ${MEDIANS[row.sector]}` +
      (row.flags.length ? `, flags: ${row.flags.join(" and ")}` : ", no event flags") +
      (filters ? `. Filters: ${filters}` : "")
    );
  }, [row, sector, minAgree, flagged]);

  usePublishScreen("Overview", summary, true);
  const rows = ROWS.filter(
    (r) =>
      (!sector || r.sector === sector) &&
      (!minAgree || agree(r) >= 3) &&
      (!flagged || r.flags.length > 0)
  );

  return (
    <div className="shell">
      <Sidebar active="Overview" />
      <div className="content">
      <div className="wrap">
        <motion.header
          className="hero"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.55, ease: [0, 0, 0.2, 1] }}
        >
          <h1>Morning snapshot</h1>
          <p>
            Friday 5 September 2026 · run <b>#412</b> finished 06:41 ·{" "}
            <span className="live"><i />live · ok</span> · weights <b>v3-momentum-heavy</b> ·
            cutoff <b>+1d 6h</b>
          </p>
        </motion.header>

        <motion.div
          className="concept-bar"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.3, duration: 0.4 }}
        >
          <b>Concept</b>
          <span>
            Every figure below is illustrative and shaped by the schema. Ingest does not
            exist yet, so nothing here reads a real snapshot.
          </span>
        </motion.div>

        <div className="filters">
          <label>Sector</label>
          <select value={sector} onChange={(e) => setSector(e.target.value)}>
            <option value="">All sectors</option>
            {sectors.map((s) => <option key={s}>{s}</option>)}
          </select>
          <button className="chip-toggle" aria-pressed={minAgree} onClick={() => setMinAgree(!minAgree)}>
            Pillar agreement ≥ 3
          </button>
          <button className="chip-toggle" aria-pressed={flagged} onClick={() => setFlagged(!flagged)}>
            Has event flag
          </button>
          <span className="range">Last 60 trading days</span>
        </div>

        <StatTiles />

        <div className="grid">
          <UniverseTable rows={rows} selected={selected} onSelect={select} />
          <div className="col" ref={chartRef}>
            <ScoreChart sym={selected} />
            <AlertFeed onSelect={select} />
          </div>
        </div>
      </div>

      <footer className="site">
        <div className="wrap">
          Every score traces to visible raw inputs · provenance
          <span className="prov">git 66371b6</span>
          <span className="prov">config 9f3a71c2</span>
          <span className="prov">weights v3</span>
        </div>
      </footer>
      </div>
    </div>
  );
}
