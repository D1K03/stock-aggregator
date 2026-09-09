"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import Ladder, { Climb } from "@/components/Ladder";
import Sidebar from "@/components/Sidebar";
import { usePublishScreen } from "@/lib/screen-context";
import {
  MagpieDetail,
  MagpieLink,
  MagpieSite,
  WHY,
  fetchDocument,
  follow,
  when,
} from "@/lib/magpie";

const EASE = [0, 0, 0.2, 1] as const;

/* Sites to a page. A well-cited article points at a hundred of them, which is a
   wall rather than a summary. Paged in the browser rather than on the server
   because the whole list arrives in the document's own response: asking again
   to hide rows we already hold would be a round trip for nothing. */
const SITES_PER_PAGE = 15;

/* One document, and the sites it points at.
 *
 * The sources are grouped by site rather than listed flat, because that is the
 * readable unit: this article cites ninety-nine sites across two hundred and
 * fourteen links, and a list of the links is a wall while a list of the sites
 * is a summary of what the piece is built on.
 *
 * Every link here was read out of the page already in the blob store. Nothing
 * was fetched to build this view, and nothing is fetched until you follow one. */
export default function Document({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const documentId = Number(id);

  const [detail, setDetail] = useState<MagpieDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  // Which link the ladder is showing, and what it found. Kept beside the link
  // rather than at the top of the page, because with fifteen sources on screen
  // a result at the top says nothing about which one it belongs to.
  const [climbing, setClimbing] = useState<{ id: number; climb: Climb | null }>({
    id: 0,
    climb: null,
  });
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    try {
      setDetail(await fetchDocument(documentId));
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "could not load");
    }
  }, [documentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const doc = detail?.document;
  const sites = detail?.sites ?? [];
  const pages = Math.max(1, Math.ceil(sites.length / SITES_PER_PAGE));
  const shown = sites.slice((page - 1) * SITES_PER_PAGE, page * SITES_PER_PAGE);
  usePublishScreen(
    "Magpie",
    doc
      ? `Looking at "${doc.title}" from ${doc.host}, ${doc.word_count} words, fetched ` +
        `${when(doc.fetched_at)} via ${doc.strategy}. It points at ${sites.length} ` +
        `other sites across ${detail?.links.length ?? 0} links.`
      : "loading a scraped document"
  );

  const keep = async (link: MagpieLink) => {
    setBusy(link.id);
    setClimbing({ id: link.id, climb: null });
    const result = await follow(link.id);
    setClimbing({
      id: link.id,
      climb: result.ok
        ? {
            attempts: result.document.attempts ?? [],
            won: result.document.strategy,
            costUsd: result.document.cost_usd ?? 0,
            kept: `“${result.document.title}”`,
          }
        : {
            attempts: result.attempts ?? [],
            won: null,
            costUsd: 0,
            refusedWhy: `not kept — ${WHY[result.reason] ?? result.reason}`,
          },
    });
    setBusy(null);
    void load();
  };

  if (error === "unauthorised") {
    return (
      <div className="shell">
        <Sidebar active="Magpie" />
        <div className="content">
          <div className="wrap">
            <section className="card sky-empty">
              <p>
                This page needs a session. <a href="/auth/login">Sign in with GitHub</a>.
              </p>
            </section>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="shell">
      <Sidebar active="Magpie" />
      <div className="content">
        <div className="wrap">
          <motion.header
            className="hero doc-head"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: EASE }}
          >
            <Link className="doc-back" href="/magpie">
              ← Everything gathered
            </Link>
            <h1>{doc ? doc.title : error ? "That document is not here" : "…"}</h1>
            {doc && (
              <p className="doc-meta">
                {doc.host}
                {doc.author ? ` · ${doc.author}` : ""}
                {doc.published ? ` · ${doc.published}` : ""}
                {` · ${doc.word_count.toLocaleString()} words · via ${doc.strategy} · ${when(doc.fetched_at)}`}
                {" · "}
                <a href={doc.url} target="_blank" rel="noreferrer noopener">
                  the original ↗
                </a>
              </p>
            )}
          </motion.header>

          <div className="doc-cols">
            <motion.section
              className="card doc-sources"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.05, duration: 0.45, ease: EASE }}
            >
              <h2>Sources{detail ? ` · ${sites.length}` : ""}</h2>
              <p className="mag-hint">
                The sites this article points at, read out of the page we already
                had. Nothing was fetched to list them. Keep one and it is scraped
                the same way anything else is.
              </p>

              <div className="doc-scroll">
              {detail && !detail.links_read && (
                <p className="mag-empty">
                  The stored page is no longer available, so its sources cannot be
                  read. Scrape the link again to get them.
                </p>
              )}
              {detail && detail.links_read && sites.length === 0 && (
                <p className="mag-empty">This article does not point anywhere else.</p>
              )}

              {shown.map((site: MagpieSite, i) => (
                <motion.div
                  key={site.host}
                  className={`doc-site${open === site.host ? " open" : ""}`}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: Math.min(i, 10) * 0.02, duration: 0.28, ease: EASE }}
                >
                  <button
                    className="doc-site-head"
                    onClick={() => setOpen(open === site.host ? null : site.host)}
                    aria-expanded={open === site.host}
                  >
                    <span className="doc-site-name">{site.host}</span>
                    <span className="doc-site-count">
                      {site.links} {site.links === 1 ? "link" : "links"}
                      {site.followed > 0 && ` · ${site.followed} kept`}
                    </span>
                  </button>
                  {open !== site.host && site.example && (
                    <p className="doc-site-eg">{site.example}</p>
                  )}
                  {open === site.host && (
                    <ul className="doc-links">
                      {(detail?.links ?? [])
                        .filter((l) => l.host === site.host)
                        .map((link) => (
                          <li key={link.id}>
                            <a href={link.url} target="_blank" rel="noreferrer noopener">
                              {link.anchor || link.url}
                            </a>
                            {link.occurrences > 1 && (
                              <span className="doc-times">×{link.occurrences}</span>
                            )}
                            {link.scraped_id ? (
                              <Link className="doc-kept" href={`/magpie/${link.scraped_id}`}>
                                kept ↗
                              </Link>
                            ) : (
                              <button
                                className="doc-keep"
                                onClick={() => keep(link)}
                                disabled={busy !== null}
                              >
                                {busy === link.id ? "Fetching…" : "Keep this too"}
                              </button>
                            )}
                            {climbing.id === link.id && (
                              <Ladder
                                compact
                                busy={busy === link.id}
                                climb={climbing.climb}
                                onDone={() => setClimbing({ id: 0, climb: null })}
                              />
                            )}
                          </li>
                        ))}
                    </ul>
                  )}
                </motion.div>
              ))}
              </div>

              {pages > 1 && (
                <div className="pager">
                  <button
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page <= 1}
                  >
                    Previous
                  </button>
                  <span>
                    Page {page} of {pages}
                  </span>
                  <button
                    onClick={() => setPage((p) => Math.min(pages, p + 1))}
                    disabled={page >= pages}
                  >
                    Next
                  </button>
                </div>
              )}
            </motion.section>

            <motion.section
              className="card doc-text"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.12, duration: 0.45, ease: EASE }}
            >
              <h2>What it says</h2>
              {doc ? (
                <div className="doc-body">
                  {doc.text.split("\n").filter(Boolean).map((line, i) => (
                    <p key={i}>{line}</p>
                  ))}
                </div>
              ) : (
                <p className="mag-empty">…</p>
              )}
            </motion.section>
          </div>
        </div>
      </div>
    </div>
  );
}
