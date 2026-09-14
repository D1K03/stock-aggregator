"use client";

import { motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import ChatChart from "@/components/ChatChart";
import Sidebar from "@/components/Sidebar";
import StatTiles from "@/components/StatTiles";
import UniverseTable from "@/components/UniverseTable";
import WhyPanel from "@/components/WhyPanel";
import { shortDate } from "@/lib/chart-svg";
import {
  type Awaiting, type ScreenPage, type SecurityDetail, type Sort,
  SORTS, clock, fetchScreen, fetchSecurity, longDate, pageRange, priceSpec,
  screenErrorText, summarise,
} from "@/lib/screen";
import { usePublishScreen } from "@/lib/screen-context";

/* A reply, stamped with the request it answers. What is drawn is checked against
   the request wanted now, so a slow reply to an old filter is never drawn as the
   answer to a new one, and "loading" is simply having no reply for this key. */
type Answer<T> = { key: string; value: T | null; error: string | null };

export default function Page() {
  const [sector, setSector] = useState("");
  const [agree, setAgree] = useState("");
  const [partial, setPartial] = useState("");
  const [sort, setSort] = useState<Sort>("score");
  const [offset, setOffset] = useState(0);
  const [chosen, setChosen] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  /* The night being browsed (D8). A ref, not state: it is pinned in reply to the
     first page, and pinning must not request that same page again. */
  const pinned = useRef<number | undefined>(undefined);
  const chartRef = useRef<HTMLDivElement>(null);

  /* The last night served. Kept across requests so the header, filters and tiles
     do not blink out while the next page is read; they describe the night, which
     a filter does not change. Rows are never kept this way. */
  const [night, setNight] = useState<ScreenPage | null>(null);

  const screenKey = `${sector}|${agree}|${partial}|${sort}|${offset}|${reloads}`;
  const [screen, setScreen] = useState<Answer<ScreenPage | Awaiting> | null>(null);
  useEffect(() => {
    let live = true;
    fetchScreen({ run: pinned.current, sector, agree, partial, sort, offset }).then(
      (value) => {
        if (!live) return;
        if (value.state === "ready") {
          pinned.current ??= value.run.id;
          setNight(value);
        }
        setScreen({ key: screenKey, value, error: null });
      },
      (exc: unknown) => {
        if (live) setScreen({ key: screenKey, value: null, error: screenErrorText(exc) });
      },
    );
    return () => {
      live = false;
    };
  }, [sector, agree, partial, sort, offset, screenKey]);

  const answer = screen?.key === screenKey ? screen : null;
  const page = answer?.value?.state === "ready" ? answer.value : null;
  const awaiting = answer?.value?.state === "awaiting_first_night";
  const selected = chosen ?? page?.rows[0]?.symbol ?? null;
  const runId = night?.run.id;

  const detailKey = `${selected}|${runId}`;
  const [detail, setDetail] = useState<Answer<SecurityDetail | Awaiting> | null>(null);
  useEffect(() => {
    if (selected === null || runId === undefined) return;
    let live = true;
    fetchSecurity(selected, runId).then(
      (value) => {
        if (live) setDetail({ key: detailKey, value, error: null });
      },
      (exc: unknown) => {
        if (live) setDetail({ key: detailKey, value: null, error: screenErrorText(exc) });
      },
    );
    return () => {
      live = false;
    };
  }, [selected, runId, detailKey]);
  const shown = detail?.key === detailKey ? detail : null;
  const security = shown?.value && "scored" in shown.value ? shown.value : null;

  // On one-column layouts the chart sits above the table, so a tapped row would
  // otherwise change something offscreen. Bring it into view instead.
  const select = useCallback((symbol: string) => {
    setChosen(symbol);
    if (typeof window !== "undefined" && window.innerWidth <= 1100) {
      chartRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, []);

  // A filter or a sort starts again at page one: page four of a narrower screen
  // is an empty table that reads as a broken filter.
  const refilter = <T,>(set: (value: T) => void) => (value: T) => {
    set(value);
    setOffset(0);
  };
  const refresh = () => {
    pinned.current = undefined;
    setNight(null);
    setChosen(null);
    setOffset(0);
    setReloads((n) => n + 1);
  };

  const sectorName = night?.sectors.find((s) => s.code === sector)?.name;
  const filters = [
    sectorName ? `sector ${sectorName}` : null,
    agree ? `pillar agreement at least ${agree}` : null,
    partial === "only" ? "partial scores only" : partial === "hide" ? "partial scores hidden" : null,
    sort !== "score" ? `sorted by ${SORTS.find((s) => s.value === sort)?.label}` : null,
  ].filter((f): f is string => f !== null);
  usePublishScreen(
    "Overview",
    awaiting
      ? "no night has been scored under v2 yet"
      : summarise(page?.rows.find((r) => r.symbol === selected), filters),
  );

  const run = night?.run;
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
          <h1>{run ? `Screen for ${longDate(run.as_of)}` : "Screen"}</h1>
          {run ? (
            <p>
              run <b>#{run.id}</b>
              {run.finished_at ? <> finished {clock(run.finished_at)}</> : null} · weights{" "}
              <b>{run.weight_version}</b> · alerts <b>off</b>
            </p>
          ) : null}
        </motion.header>

        {page && !page.latest ? (
          <div className="notice-bar">
            <b>A newer night is available.</b>
            <span>This view stays on {longDate(page.run.as_of)} so its pages never mix two nights.</span>
            <button className="chip-toggle" onClick={refresh}>Refresh</button>
          </div>
        ) : null}

        {awaiting ? (
          <section className="card awaiting">
            <h2>No night scored yet</h2>
            <div className="sub">
              The first v2 night has not been scored yet. The screen appears here once it has.
            </div>
          </section>
        ) : (
          <>
            {night ? (
              <div className="filters">
                <label>Sector</label>
                <select value={sector} onChange={(e) => refilter(setSector)(e.target.value)}>
                  <option value="">All sectors</option>
                  {night.sectors.map((s) => (
                    <option key={s.code} value={s.code}>{s.name}</option>
                  ))}
                </select>
                <label>Pillar agreement ≥</label>
                <select value={agree} onChange={(e) => refilter(setAgree)(e.target.value)}>
                  <option value="">any</option>
                  <option value="1">1</option>
                  <option value="2">2</option>
                  <option value="3">3</option>
                </select>
                <label>Partial</label>
                <select value={partial} onChange={(e) => refilter(setPartial)(e.target.value)}>
                  <option value="">all</option>
                  <option value="only">only</option>
                  <option value="hide">hide</option>
                </select>
                <label>Sort</label>
                <select value={sort} onChange={(e) => refilter(setSort)(e.target.value as Sort)}>
                  {SORTS.map((s) => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
                <span className="range">
                  {page ? pageRange(offset, page.rows.length, page.total) : "reading…"}
                </span>
              </div>
            ) : null}

            {night ? <StatTiles tiles={night.tiles} /> : null}

            <div className="grid">
              {answer?.error ? (
                <section className="card">
                  <h2>Screen unavailable</h2>
                  <div className="sub">{answer.error}</div>
                  <div className="card-actions">
                    <button className="chip-toggle" onClick={refresh}>Try again</button>
                  </div>
                </section>
              ) : (
                <UniverseTable
                  rows={page ? page.rows : null}
                  total={page?.total ?? 0}
                  offset={offset}
                  selected={selected}
                  onSelect={select}
                  onPage={setOffset}
                />
              )}
              <div className="col" ref={chartRef}>
                {security && security.closes.length >= 2 ? (
                  <ChatChart spec={priceSpec(security.symbol, security.name, security.closes)} />
                ) : null}
                {selected !== null ? (
                  <WhyPanel
                    detail={security}
                    error={shown?.error ?? null}
                    asOf={run ? shortDate(run.as_of) : null}
                  />
                ) : null}
              </div>
            </div>
          </>
        )}
      </div>

      <footer className="site">
        <div className="wrap">
          Every score traces to visible raw inputs
          {run ? (
            <>
              {" "}· provenance
              <span className="prov">git {run.git_sha.slice(0, 7)}</span>
              <span className="prov">config {run.config_hash.slice(0, 8)}</span>
              <span className="prov">weights {run.weight_version}</span>
            </>
          ) : null}
        </div>
      </footer>
      </div>
    </div>
  );
}
