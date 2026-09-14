"use client";

import type { Metric, SecurityDetail } from "@/lib/screen";
import { PILLAR_NAMES, difference, metricValue, percentile, period } from "@/lib/screen";

/* "Why this score": every metric behind each pillar, with its percentile and its
 * peers, beside whether it reproduces now (ui-swap spec D4, D14).
 *
 * The status service re-runs scoring's explaining forms for this one security
 * under the run's own view, so each row is a claim that has just been checked,
 * not a stored number repeated. What a status cannot check is the percentile,
 * which depends on every peer; the footer says so, so "ok" does not claim more
 * than it means. */

const CAUSES = [
  "a currency change or sector reclassification since the run",
  "the split rule's calendar-boundary gap",
  "an ingest that overlapped the run's start",
];

function note(metric: Metric): { text: string; warn: boolean } | null {
  const { stored, reproduced } = metric;
  switch (metric.status) {
    case "mismatch":
      return {
        warn: true,
        text:
          stored && reproduced !== null
            ? `does not reproduce: stored ${stored.raw}, now ${reproduced} (${difference(stored.raw, reproduced)})`
            : `does not reproduce: now absent, ${metric.reason ?? "no reason recorded"}`,
      };
    case "absent":
      return metric.reason ? { warn: false, text: metric.reason } : null;
    case "unexpected":
      return {
        warn: true,
        text:
          reproduced === null
            ? "not scored, but reproduces now"
            : `not scored, but reproduces now: ${metricValue(reproduced, metric.unit)}`,
      };
    case "refreshed":
      return { warn: false, text: "inputs changed since this run" };
    case "unchecked":
      return { warn: false, text: "could not re-check" };
    default:
      return null;
  }
}

function MetricRow({ metric }: { metric: Metric }) {
  const { stored } = metric;
  const said = note(metric);
  return (
    <>
      <span className="why-name">
        {metric.name}
        {metric.higher_is_better ? null : <span title="lower is better"> ↓</span>}
      </span>
      <span className="why-value">{stored ? metricValue(stored.raw, metric.unit) : "—"}</span>
      <span className="why-bar" title={stored ? `${percentile(stored.percentile)}th percentile` : undefined}>
        {stored ? <i style={{ width: `${percentile(stored.percentile)}%` }} /> : null}
      </span>
      <span className="why-meta">
        {stored
          ? `${percentile(stored.percentile)} · ${stored.market_ranked ? "vs market" : stored.peer_group} (${stored.peer_count}) · ${period(stored)}`
          : null}
      </span>
      {said ? <span className={`why-note${said.warn ? " warn" : ""}`}>{said.text}</span> : null}
    </>
  );
}

export default function WhyPanel({
  detail, error, asOf,
}: {
  detail: SecurityDetail | null;
  error: string | null;
  asOf: string | null;
}) {
  if (error) {
    return (
      <section className="card">
        <h2>Why this score</h2>
        <div className="sub">{error}</div>
      </section>
    );
  }
  if (!detail) {
    return (
      <section className="card">
        <h2>Why this score</h2>
        <div className="sub">Re-checking the inputs…</div>
      </section>
    );
  }
  if (!detail.scored) {
    return (
      <section className="card">
        <h2>Why this score</h2>
        <div className="sub">
          {detail.symbol} was not scored on {asOf ?? "this night"}
          {detail.active ? "." : ", and is no longer in the universe."}
        </div>
      </section>
    );
  }

  const { reproduction } = detail;
  const flagged = detail.pillars.some((pillar) =>
    pillar.metrics.some((metric) => metric.status === "mismatch" || metric.status === "unexpected")
  );
  const rebuilt = reproduction.run_build !== reproduction.running_build;
  const causes = rebuilt
    ? [`a build difference (run ${reproduction.run_build}, now ${reproduction.running_build})`, ...CAUSES]
    : CAUSES;

  return (
    <section className="card">
      <h2>Why this score</h2>
      <div className="sub">
        {detail.symbol} · {detail.score}
        {detail.partial ? " ◐ partial" : ""} · {detail.agreement} of 3 pillars top-quartile ·{" "}
        {detail.industry?.name ?? detail.sector.name}
      </div>
      {detail.pillars.map((pillar) => (
        <div key={pillar.code}>
          <div className="why-head">
            {PILLAR_NAMES[pillar.key]} {pillar.score ?? "—"}
            <small>
              {pillar.present} of {pillar.expected} metrics
            </small>
          </div>
          <div className="why-grid">
            {pillar.metrics.map((metric) => (
              <MetricRow key={metric.code} metric={metric} />
            ))}
          </div>
        </div>
      ))}
      <div className="why-foot">
        <p>
          Each value is re-computed from the inputs the run could see. Percentiles are not
          re-derived, because they depend on every peer, so “ok” means the raw value reproduces.
        </p>
        {reproduction.refreshed_inputs.length > 0 ? (
          <p>Price bars were rewritten since this run, so the metrics that read a close are not compared.</p>
        ) : null}
        {flagged ? <p>Known causes of a value that does not reproduce: {causes.join("; ")}.</p> : null}
      </div>
    </section>
  );
}
