"use client";

import { motion } from "framer-motion";

/* How the resolver decides, as one bar.
 *
 * The counts were chips before this, and chips are a list rather than a shape:
 * "resolved 339, none 291" reads as two facts, while the same two as widths
 * reads as the thing that actually matters here — **most of what gets
 * shortlisted turns out to be about nothing**, and that is the layer working
 * rather than failing. A regex that matched only real tickers would not need a
 * model behind it.
 *
 * Ordered by the decision rather than by size: resolved, then the three ways of
 * declining to link, then the failures. A bar sorted by count would reshuffle
 * itself as the corpus changed and stop being comparable with yesterday.
 *
 * Each segment filters the list below it, so the shape and the evidence are the
 * same control. */

export const STATE_ORDER = ["resolved", "none", "unsure", "crowded", "failed"] as const;

export type StateName = (typeof STATE_ORDER)[number];

const COLOUR: Record<StateName, string> = {
  resolved: "var(--copper)",
  none: "var(--line)",
  unsure: "var(--amber-2)",
  crowded: "var(--amber-4)",
  failed: "var(--blue)",
};

const WHAT: Record<StateName, string> = {
  resolved: "linked to a security",
  none: "about none of them",
  unsure: "below the confidence floor",
  crowded: "too many tickers to be about one",
  failed: "the decision could not be made",
};

export default function DecisionFlow({
  counts,
  active,
  onPick,
}: {
  counts: Record<string, number>;
  active: string | null;
  onPick: (state: string | null) => void;
}) {
  const values = STATE_ORDER.map((s) => ({ state: s, n: counts[s] ?? 0 }));
  const total = values.reduce((sum, v) => sum + v.n, 0);

  if (total === 0) return <p className="rup-empty">Nothing decided yet.</p>;

  const share = (n: number) => (n / total) * 100;

  return (
    <div className="flow">
      <div className="flow-bar" role="img"
           aria-label={values.map((v) => `${v.state} ${v.n}`).join(", ")}>
        {values
          .filter((v) => v.n > 0)
          .map((v, i) => (
            <motion.button
              key={v.state}
              className={`flow-seg${active === v.state ? " on" : ""}${active && active !== v.state ? " off" : ""}`}
              style={{ background: COLOUR[v.state] }}
              // Width animates from nothing so the shape draws itself rather
              // than appearing; the delay walks left to right.
              initial={{ width: 0 }}
              animate={{ width: `${share(v.n)}%` }}
              transition={{ delay: 0.08 + i * 0.06, duration: 0.5, ease: [0, 0, 0.2, 1] }}
              onClick={() => onPick(active === v.state ? null : v.state)}
              title={`${v.state} — ${WHAT[v.state]} (${v.n.toLocaleString()}, ${share(v.n).toFixed(0)}%)`}
              aria-pressed={active === v.state}
            />
          ))}
      </div>

      <div className="flow-keys">
        {values.map((v) => (
          <button
            key={v.state}
            className={`flow-key${active === v.state ? " on" : ""}`}
            onClick={() => onPick(active === v.state ? null : v.state)}
            title={WHAT[v.state]}
          >
            <span className="flow-dot" style={{ background: COLOUR[v.state] }} />
            <span className="flow-name">{v.state}</span>
            <span className="flow-n">{v.n.toLocaleString()}</span>
            <span className="flow-pct">{share(v.n).toFixed(0)}%</span>
          </button>
        ))}
        <button
          className={`flow-key${active === null ? " on" : ""}`}
          onClick={() => onPick(null)}
        >
          <span className="flow-name">everything</span>
          <span className="flow-n">{total.toLocaleString()}</span>
        </button>
      </div>
    </div>
  );
}
