"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import { use, useEffect, useState } from "react";
import Sidebar from "@/components/Sidebar";
import { type RupertFull, fetchDecision } from "@/lib/rupert";

const EASE = [0, 0, 0.2, 1] as const;

/* One decision, and everything that produced it.
 *
 * The review list shows outputs — a symbol, a confidence, a tone. This is where
 * the inputs are: the comment as it was sent, the shortlist the regex handed
 * over, the exact question set the model was asked, every probability it
 * returned, and the three numbers FinBERT gave back. `CLAUDE.md` asks that every
 * score trace to visible raw inputs; for a decision that is not a score, this is
 * the same promise kept.
 *
 * **The request is reconstructed, not stored**, and the page says so. The
 * question modules are pure, so the questions for a given shortlist rebuild
 * exactly rather than being kept as a second copy that could drift from what is
 * actually sent. The cost of that is real and is stated: rebuilding uses
 * *today's* question set, so a decision taken under an older Rupert is marked
 * rather than silently redrawn. */

function Bar({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="dec-bar">
      <span className="dec-bar-label">{label}</span>
      <span className="dec-bar-track">
        <span
          className={`dec-bar-fill${tone ? ` ${tone}` : ""}`}
          style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }}
        />
      </span>
      <span className="dec-bar-value">{value.toFixed(3)}</span>
    </div>
  );
}

