"use client";

import { motion } from "framer-motion";
import { useCallback, useEffect, useState } from "react";
import Sidebar from "@/components/Sidebar";
import { ActorSpend, AuditPage, compact, fetchAudit, money } from "@/lib/audit";
import { usePublishScreen } from "@/lib/screen-context";

const KINDS = ["agent", "command", "tool", "system"] as const;

function relative(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/* One person's row. The bar is their share of everything spent, which is the
   comparison actually being made — two numbers side by side make you do the
   division yourself. */
function Person({ actor, share }: { actor: ActorSpend; share: number }) {
  const pct = share > 0 ? (actor.cost_usd / share) * 100 : 0;
  return (
    <div className="who-row">
      <div className="who-head">
        <span className="who-name">
          {actor.actor}
          <em>{actor.actor_kind}</em>
        </span>
        <span className="who-cost">{money(actor.cost_usd)}</span>
      </div>
      <div className="who-bar">
        <motion.i
          initial={{ width: 0 }}
          animate={{ width: `${Math.max(1.5, pct)}%` }}
          transition={{ duration: 0.6, ease: [0, 0, 0.2, 1] }}
        />
      </div>
      <div className="who-meta">
        {pct.toFixed(0)}% of all spend · {actor.events} paid {actor.events === 1 ? "event" : "events"} ·{" "}
        {compact(actor.tokens)} tokens · {money(actor.cost_24h_usd)} today · last {relative(actor.last_seen)}
      </div>
    </div>
  );
}

export default function Audit() {
  const [data, setData] = useState<AuditPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState("");
  const [operation, setOperation] = useState("");
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    try {
      setData(await fetchAudit({ kind, operation, page }));
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "failed");
    }
  }, [kind, operation, page]);

  useEffect(() => {
    load();
  }, [load]);

  // Changing a filter must reset to the first page: staying on page 4 of a
  // narrower result set shows an empty table and reads as a broken filter.
  const changeKind = (value: string) => {
    setKind(value);
    setOperation("");
    setPage(1);
  };

  const operations = (data?.operations ?? []).filter((o) => !kind || o.kind === kind);

  // Real figures, unlike the Overview table, so not flagged illustrative.
  usePublishScreen(
    "Audit",
    data
      ? `${data.total} recorded events, page ${data.page} of ${data.pages}` +
        `${kind ? `, filtered to ${kind}` : ""}${operation ? ` / ${operation}` : ""}` +
        `, total spend ${money(data.spend.total_cost_usd)} over ${compact(data.spend.total_tokens)} tokens`
      : "loading the audit trail"
  );

  return (
    <div className="shell">
      <Sidebar active="Audit" />
      <div className="content">
      <div className="wrap">
        <motion.header
          className="hero"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: [0, 0, 0.2, 1] }}
        >
          <h1>Audit</h1>
          <p>
            Every command, agent reply and tool call, newest first, with what it
            cost. Spend is what the provider actually billed, not an estimate.
          </p>
        </motion.header>

        {error === "unauthorised" ? (
          <section className="card" style={{ padding: "calc(var(--sp) * 8)", textAlign: "center" }}>
            <p style={{ color: "var(--ink-muted)" }}>
              This page needs a session. <a href="/auth/login" style={{ color: "var(--copper)" }}>Sign in with GitHub</a>.
            </p>
          </section>
        ) : (
          <>
            <div className="tiles">
              {[
                { k: "Total spend", v: money(data?.spend.total_cost_usd ?? 0), s: `${compact(data?.spend.total_tokens ?? 0)} tokens all time` },
                { k: "Last 24 hours", v: money(data?.spend.cost_24h_usd ?? 0), s: `${compact(data?.spend.tokens_24h ?? 0)} tokens · ${data?.spend.events_24h ?? 0} events`, alerted: true },
                { k: "Events recorded", v: String(data?.spend.events ?? 0), s: `${data?.pages ?? 1} page${(data?.pages ?? 1) === 1 ? "" : "s"} of ${data?.page_size ?? 50}` },
                { k: "Average per event", v: money((data?.spend.events ?? 0) > 0 ? (data!.spend.total_cost_usd / data!.spend.events) : 0), s: "billed, not estimated" },
              ].map((t, i) => (
                <motion.div
                  key={t.k}
                  className={`tile${t.alerted ? " alerted" : ""}`}
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.05 + i * 0.06, duration: 0.45, ease: [0, 0, 0.2, 1] }}
                >
                  <div className="k">{t.k}</div>
                  <div className="v">{t.v}</div>
                  <div className="s">{t.s}</div>
                </motion.div>
              ))}
            </div>

            {(data?.actors?.length ?? 0) > 0 && (
              <motion.section
                className="card who"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.3, duration: 0.45, ease: [0, 0, 0.2, 1] }}
              >
                <h2>Spend by person</h2>
                <p className="who-lede">
                  Dearest first, billed rather than estimated. Someone who has used
                  both the dashboard and Discord appears once for each, because
                  joining the two identities needs a mapping this page does not have.
                </p>
                <div className="who-rows">
                  {data!.actors.map((a) => (
                    <Person key={`${a.actor_kind}:${a.actor}`} actor={a}
                            share={data!.spend.total_cost_usd} />
                  ))}
                </div>
              </motion.section>
            )}

            <div className="filters">
              <label>Type</label>
              <select value={kind} onChange={(e) => changeKind(e.target.value)}>
                <option value="">All types</option>
                {KINDS.map((k) => (
                  <option key={k} value={k}>{k}</option>
                ))}
              </select>

              <label>Operation</label>
              <select
                value={operation}
                onChange={(e) => { setOperation(e.target.value); setPage(1); }}
                disabled={operations.length === 0}
              >
                <option value="">All operations</option>
                {operations.map((o) => (
                  <option key={`${o.kind}.${o.operation}`} value={o.operation}>
                    {o.operation} ({o.count})
                  </option>
                ))}
              </select>

              <span className="range">
                {data ? `${data.total} event${data.total === 1 ? "" : "s"}` : "loading"}
              </span>
            </div>

            <section className="card" style={{ marginBottom: "calc(var(--sp) * 10)" }}>
              <h2>Events</h2>
              <div className="sub">
                Newest first. Hover a row to see the detail recorded with it.
              </div>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>When</th><th>Type</th><th>Operation</th><th>Actor</th>
                      <th>Where</th><th>Outcome</th><th>Model</th>
                      <th className="num">Tokens</th><th className="num">Cost</th><th className="num">Took</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(data?.events ?? []).map((e, i) => (
                      <motion.tr
                        key={e.id}
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        transition={{ delay: Math.min(i * 0.012, 0.4), duration: 0.3 }}
                        title={JSON.stringify(e.detail)}
                      >
                        <td className="sector" title={e.occurred_at}>{relative(e.occurred_at)}</td>
                        <td><span className={`kind kind-${e.kind}`}>{e.kind}</span></td>
                        <td className="tick">{e.operation}</td>
                        <td className="sector">{e.actor_kind === "system" ? "system" : e.actor}</td>
                        <td className="sector">
                          {typeof e.detail?.surface === "string" ? String(e.detail.surface) : "—"}
                        </td>
                        <td>
                          <span className={`outcome outcome-${e.outcome}`}>{e.outcome}</span>
                        </td>
                        <td className="sector">{e.model ? e.model.split("/").pop() : "—"}</td>
                        <td className="num score">{e.tokens || "—"}</td>
                        <td className="num score">{e.cost_usd ? money(e.cost_usd) : "—"}</td>
                        <td className="num sector">{e.duration_ms != null ? `${e.duration_ms}ms` : "—"}</td>
                      </motion.tr>
                    ))}
                    {data && data.events.length === 0 && (
                      <tr>
                        <td colSpan={10} style={{ textAlign: "center", color: "var(--ink-muted)", padding: "calc(var(--sp) * 10)" }}>
                          Nothing matches those filters yet.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {data && data.pages > 1 && (
                <div className="pager">
                  <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}>
                    Previous
                  </button>
                  <span>Page {data.page} of {data.pages}</span>
                  <button onClick={() => setPage((p) => Math.min(data.pages, p + 1))} disabled={page >= data.pages}>
                    Next
                  </button>
                </div>
              )}
            </section>
          </>
        )}
      </div>
      </div>
    </div>
  );
}
