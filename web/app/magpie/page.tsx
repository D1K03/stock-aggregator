"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import Confirm from "@/components/Confirm";
import Ladder, { Climb } from "@/components/Ladder";
import Sidebar from "@/components/Sidebar";
import { usePublishScreen } from "@/lib/screen-context";
import {
  MagpieAttempt,
  MagpieDocument,
  MagpieListing,
  WHY,
  fetchGathered,
  forget,
  scrape,
  when,
} from "@/lib/magpie";

const EASE = [0, 0, 0.2, 1] as const;

/* Paste a link, keep the article.
 *
 * The list is what was gathered; the column beside it is every attempt,
 * including the ones that were refused — which is the half that would be
 * invisible if this only listed successes. A page the site disallows and a page
 * nobody has tried look identical without it, and that is how the same dead
 * link gets fetched again every time somebody asks. */
function badge(attempt: MagpieAttempt): string {
  if (attempt.state === "stored") return "on";
  if (attempt.state === "unchanged") return "same";
  if (attempt.state === "refused") return "no";
  if (attempt.state === "failed") return "bad";
  return "wait";
}

export default function Magpie() {
  const [listing, setListing] = useState<MagpieListing | null>(null);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [climb, setClimb] = useState<Climb | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The document the modal is asking about, or null when it is closed. Holding
  // the whole row rather than the id so the question can name it.
  const [asking, setAsking] = useState<MagpieDocument | null>(null);
  const [docsPage, setDocsPage] = useState(1);
  const [triesPage, setTriesPage] = useState(1);

  // Stable, so the ladder's reveal is not restarted by this page re-rendering.
  const clearClimb = useCallback(() => setClimb(null), []);

  const load = useCallback(async () => {
    try {
      setListing(await fetchGathered(docsPage, triesPage));
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "could not load");
    }
  }, [docsPage, triesPage]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reads browser state or starts a load on mount; predates lint in CI, and new code must pass the rule
    void load();
  }, [load]);

  const gathered = listing?.documents ?? [];
  usePublishScreen(
    "Magpie",
    gathered.length
      ? `${gathered.length} scraped documents, newest "${gathered[0].title}" from ${gathered[0].host}`
      : "nothing scraped yet"
  );

  const submit = async () => {
    const link = url.trim();
    if (!link || busy) return;
    setBusy(true);
    setNotice(null);
    const result = await scrape(link);
    if (result.ok) {
      setUrl("");
      // Deliberately no separate notice: the ladder below says what happened,
      // and a second line appearing under it repeated itself and pushed the
      // list down in one frame with nothing animating it.
      setNotice(null);
      setClimb({
        attempts: result.document.attempts ?? [],
        won: result.document.strategy,
        costUsd: result.document.cost_usd ?? 0,
        kept: `“${result.document.title}”`,
      });
    } else {
      setNotice(null);
      setClimb({
        attempts: result.attempts ?? [],
        won: null,
        costUsd: 0,
        refusedWhy: `not kept — ${WHY[result.reason] ?? result.reason}`,
      });
    }
    setBusy(false);
    // A new document lands at the top of the first page of both lists, so go
    // there rather than appending it to whichever page is being read.
    if (docsPage !== 1 || triesPage !== 1) {
      setDocsPage(1);
      setTriesPage(1);
    } else {
      void load();
    }
  };

  return (
    <div className="shell">
      <Sidebar active="Magpie" />
      <div className="content">
        <div className="wrap">
          <motion.header
            className="hero"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: EASE }}
          >
            <h1>Magpie</h1>
            <p>
              Paste a link and it fetches the article by the cheapest route that
              works, reads it out of the page and keeps it. The whole text lands in{" "}
              <code>magpie.document</code>, which you can query on{" "}
              <a href="/playground">the playground</a>; Steven can scrape and read
              the same way. A site that says no in its robots.txt is not fetched,
              and that refusal is final.
            </p>
          </motion.header>

          <section className="card mag-add">
            <input
              value={url}
              placeholder="https://…"
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              aria-label="Link to scrape"
            />
            <button onClick={submit} disabled={busy || !url.trim()}>
              {busy ? "Fetching…" : "Keep it"}
            </button>
          </section>

          <Ladder busy={busy} climb={climb} onDone={clearClimb} />
          <AnimatePresence>
            {notice && (
              <motion.p
                className="mag-note"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.24, ease: EASE }}
              >
                {notice}
              </motion.p>
            )}
          </AnimatePresence>
          {error === "unauthorised" && (
            <p className="mag-note">
              This page needs a session. <a href="/auth/login">Sign in</a>.
            </p>
          )}

          <div className="mag-cols">
            <section className="card">
              <h2>Gathered{listing ? ` · ${listing.documents_total}` : ""}</h2>
              {gathered.length === 0 && (
                <p className="mag-empty">Nothing yet. Paste a link above.</p>
              )}
              {gathered.map((document: MagpieDocument, i) => (
                <motion.article
                  key={document.id}
                  className="mag-doc"
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: Math.min(i, 8) * 0.03, duration: 0.32, ease: EASE }}
                >
                  {/* The row opens the document rather than expanding in
                      place: the sources are the reason to open one, and they
                      do not belong in a list. */}
                  <Link className="mag-doc-body" href={`/magpie/${document.id}`}>
                    <h3>{document.title || document.url}</h3>
                    <div className="mag-meta">
                      {document.host}
                      {` · ${document.word_count.toLocaleString()} words`}
                      {document.published ? ` · ${document.published}` : ""}
                      {` · via ${document.strategy} · ${when(document.fetched_at)}`}
                    </div>
                  </Link>
                  <button
                      className="mag-x"
                      aria-label={`Delete ${document.title}`}
                      title="Stop keeping this"
                      onClick={(e) => {
                        // Otherwise the row's link navigates away behind the
                        // dialog that is asking whether to delete it.
                        e.preventDefault();
                        e.stopPropagation();
                        setAsking(document);
                      }}
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                           stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                           aria-hidden="true">
                        <path d="M18 6L6 18M6 6l12 12" />
                      </svg>
                    </button>
                </motion.article>
              ))}
              {listing && listing.documents_pages > 1 && (
                <div className="pager">
                  <button
                    onClick={() => setDocsPage((p) => Math.max(1, p - 1))}
                    disabled={docsPage <= 1}
                  >
                    Previous
                  </button>
                  <span>
                    Page {listing.documents_page} of {listing.documents_pages}
                  </span>
                  <button
                    onClick={() =>
                      setDocsPage((p) => Math.min(listing.documents_pages, p + 1))
                    }
                    disabled={docsPage >= listing.documents_pages}
                  >
                    Next
                  </button>
                </div>
              )}
            </section>

            <section className="card">
              <h2>Every attempt{listing ? ` · ${listing.attempts_total}` : ""}</h2>
              <p className="mag-hint">
                Including the ones that were refused. Without them a link a site
                disallows and a link nobody has tried look the same.
              </p>
              {(listing?.attempts ?? []).length === 0 && (
                <p className="mag-empty">No attempts yet.</p>
              )}
              {(listing?.attempts ?? []).map((attempt) => (
                <div key={attempt.id} className="mag-try">
                  <span className={`mag-badge ${badge(attempt)}`}>{attempt.state}</span>
                  <span className="mag-try-host">{attempt.host || attempt.url}</span>
                  <span className="mag-try-why">
                    {attempt.reason ? WHY[attempt.reason] ?? attempt.reason : attempt.strategy}
                    {attempt.cost_usd > 0 && ` · $${attempt.cost_usd.toFixed(4)}`}
                  </span>
                  <span className="mag-try-when">{when(attempt.requested_at)}</span>
                </div>
              ))}
              {listing && listing.attempts_pages > 1 && (
                <div className="pager">
                  <button
                    onClick={() => setTriesPage((p) => Math.max(1, p - 1))}
                    disabled={triesPage <= 1}
                  >
                    Previous
                  </button>
                  <span>
                    Page {listing.attempts_page} of {listing.attempts_pages}
                  </span>
                  <button
                    onClick={() =>
                      setTriesPage((p) => Math.min(listing.attempts_pages, p + 1))
                    }
                    disabled={triesPage >= listing.attempts_pages}
                  >
                    Next
                  </button>
                </div>
              )}
            </section>
          </div>
        </div>
      </div>

      <Confirm
        open={asking !== null}
        title="Delete this document?"
        body={
          asking
            ? `“${asking.title}” — ${asking.word_count.toLocaleString()} words from ${asking.host}. The record of fetching it stays on the attempts list, so it will not look like a link nobody has tried.`
            : undefined
        }
        onCancel={() => setAsking(null)}
        onConfirm={async () => {
          const going = asking;
          setAsking(null);
          if (!going) return;
          if (await forget(going.id)) {
            setNotice(`Deleted “${going.title}”.`);
            // Deleting the only row on the last page would otherwise leave you
            // looking at an empty one with no way back but Previous.
            if (gathered.length === 1 && docsPage > 1) setDocsPage((p) => p - 1);
            else void load();
          } else {
            setNotice("That could not be deleted.");
          }
        }}
      />
    </div>
  );
}
