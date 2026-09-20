"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import Confirm from "@/components/Confirm";
import { useCallback, useEffect, useState } from "react";
import DecisionFlow from "@/components/DecisionFlow";
import RupertActivity from "@/components/RupertActivity";
import ScoreChart from "@/components/ScoreChart";
import Sidebar from "@/components/Sidebar";
import { usePublishScreen } from "@/lib/screen-context";
import {
  MEANING,
  type Rupert,
  type RupertDecision,
  type RupertNarrative,
  TONE_WORD,
  fetchNarrative,
  fetchRupert,
  setPaused,
  until,
  usd,
  when,
} from "@/lib/rupert";

const EASE = [0, 0, 0.2, 1] as const;

/* Which security a text is about, and how it reads.
 *
 * The page is arranged around one claim: **the review list at the bottom is the
 * point**, and everything above it is context for reading it. A wrong link is
 * this layer's failure mode, no aggregate will ever show you one, and the only
 * way to catch "ALL" being read as Allstate in "I put it ALL on calls" is to
 * read the sentence next to the decision. So the tiles and the chart are how
 * you decide whether to worry, and the list is where you find out. */

function badge(state: string): string {
  if (state === "resolved") return "on";
  if (state === "unsure") return "maybe";
  if (state === "failed") return "bad";
  return "no";
}

function Decision({ d }: { d: RupertDecision }) {
  return (
    /* The whole row is a link. Every figure on it — the confidence, the claim
       kind, the tone — is the *output* of something, and the page behind this
       is where the inputs are: the exact questions asked, every probability
       returned, and what FinBERT was handed. */
    <Link className="rup-row" href={`/rupert/decision/${d.id}`}>
      <span className={`rup-badge ${badge(d.state)}`}>
        {d.state === "resolved" ? d.symbol ?? d.chosen : d.state}
      </span>
      <div className="rup-row-body">
        <p className="rup-text">{d.excerpt}</p>
        <div className="rup-meta">
          {d.candidates.length > 0 && (
            <span title="What the regex shortlisted, before anything chose">
              from {d.candidates.join(", ")}
            </span>
          )}
          {d.confidence !== null && <span>{(d.confidence * 100).toFixed(0)}% sure</span>}
          {d.claim_kind && <span>{d.claim_kind}</span>}
          {d.tone !== null && (
            <span className={d.tone > 0 ? "rup-pos" : d.tone < 0 ? "rup-neg" : undefined}>
              {TONE_WORD(d.tone)} {d.tone > 0 ? "+" : ""}{d.tone.toFixed(2)}
            </span>
          )}
          {/* Only when it actually fired. A 0.01 injection score on every row
              is noise that trains you to stop reading the column. */}
          {d.injection !== null && d.injection > 0.5 && (
            <span className="rup-neg">tried to steer the reader</span>
          )}
          {d.position_talk !== null && d.position_talk > 0.6 && <span>a position, not a claim</span>}
          {d.subreddit && <span>r/{d.subreddit}</span>}
          <span>{when(d.at)}</span>
          <span className="rup-row-more">everything that went into this →</span>
        </div>
      </div>
    </Link>
  );
}