export default function DecisionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [d, setD] = useState<RupertFull | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setD(await fetchDecision(Number(id)));
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : "could not load");
      }
    })();
  }, [id]);

  return (
    <div className="shell">
      <Sidebar active="Rupert" />
      <div className="content">
        <div className="wrap">
          <motion.header
            className="hero"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: EASE }}
          >
            <Link className="rup-back" href="/rupert">← Rupert</Link>
            <div className="rup-title">
              <h1>One decision</h1>
              {d && (
                <span
                  className={`how-version${d.version_is_current ? "" : " stale"}`}
                  title={
                    d.version_is_current
                      ? "Decided by the Rupert that is running now"
                      : `Decided by ${d.rupert_version}; ${d.current_version} is running now, so the questions below are today's rather than the ones actually asked`
                  }
                >
                  {d.rupert_version}
                  {!d.version_is_current && <small>not current</small>}
                </span>
              )}
            </div>
            <p>
              Everything that went into this and everything that came out —
              the text as it was sent, the shortlist, the exact questions asked,
              every probability returned, and what FinBERT read.
            </p>
          </motion.header>

          {error && <p className="rup-note">{error}</p>}

          {d && (
            <>
              <section className="card rup-card">
                <h2>The text</h2>
                <div className="dec-body">
                  {d.source.title && <p className="dec-title">{d.source.title}</p>}
                  <p className="dec-text">{d.source.body}</p>
                  <p className="dec-meta">
                    {d.source.subreddit ? `r/${d.source.subreddit}` : d.source.corpus}
                    {d.source.author ? ` · ${d.source.author}` : ""}
                    {d.source.created_utc
                      ? ` · ${new Date(d.source.created_utc).toLocaleString("en-GB")}`
                      : ""}
                    {d.source.permalink && (
                      <>
                        {" · "}
                        <a href={`https://reddit.com${d.source.permalink}`} rel="noreferrer">
                          on Reddit
                        </a>
                      </>
                    )}
                  </p>
                  {d.shortlist.sent_text !== d.source.body && (
                    <p className="dec-note">
                      What was sent was trimmed to {d.shortlist.sent_text.length}{" "}
                      characters. The model saw the text above only as far as that.
                    </p>
                  )}
                </div>
              </section>

              <section className="card rup-card">
                <h2>1 · The regex shortlisted</h2>
                <p className="sub">
                  Free, deterministic, and it refuses to choose. This is the whole
                  of what the model was offered.
                </p>
                <div className="dec-body">
                  {d.shortlist.candidates.length === 0 ? (
                    <p className="dec-text">Nothing — so nothing was asked and nothing was paid.</p>
                  ) : (
                    <ul className="dec-cands">
                      {d.shortlist.candidates.map((c) => (
                        <li key={c}>
                          <b>{c}</b> {d.shortlist.names[c] ?? "not a current symbol"}
                        </li>
                      ))}
                      <li className="dec-none">
                        <b>none</b> the option that makes the rest honest
                      </li>
                    </ul>
                  )}
                </div>
              </section>

              {d.jev.asked && (
                <section className="card rup-card">
                  <h2>2 · What {d.jev.model} was asked</h2>
                  <p className="sub">
                    Reconstructed from the shortlist rather than stored — the
                    question modules are pure, so this rebuilds exactly instead of
                    being a second copy that could drift from what is sent.
                    {!d.version_is_current &&
                      " Rebuilt under today's question set, which is not the one this decision was taken under."}
                  </p>
                  <div className="dec-body">
                    <p className="dec-label">state</p>
                    <pre className="dec-json">{JSON.stringify(d.jev.asked.state, null, 2)}</pre>
                    <p className="dec-label">questions</p>
                    <pre className="dec-json">
                      {JSON.stringify(d.jev.asked.questions, null, 2)}
                    </pre>
                  </div>
                </section>
              )}

              <section className="card rup-card">
                <h2>3 · What it answered</h2>
                <p className="sub">
                  The distribution, not just the winner. An independent test
                  measured this model overconfident on <code>choice</code> out of
                  distribution, so the confidence is a reading to threshold rather
                  than a fact to trust — which is why the whole spread is kept.
                </p>
                <div className="dec-body">
                  {d.jev.probabilities && (
                    <>
                      <p className="dec-label">which company · choice</p>
                      {Object.entries(d.jev.probabilities)
                        .sort((a, b) => b[1] - a[1])
                        .map(([k, v]) => (
                          <Bar
                            key={k}
                            label={k}
                            value={v}
                            tone={k === d.jev.chosen ? "won" : undefined}
                          />
                        ))}
                      <p className="dec-note">
                        confidence {d.jev.confidence?.toFixed(3)} — chose{" "}
                        <b>{d.jev.chosen}</b>, and the row was recorded as{" "}
                        <b>{d.state}</b>.
                      </p>
                    </>
                  )}
                  <p className="dec-label">the three gates · noul</p>
                  {d.jev.own_business !== null && (
                    <Bar label="about its own business" value={d.jev.own_business} />
                  )}
                  {d.jev.position_talk !== null && (
                    <Bar label="a position, not a claim" value={d.jev.position_talk} />
                  )}
                  {d.jev.injection !== null && (
                    <Bar
                      label="trying to steer the reader"
                      value={d.jev.injection}
                      tone={d.jev.injection > 0.5 ? "bad" : undefined}
                    />
                  )}
                  {d.jev.claim_kind && (
                    <p className="dec-note">
                      claim kind <b>{d.jev.claim_kind}</b> at{" "}
                      {d.jev.claim_confidence?.toFixed(3)} confidence
                    </p>
                  )}
                  <p className="dec-meta">
                    {d.jev.input_tokens.toLocaleString()} input tokens · $
                    {d.jev.cost_usd.toFixed(6)} · decided{" "}
                    {new Date(d.jev.decided_at).toLocaleString("en-GB")}
                  </p>
                </div>
              </section>

              <section className="card rup-card">
                <h2>4 · What FinBERT read</h2>
                <p className="sub">
                  Tone is FinBERT&rsquo;s and only FinBERT&rsquo;s. Three
                  probabilities, never one number — &ldquo;confidently
                  neutral&rdquo; and &ldquo;torn&rdquo; both land near zero and
                  are not the same reading, so the score is derived from these
                  rather than stored beside them.
                </p>
                <div className="dec-body">
                  {/* The input, before the output. It is the same excerpt Jev
                      was given — worth saying rather than leaving a reader to
                      compare two blocks and guess. */}
                  <p className="dec-label">what it was given</p>
                  <pre className="dec-json">{d.finbert_input.text}</pre>
                  <p className="dec-note">
                    {d.finbert_input.chars.toLocaleString()} characters
                    {d.finbert_input.same_as_jev
                      ? " — the same excerpt the decision model saw"
                      : ""}
                    . Three caps sit between the comment and the tokens the model
                    actually read: the excerpt trims at{" "}
                    {d.finbert_input.excerpt_cap.toLocaleString()}, the client at{" "}
                    {d.finbert_input.client_cap.toLocaleString()}, and FinBERT
                    itself truncates at {d.finbert_input.token_cap} word-pieces —
                    it is BERT-base, so anything past that was not read rather
                    than summarised. Sent in batches of at most{" "}
                    {d.finbert_input.batch_cap}.
                  </p>
                  <p className="dec-label">what came back</p>
                  {d.finbert.length === 0 ? (
                    <p className="dec-text">
                      Nothing read this. Only resolved mentions are scored for
                      tone — scoring the rest would be minutes of the box a night
                      to produce numbers about no security.
                    </p>
                  ) : (
                    d.finbert.map((r) => (
                      <div key={r.model} className="dec-reading">
                        <Bar label="positive" value={r.positive} tone="won" />
                        <Bar label="negative" value={r.negative} tone="bad" />
                        <Bar label="neutral" value={r.neutral} />
                        <p className="dec-note">
                          score {r.score > 0 ? "+" : ""}
                          {r.score.toFixed(3)} = positive − negative, derived ·{" "}
                          {r.model} · {new Date(r.read_at).toLocaleString("en-GB")}
                        </p>
                      </div>
                    ))
                  )}
                </div>
              </section>

              {d.security && (
                <p className="how-foot">
                  Linked to <b>{d.security.symbol}</b> — {d.security.name}. Nothing
                  scores anything with this yet.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
