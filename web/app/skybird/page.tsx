"use client";

import { motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import Sidebar from "@/components/Sidebar";
import { usePublishScreen } from "@/lib/screen-context";
import {
  SkybirdListing,
  SkybirdSegment,
  SkybirdSession,
  deleteCapture,
  describe,
  fetchSessions,
  fetchTranscript,
  pauseCapture,
  resumeCapture,
  stamp,
  startCapture,
  stopCapture,
} from "@/lib/skybird";

const EASE = [0, 0, 0.2, 1] as const;

/* Two intervals rather than one. The transcript is what somebody is watching,
   so it moves at roughly the chunk length; the list of captures only changes
   when a state does, and polling it as hard would triple the queries for
   nothing. */
const TRANSCRIPT_MS = 3000;
const LISTING_MS = 6000;

function badge(session: SkybirdSession): string {
  if (session.state === "running") return "on";
  if (session.state === "failed") return "bad";
  if (session.state === "paused") return "held";
  return session.live ? "wait" : "off";
}

export default function Skybird() {
  const [listing, setListing] = useState<SkybirdListing | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [segments, setSegments] = useState<SkybirdSegment[]>([]);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  // The highest sequence number already on screen.
  const seen = useRef(0);

  const load = useCallback(async () => {
    try {
      const next = await fetchSessions();
      setListing(next);
      setError((previous) => (previous === "unauthorised" ? previous : null));
      // Land on something rather than an empty stage: whatever is live, or the
      // most recent capture if nothing is.
      setSelected((current) => {
        if (current !== null && next.sessions.some((s) => s.id === current)) return current;
        return (next.sessions.find((s) => s.live) ?? next.sessions[0])?.id ?? null;
      });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "failed");
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, LISTING_MS);
    return () => clearInterval(timer);
  }, [load]);

  /* Polls for everything after the last sequence number it holds, so a two
     hour transcript is fetched once and then extended a line at a time.
     The mark is a ref rather than state because the poll reads it and the
     effect must not restart every time it moves. */
  useEffect(() => {
    if (selected === null) return;
    let cancelled = false;
    // A different capture is a different transcript, so the lines go with it.
    seen.current = 0;
    setSegments([]);

    const poll = async () => {
      try {
        const page = await fetchTranscript(selected, seen.current);
        if (cancelled || page.segments.length === 0) return;
        seen.current = page.segments[page.segments.length - 1].seq;
        setSegments((current) => [...current, ...page.segments]);
      } catch {
        // Left to the listing poll to report. A transcript that skips a round
        // because the network hiccupped should not blank the page.
      }
    };

    poll();
    const timer = setInterval(poll, TRANSCRIPT_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [selected]);

  // Follow the transcript only while it is already at the bottom, so reading
  // back through an hour of it is not yanked forward every three seconds.
  useEffect(() => {
    const box = feed.current;
    if (!box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    if (atBottom) box.scrollTop = box.scrollHeight;
  }, [segments]);

  const act = async (what: () => Promise<unknown>, said: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await what();
      setNotice(said);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "failed");
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!url.trim() || busy) return;
    act(async () => {
      const { session } = await startCapture(url);
      setSelected(session.id);
      setUrl("");
    }, "Capture requested. It starts within a couple of seconds.");
  };

  const remove = (session: SkybirdSession) => {
    const what = session.title ?? session.source_url;
    if (!window.confirm(`Delete this capture and its ${session.segment_count} transcribed lines?\n\n${what}`)) {
      return;
    }
    act(() => deleteCapture(session.id), "Capture and transcript deleted.");
  };

  const sessions = listing?.sessions ?? [];
  const current = sessions.find((s) => s.id === selected) ?? null;
  const liveCount = sessions.filter((s) => s.live).length;

  usePublishScreen(
    "Skybird",
    listing
      ? `${liveCount} of ${listing.max_sessions} live stream captures running, ` +
        `${sessions.length} in the list. ` +
        (current
          ? `Looking at ${current.title ?? current.source_url} on ${current.platform}, ` +
            `${describe(current)}, ${current.segment_count} transcribed lines.`
          : "Nothing selected.")
      : "loading the captures"
  );

  return (
    <div className="shell">
      <Sidebar active="Skybird" />
      <div className="content">
        <div className="wrap">
          <motion.header
            className="hero"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: EASE }}
          >
            <h1>Skybird</h1>
            <p>
              Paste a live stream and its audio is transcribed as it happens and
              kept. Watch it here while it runs. Nothing is deleted on its own.
            </p>
          </motion.header>

          {error === "unauthorised" ? (
            <section className="card sky-empty">
              <p>
                This page needs a session.{" "}
                <a href="/auth/login">Sign in with GitHub</a>.
              </p>
            </section>
          ) : (
            <>
              <motion.form
                className="card sky-start"
                onSubmit={submit}
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.05, duration: 0.45, ease: EASE }}
              >
                <input
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://www.youtube.com/watch?v=… or https://twitch.tv/…"
                  aria-label="Stream URL"
                  spellCheck={false}
                />
                <button type="submit" disabled={busy || !url.trim()}>
                  {busy ? "Working…" : "Capture"}
                </button>
                <p className="sky-hint">
                  {listing
                    ? `${listing.platforms.map((p) => p.display_name).join(" and ")} · ` +
                      `${liveCount} of ${listing.max_sessions} running · ` +
                      `${listing.chunk_seconds}s chunks`
                    : "…"}
                </p>
              </motion.form>

              {(error || notice) && (
                <p className={`sky-say${error ? " bad" : ""}`} role="status">
                  {error ?? notice}
                </p>
              )}

              {current && (
                <motion.section
                  className="card sky-stage"
                  key={current.id}
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.45, ease: EASE }}
                >
                  <div className="sky-player">
                    {current.embed_url ? (
                      /* The platform's own player. No restreaming through our
                         box: it is bandwidth we do not need for something
                         served for free, and it is the sanctioned way to
                         watch. */
                      <iframe
                        src={current.embed_url}
                        title={current.title ?? "Live stream"}
                        allow="autoplay; fullscreen; encrypted-media; picture-in-picture"
                        allowFullScreen
                      />
                    ) : (
                      <div className="sky-noplayer">
                        <p>No player yet.</p>
                        <span>
                          A channel URL names no broadcast until the capture has
                          connected — this fills in once it has.
                        </span>
                      </div>
                    )}
                  </div>

                  <div className="sky-feed-side">
                    <div className="sky-head">
                      <span className={`sky-dot ${badge(current)}`} aria-hidden="true" />
                      <strong>{current.title ?? current.source_url}</strong>
                      <em>{describe(current)}</em>
                    </div>
                    {/* The wrapper takes whatever height the player leaves and
                        the feed fills it out of flow, so a transcript cannot
                        size the row it is sitting in. */}
                    <div className="sky-feed-wrap">
                      <div className="sky-feed" ref={feed}>
                        {segments.length === 0 ? (
                          <p className="sky-waiting">
                            {current.state === "paused"
                              ? "Paused. Resume to carry on from where this left off."
                              : current.live
                              ? `Listening. The first lines arrive about ${current.chunk_seconds} seconds behind live.`
                              : "No transcript was recorded for this capture."}
                          </p>
                        ) : (
                          segments.map((segment) => (
                            <p key={segment.seq} className="sky-line">
                              <span>{stamp(segment.offset_seconds)}</span>
                              {segment.text}
                            </p>
                          ))
                        )}
                      </div>
                    </div>
                  </div>
                </motion.section>
              )}

              <motion.section
                className="card sky-list"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.15, duration: 0.45, ease: EASE }}
              >
                <h2>Captures</h2>
                {sessions.length === 0 ? (
                  <p className="sky-waiting">
                    Nothing captured yet. Paste a live stream above.
                  </p>
                ) : (
                  <ul>
                    {sessions.map((session) => (
                      <li
                        key={session.id}
                        className={session.id === selected ? "on" : undefined}
                      >
                        <button
                          type="button"
                          className="sky-pick"
                          onClick={() => setSelected(session.id)}
                        >
                          <span className={`sky-dot ${badge(session)}`} aria-hidden="true" />
                          <span className="sky-what">
                            <strong>{session.title ?? session.source_url}</strong>
                            <em>
                              {session.platform}
                              {session.channel ? ` · ${session.channel}` : ""} ·{" "}
                              {describe(session)} · {session.segment_count} lines
                              {/* Shown rather than hidden: a capture failing
                                  every chunk and one nobody is talking on
                                  produce the same empty transcript. */}
                              {session.chunks_failed > 0 && ` · ${session.chunks_failed} failed`}
                              {session.chunks_dropped > 0 && ` · ${session.chunks_dropped} dropped`}
                            </em>
                            {session.last_error && (
                              <em className="sky-bad">{session.last_error}</em>
                            )}
                          </span>
                        </button>
                        <span className="sky-acts">
                          {/* Pause holds the stream; stop gives it up. Only one
                              of the pair is ever offered, because the other is
                              not a thing you can do from where it is. */}
                          {session.state === "paused" && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                act(() => resumeCapture(session.id), "Capture resumed.")
                              }
                            >
                              Resume
                            </button>
                          )}
                          {(session.state === "running" ||
                            session.state === "starting" ||
                            session.state === "requested") && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                act(() => pauseCapture(session.id), "Capture paused.")
                              }
                            >
                              Pause
                            </button>
                          )}
                          {session.live && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                act(() => stopCapture(session.id), "Capture stopped.")
                              }
                            >
                              Stop
                            </button>
                          )}
                          <button
                            type="button"
                            className="bad"
                            disabled={busy}
                            onClick={() => remove(session)}
                          >
                            Delete
                          </button>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </motion.section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