export default function RupertPage() {
  const [data, setData] = useState<Rupert | null>(null);
  const [state, setState] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [security, setSecurity] = useState<number | null>(null);
  const [story, setStory] = useState<RupertNarrative | null>(null);
  const [storyFor, setStoryFor] = useState<number | null>(null);
  const [storyBusy, setStoryBusy] = useState(false);
  const [storyError, setStoryError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await fetchRupert(state ?? undefined, page, security ?? undefined));
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "could not load");
    }
  }, [state, page, security]);

  /* Back to the first page whenever the filter changes. Staying on page 6 of
     "everything" while switching to `failed` — which has seven rows — lands on
     an empty page that reads as "there are none" rather than "you are past the
     end". */
  const pick = useCallback((next: string | null) => {
    setState(next);
    setPage(1);
  }, []);

  /* Clicking a security does two things at once: it points the chart at it and
     it asks what the discussion was about. Both are answers to the same
     question — "what is going on with this one" — and making the narrative a
     second, separate click would mean the chart and the paragraph could end up
     describing different companies. */
  const open = useCallback(async (id: number) => {
    setSecurity(id);
    if (storyFor === id) return;
    setStoryFor(id);
    setStory(null);
    setStoryError(null);
    setStoryBusy(true);
    try {
      setStory(await fetchNarrative(id));
    } catch (exc) {
      setStoryError(exc instanceof Error ? exc.message : "could not summarise");
    } finally {
      setStoryBusy(false);
    }
  }, [storyFor]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- starts a load on mount; predates lint in CI, and new code must pass the rule
    void load();
  }, [load]);

  usePublishScreen(
    "Rupert",
    data
      ? `${(data.counts.resolved ?? 0).toLocaleString()} mentions linked to a security, ` +
        `${data.coverage.scoreable} securities with enough to score, ` +
        `${usd(data.spend.cost_window)} over 30 days`
      : "loading"
  );

  const counts = data?.counts ?? {};
  const spend = data?.spend;
  const coverage = data?.coverage;

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
            <div className="rup-title">
              <h1>Rupert</h1>
              {/* Next to the title rather than in the sidebar: it explains this
                  page, and a help link somewhere else is one you find after you
                  have stopped needing it. */}
              <Link className="rup-help" href="/rupert/how" title="How Rupert works">
                <span aria-hidden="true">?</span>
                <span className="rup-help-label">How it works</span>
              </Link>
            </div>
            <p>
              Which security a text is about, and how it reads. A regex
              shortlists candidate symbols and refuses to choose between them; a
              decision model that has the sentence in front of it picks which one
              — if any — the text is actually about; FinBERT reads what resolved.
              The decisions land in <code>rupert.mention</code> and the readings
              in <code>rupert.reading</code>, both queryable on{" "}
              <a href="/playground">the playground</a>.{" "}
              <b>Nothing scores anything with this yet.</b>
            </p>
          </motion.header>

          {/* Three states, not two. `paused` is the operator's switch and
              `enabled` is the deployment's, and a card that read only the
              first said "Running" while the container was exiting on boot
              because RUPERT_DAILY_MAX_CALLS was 0. Off wins: there is nothing
              to pause, so the button is disabled rather than offering to stop
              something that is not started. */}
          {data && (
            <section className={`card rup-sched${data.enabled ? "" : " off"}`}>
              <button
                className={`rup-play${data.paused ? " paused" : ""}`}
                onClick={() => setAsking(true)}
                disabled={pausing || !data.enabled}
                title={
                  !data.enabled
                    ? "Nothing to pause: the budget is zero, so no pass runs"
                    : data.paused
                      ? "Let it run again"
                      : "Stop the nightly passes"
                }
                aria-label={data.paused ? "Resume Rupert" : "Pause Rupert"}
              >
                {data.paused || !data.enabled ? "▶" : "❚❚"}
              </button>
              <div className="rup-sched-words">
                <p className="rup-sched-state">
                  {!data.enabled ? "Off" : data.paused ? "Paused" : "Running"}
                  <small>
                    {" · every "}
                    {data.refresh_hours}h
                    {data.enabled && data.paused && data.paused_by
                      ? ` · by ${data.paused_by}`
                      : ""}
                  </small>
                </p>
                {/* Definite in all three states. "Runs in 4h" and "would run in
                    4h" are both statements; a hedge here is what sends somebody
                    to the logs to find out what is actually going to happen. */}
                <p className="rup-sched-next">
                  {data.enabled ? (
                    until(data.next_run_at, data.paused)
                  ) : (
                    <>
                      No pass will run. <code>RUPERT_DAILY_MAX_CALLS</code> is 0,
                      which is the budget and the switch at once.
                    </>
                  )}
                </p>
              </div>
            </section>
          )}

          <Confirm
            open={asking}
            title={data?.paused ? "Let Rupert run again?" : "Pause Rupert?"}
            body={
              data?.paused
                ? "The next scheduled wake will resolve the backlog that built up while it was paused, within the usual daily caps."
                : "Nightly passes stop until you start them again. Nothing already decided is lost, the corpus keeps being ingested, and the backlog is picked up when you resume."
            }
            confirmLabel={data?.paused ? "Let it run" : "Pause it"}
            onCancel={() => setAsking(false)}
            onConfirm={async () => {
              setAsking(false);
              setPausing(true);
              try {
                await setPaused(!data?.paused);
                await load();
              } catch (exc) {
                setError(exc instanceof Error ? exc.message : "could not change that");
              } finally {
                setPausing(false);
              }
            }}
          />

          {error === "unauthorised" && (
            <p className="rup-note">
              This page needs a session. <a href="/auth/login">Sign in</a>.
            </p>
          )}
          {error && error !== "unauthorised" && <p className="rup-note">{error}</p>}

          {data && !data.enabled && (
            <section className="card rup-off">
              <h2>Switched off</h2>
              <p>
                <code>RUPERT_DAILY_MAX_CALLS</code> is 0, so the container exits
                rather than resolving anything. It is the only thing here that
                spends money per item rather than per question from a person, so
                it ships off and the budget is also the switch. Setting it needs
                the container recreated, not restarted.{" "}
                {(spend?.decisions_total ?? 0) > 0
                  ? "Everything below is what it decided while it was last running."
                  : "It has not decided anything yet, so everything below is empty rather than quiet."}
              </p>
            </section>
          )}

          <div className="tiles">
            {[
              {
                k: "Linked to a security",
                v: (counts.resolved ?? 0).toLocaleString(),
                s: `of ${(spend?.decisions_total ?? 0).toLocaleString()} decisions asked for`,
              },
              {
                k: "Read for tone",
                v: (counts.read ?? 0).toLocaleString(),
                s: "FinBERT, on what resolved — three probabilities, never one number",
              },
              {
                k: "Securities worth scoring",
                v: coverage ? `${coverage.scoreable}` : "—",
                s: coverage
                  ? `${coverage.floor}+ mentions in ${coverage.days} days, of ${coverage.mentioned} mentioned and ${coverage.active.toLocaleString()} active`
                  : "",
              },
              {
                k: "Spent over 30 days",
                v: spend ? usd(spend.cost_window) : "—",
                s: spend
                  ? `${usd(spend.cost_today)} today · ${spend.calls_today}/${data?.daily_max_calls ?? 0} calls`
                  : "",
              },
            ].map((t, i) => (
              <motion.div
                key={t.k}
                className="tile"
                initial={{ opacity: 0, y: 14 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.1 + i * 0.07, duration: 0.5, ease: EASE }}
              >
                <div className="k">{t.k}</div>
                <div className="v">{t.v}</div>
                <div className="s">{t.s}</div>
              </motion.div>
            ))}
          </div>

          <section className="card rup-card">
            <div className="rup-head">
              <h2>What it scored</h2>
              {/* The obvious control. The leaderboard rows below also select,
                  which is a nice shortcut once you know it and a hidden feature
                  until you do — a chart titled with one security and no visible
                  way to change it reads as the only one it can draw. */}
              {(data?.standings ?? []).length > 0 && (
                <select
                  className="rup-pick"
                  value={data?.scored_security?.security_id ?? ""}
                  onChange={(e) => void open(Number(e.target.value))}
                  aria-label="Which security to chart"
                >
                  {data?.standings.map((s) => (
                    <option key={s.security_id} value={s.security_id}>
                      {s.symbol} — {s.mentions.toLocaleString()} mentions
                    </option>
                  ))}
                </select>
              )}
            </div>
            <p className="sub">
              {data?.scored_security ? (
                <>
                  Tone for <b>{data.scored_security.name}</b>, day by day —
                  FinBERT&rsquo;s <code>positive − negative</code>, trimmed so one
                  viral post cannot decide it. The axis is fixed at −1 to +1 and
                  never rescales, because a quiet week and a wild one must not
                  look alike. Bars are mentions that day. A break in the line is a
                  day whose trailing {data.rolling_days} days did not reach{" "}
                  {coverage?.floor ?? 10} mentions — a gap, deliberately not a
                  zero, because zero is what a genuinely balanced week looks like.
                  Pick another security in the table below.
                </>
              ) : (
                "Nothing has resolved yet, so there is nothing to score."
              )}
            </p>
            {data && (
              <ScoreChart series={data.scored} rollingDays={data.rolling_days} />
            )}

          </section>

          <section className="card rup-card">
            <h2>Work and cost, by day</h2>
            <p className="sub">
              Bars are decisions and the filled part is what linked to a
              security; the line is what the day cost. Keyed on when it was
              decided rather than when the comment was written, so draining a
              backlog reads as a busy day for Rupert rather than a busy day on
              Reddit.
            </p>
            {data && <RupertActivity days={data.daily} />}
            <p className="rup-foot">
              Pick a security in <b>Most talked about</b> below to chart it and
              read what the discussion was about.
            </p>
            {spend?.per_decision != null && (
              <p className="rup-foot">
                {usd(spend.per_decision)} a decision on average. The unit price
                is fixed, so this only moves when the questions or the text sent
                with them get longer.
              </p>
            )}
          </section>

          <div className="rup-cols">
            <section className="card rup-card">
              <h2>Most talked about</h2>
              <p className="sub">
                Over {coverage?.days ?? 7} days. Tone is the trimmed mean of what
                FinBERT read, so one viral post cannot decide it — and a security
                under {coverage?.floor ?? 10} mentions shows its count and no
                tone, because a count is not a reading. Attention is volume
                against that security&rsquo;s own recent normal, not the
                market&rsquo;s.
              </p>
              {(data?.standings ?? []).length === 0 && (
                <p className="rup-empty">Nothing resolved in this window.</p>
              )}
              {(data?.standings ?? []).map((s, i) => (
                <div key={s.security_id}>
                <motion.button
                  className={`rup-stand${
                    data?.scored_security?.security_id === s.security_id ? " on" : ""
                  }`}
                  onClick={() => void open(s.security_id)}
                  title={`Chart ${s.symbol} and summarise what was said about it`}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: Math.min(i, 8) * 0.03, duration: 0.3, ease: EASE }}
                >
                  <span className="rup-sym">{s.symbol}</span>
                  <span className="rup-name">{s.name}</span>
                  <span className="rup-count">{s.mentions.toLocaleString()}</span>
                  <span
                    className={
                      s.tone === null
                        ? "rup-tone thin"
                        : s.tone > 0.15
                          ? "rup-tone rup-pos"
                          : s.tone < -0.15
                            ? "rup-tone rup-neg"
                            : "rup-tone"
                    }
                    title={
                      s.tone === null
                        ? `under ${coverage?.floor ?? 10} mentions — no reading`
                        : `${s.read} read, ${s.trimmed ?? 0} trimmed`
                    }
                  >
                    {s.tone === null ? "—" : `${s.tone > 0 ? "+" : ""}${s.tone.toFixed(2)}`}
                  </span>
                  <span className="rup-attn" title="against its own 30-day baseline">
                    {s.attention === null
                      ? "—"
                      : `${s.attention > 0 ? "+" : ""}${s.attention.toFixed(1)}σ`}
                  </span>
                </motion.button>
                {/* Directly under the row that was clicked, not in the card
                    above. The panel used to live beside the chart, which put
                    the answer seven hundred pixels off the top of the screen
                    from where you pressed — so clicking a security looked like
                    it did nothing at all. */}
                {storyFor === s.security_id && (
                  <div className="rup-story">
                    {storyBusy && (
                      <p className="rup-story-wait">Reading what was said…</p>
                    )}
                    {storyError && <p className="rup-story-wait">{storyError}</p>}
                    {!storyBusy && !storyError && story && (
                      story.text ? (
                        <>
                          <p className="rup-story-text">{story.text}</p>
                          <p className="rup-story-foot">
                            from {story.mentions_used} comments over{" "}
                            {story.window_days} days
                            {story.cached ? " · written earlier today" : ""} · a
                            summary of what was said, not a finding and not
                            advice. Every number on this page is
                            FinBERT&rsquo;s or arithmetic; the model writing
                            this is shown neither.
                          </p>
                        </>
                      ) : (
                        <p className="rup-story-wait">
                          Nothing resolved to {story.symbol} in the last{" "}
                          {story.window_days} days, so there is nothing to
                          summarise.
                        </p>
                      )
                    )}
                  </div>
                )}
              </div>
              ))}
            </section>

            <section className="card rup-card">
              <h2>Where it has read to</h2>
              <p className="sub">
                The frontier is a position on the timeline, not a set of stored
                rows: most of the corpus mentions nothing and writes no decision,
                so &ldquo;read it, found nothing&rdquo; has to be different from
                &ldquo;never read it&rdquo;.
              </p>
              {(data?.frontiers ?? []).length === 0 && (
                <p className="rup-empty">Nothing read yet.</p>
              )}
              {(data?.frontiers ?? []).map((f) => (
                <div key={f.corpus} className="rup-front">
                  <span className="rup-sym">{f.corpus}</span>
                  <span className="rup-name">
                    up to {new Date(f.read_through).toLocaleString("en-GB", {
                      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
                    })}
                  </span>
                  <span className="rup-count">{f.items_read.toLocaleString()} examined</span>
                </div>
              ))}

              <h2 className="rup-h2-again">Recent passes</h2>
              {(data?.passes ?? []).length === 0 && (
                <p className="rup-empty">No passes recorded.</p>
              )}
              {(data?.passes ?? []).map((p, i) => (
                <div key={`${p.started_at}-${i}`} className="rup-pass">
                  <span className={`rup-badge ${p.status === "ok" ? "on" : p.status === "partial" ? "maybe" : "bad"}`}>
                    {p.status}
                  </span>
                  <span className="rup-name">{p.endpoint}</span>
                  <span className="rup-count">
                    {p.resolved ?? 0}/{p.shortlisted ?? 0}
                  </span>
                  <span className="rup-when" title={p.error ?? undefined}>
                    {when(p.started_at)}
                  </span>
                </div>
              ))}
            </section>
          </div>

          <section className="card rup-card">
            <h2>What it decided</h2>
            <p className="sub">
              <b>This list is the point of the page.</b> A wrong link is this
              layer&rsquo;s failure mode and no total will ever show you one —
              the only way to catch <code>ALL</code> being read as Allstate in
              &ldquo;I put it ALL on calls&rdquo; is to read the sentence beside
              the decision. The refusals are here for the same reason Magpie
              lists the pages it would not fetch.
            </p>
            <DecisionFlow counts={counts} active={state} onPick={pick} />
            {state && <p className="rup-hint">{MEANING[state]}</p>}
            {(data?.review ?? []).length === 0 && (
              <p className="rup-empty">Nothing here.</p>
            )}
            {(data?.review ?? []).map((d) => (
              <Decision key={d.id} d={d} />
            ))}
            {data && data.review_pages > 1 && (
              <div className="pager">
                <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}>
                  Previous
                </button>
                <span>
                  Page {data.review_page} of {data.review_pages}
                  <small> · {data.review_total.toLocaleString()} decisions</small>
                </span>
                <button
                  onClick={() => setPage((p) => Math.min(data.review_pages, p + 1))}
                  disabled={page >= data.review_pages}
                >
                  Next
                </button>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
