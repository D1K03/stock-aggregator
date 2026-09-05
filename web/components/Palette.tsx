"use client";

import { AnimatePresence, motion } from "framer-motion";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import ChatChart from "@/components/ChatChart";
import ChatRows from "@/components/ChatRows";
import DiscordHandoff from "@/components/DiscordHandoff";
import MicButton from "@/components/MicButton";
import Orb, { OrbState } from "@/components/Orb";
import ToolTrace from "@/components/ToolTrace";
import { renderMarkdown } from "@/lib/markdown";
import { useSteven } from "@/lib/steven";
import { SKILLS, Thread, whenever } from "@/lib/threads";

const EASE = [0, 0, 0.2, 1] as const;

/* Only what exists. The rail lists Universe, Alerts and Runs greyed out to
   show the shape of the thing; offering them here, where every row is
   something you are about to press enter on, would just be a dead end. */
const PAGES = [
  { label: "Overview", href: "/", hint: "Morning snapshot" },
  { label: "Steven", href: "/steven", hint: "The full page, same conversation" },
  { label: "Audit", href: "/audit", hint: "Commands, spend, tool calls" },
  { label: "Playground", href: "/playground", hint: "Read-only SQL over the data" },
  { label: "Skybird", href: "/skybird", hint: "Live stream capture and transcripts" },
];

/* Docked to the right, it stays put across navigations, so its width lives in
   localStorage the way the sidebar's does. */
const STORAGE_DOCKED = "screener.palette.docked";
const STORAGE_OPEN = "screener.palette.open";

/* Pages the palette does not belong on.
 *
 * Steven has a page of his own, where the palette would be the same
 * conversation in a smaller box floating over the larger one.
 *
 * And the sign-in page, which is the one page reachable without a session:
 * offering "Ask Steven" to anyone on the internet advertises a feature they
 * cannot use and answers a click with an authorization error. The API refuses
 * them regardless — that is where the enforcement is — but a control nobody
 * signed in should see does not belong on the page they land on. */
const HIDDEN_ON = new Set(["/steven", "/login"]);

export default function Palette() {
  const pathname = usePathname();
  const {
    turns, threads, thinking, settling, conversing, seeing,
    ask, newChat, openThread, removeThread,
  } = useSteven();

  const [open, setOpen] = useState(false);
  // The side, unless you have moved it. A panel you can talk to while using
  // the page is the point; the centred overlay covers the thing being asked
  // about.
  const [docked, setDocked] = useState(true);
  const [query, setQuery] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  // Slid off to the right, with only its tab showing. Docked only.
  const [tucked, setTucked] = useState(false);
  const [ready, setReady] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Absent means never moved, which is docked.
    const storedDock = localStorage.getItem(STORAGE_DOCKED);
    setDocked(storedDock === null ? true : storedDock === "1");

    /* Open only if it was open when you last left. Docking is a position, not
       an instruction to appear: defaulting to the side and auto-opening would
       put a panel over the page on a first visit, before anyone had asked for
       one. */
    setOpen(localStorage.getItem(STORAGE_OPEN) === "1");
    setReady(true);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Cmd+J on a Mac, Ctrl+J elsewhere. metaKey alone would leave every
      // Linux and Windows browser without a shortcut.
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        setOpen((v) => !v);
      }
      // Cmd+K sends it to the side. It is the browser's find-in-page shortcut
      // on some setups, but this only fires while the palette is the thing
      // being used, and preventDefault keeps it from doing both.
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setDocked((d) => {
          const next = !d;
          localStorage.setItem(STORAGE_DOCKED, next ? "1" : "0");
          return next;
        });
        setTucked(false);
        setOpen(true);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [docked]);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 60);
  }, [open]);

  useEffect(() => {
    // Skipped on the very first render, which runs before the stored value has
    // been read and would otherwise overwrite it with the default.
    if (!ready) return;
    localStorage.setItem(STORAGE_OPEN, open ? "1" : "0");
  }, [open, ready]);

  /* Docked, it stays put. Clicking the page is how you use the page while
     talking to Steven about it, so dismissing on an outside click made the
     panel unusable for the thing it is for. Tucking is explicit instead: the
     tab on its left edge slides it away and brings it back. */

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns, thinking]);

  const matches = query.trim()
    ? PAGES.filter((p) => p.label.toLowerCase().includes(query.trim().toLowerCase()))
    : PAGES;

  // Speaking for a moment after a reply lands, so the orb settles rather than
  // snapping from fast to still.
  const orbState: OrbState = thinking ? "thinking" : settling ? "speaking" : "idle";

  const submit = () => {
    void ask(query);
    setQuery("");
    setShowHistory(false);
  };

  const runSkill = (prompt: string) => {
    void ask(prompt);
    setShowHistory(false);
  };

  const toggleDock = () => {
    const next = !docked;
    setDocked(next);
    localStorage.setItem(STORAGE_DOCKED, next ? "1" : "0");
    setTucked(false);
    setOpen(true);
  };

  const body = (
    <>
      <div className="pal-input">
        <Orb state={orbState} size={17} />
        <input
          ref={inputRef}
          value={query}
          placeholder={conversing ? "Ask Steven something else…" : "Search, or ask Steven…"}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          aria-label="Search or ask Steven"
        />
        <MicButton
          onTranscript={(text) =>
            setQuery((current) => (current ? `${current} ${text}` : text))
          }
        />
        <button
          className={`pal-icon${showHistory ? " on" : ""}`}
          onClick={() => setShowHistory((v) => !v)}
          title="Previous chats"
          aria-label="Previous chats"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M3 12a9 9 0 109-9 9 9 0 00-6.4 2.6L3 8" /><path d="M3 3v5h5M12 7v5l3 2" />
          </svg>
        </button>
        {conversing && (
          <button
            className="pal-icon"
            onClick={() => {
              newChat();
              setShowHistory(false);
              setQuery("");
            }}
            title="New chat"
            aria-label="New chat"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
        )}
        <button
          className="pal-dock"
          onClick={toggleDock}
          title={`${docked ? "Return to centre" : "Send to the side"} (⌘K)`}
          aria-label={docked ? "Return to centre" : "Send to the side"}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <path d={docked ? "M9 4v16" : "M15 4v16"} />
          </svg>
        </button>
        {!docked && <kbd className="pal-kbd">esc</kbd>}
      </div>

      {/* Under the bar: what Steven can see, and where the conversation can go
          next. Small and quiet, because the answer is the thing. */}
      <div className="pal-chips">
        {seeing && (
          <span className="chip chip-seeing" title={seeing}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z" /><circle cx="12" cy="12" r="3" />
            </svg>
            <span className="chip-text">{seeing}</span>
          </span>
        )}
        <DiscordHandoff />
      </div>

      {showHistory ? (
        <div className="pal-list">
          <div className="pal-group">Previous chats</div>
          {threads.length === 0 && (
            <div className="pal-item as-hint">Nothing yet. Ask Steven something.</div>
          )}
          {threads.map((thread) => (
            <div key={thread.id} className="pal-thread-row">
              <button
                className="pal-item"
                onClick={() => {
                  openThread(thread);
                  setShowHistory(false);
                }}
              >
                <span>{thread.title}</span>
                <small>{whenever(thread.updatedAt)} · {thread.turns.length} messages</small>
              </button>
              <button
                className="pal-icon subtle"
                onClick={() => removeThread(thread.id)}
                title="Delete"
                aria-label={`Delete ${thread.title}`}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              </button>
            </div>
          ))}
        </div>
      ) : conversing ? (
        <div className="pal-thread" ref={scrollRef}>
          {turns.map((turn, i) => (
            <motion.div
              key={i}
              className={`pal-line ${turn.role}`}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.28, ease: EASE }}
            >
              {turn.tools && turn.tools.length > 0 && <ToolTrace tools={turn.tools} />}
              <div className={`pal-turn ${turn.role}`}>
                {turn.role === "steven" ? (
                  <div className="md">{renderMarkdown(turn.text)}</div>
                ) : (
                  turn.text
                )}
              </div>
              {/* Under the answer: the sentence says what happened, the chart
                  shows where. */}
              {turn.charts?.map((spec, c) => (
                <ChatChart key={`${i}-${c}`} spec={spec} />
              ))}
                      {turn.rows?.map((spec, c) => (
                        <ChatRows key={`r${i}-${c}`} spec={spec} />
                      ))}
            </motion.div>
          ))}
          {thinking && (
            <div className="pal-thinking">
              <Orb state="thinking" size={18} />
              <span>Steven is thinking</span>
            </div>
          )}

          {/* Under the conversation, so they are a nudge for what to ask next
              rather than a menu competing with the answer. */}
          {!thinking && (
            <div className="pal-skills">
              {SKILLS.map((skill) => (
                <button key={skill.label} onClick={() => runSkill(skill.prompt)}>
                  {skill.label}
                </button>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="pal-list">
          <div className="pal-group">Go to</div>
          {matches.map((p) => (
            <Link key={p.label} href={p.href} className="pal-item">
              <span>{p.label}</span>
              <small>{p.hint}</small>
            </Link>
          ))}
          {matches.length === 0 && (
            <div className="pal-item as-hint">
              Press <kbd className="pal-kbd">enter</kbd> to ask Steven instead
            </div>
          )}

          <div className="pal-group">Ask Steven</div>
          <div className="pal-skills in-list">
            {SKILLS.map((skill) => (
              <button key={skill.label} onClick={() => runSkill(skill.prompt)}>
                {skill.label}
              </button>
            ))}
          </div>

          {/* Only here. During a conversation these would sit under the last
              message and march down the panel as it grows. */}
          <div className="pal-hints">
            <span><kbd className="pal-kbd">↵</kbd> ask Steven</span>
            <span><kbd className="pal-kbd">⌘J</kbd> toggle</span>
            <span><kbd className="pal-kbd">⌘K</kbd> {docked ? "centre" : "side"}</span>
          </div>
        </div>
      )}
    </>
  );

  // After the hooks, never before: bailing earlier would change how many run
  // between renders and React would take the whole tree down.
  if (HIDDEN_ON.has(pathname)) return null;

  return (
    <>
      {/* The only sign Steven exists, for anyone who does not already know the
          shortcut. It carries the shortcut so it teaches it, and gets out of
          the way the moment the palette is open. */}
      <AnimatePresence>
        {!open && (
          <motion.button
            className="pal-launcher"
            onClick={() => setOpen(true)}
            initial={{ opacity: 0, y: 8, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.96 }}
            transition={{ duration: 0.22, ease: EASE }}
            aria-label="Ask Steven"
          >
            <Orb state="idle" size={20} />
            <span className="pal-launcher-text">Ask Steven</span>
            <kbd className="pal-kbd">⌘J</kbd>
          </motion.button>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {open && !docked && (
          <motion.div
            className="pal-scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            onClick={() => setOpen(false)}
          />
        )}
      </AnimatePresence>

      <AnimatePresence mode="wait">
        {open && (
          <motion.div
            // Keyed on the mode so the two positions are separate elements:
            // animating one box between centre and edge fights the layout and
            // reads as a slide with a resize bolted on.
            key={docked ? "docked" : "centre"}
            ref={panelRef}
            className={`pal ${docked ? "docked" : "centre"}${tucked ? " tucked" : ""}`}
            initial={docked ? { x: 40, opacity: 0 } : { y: -12, opacity: 0, scale: 0.97 }}
            animate={
              docked
                // Slides out by its own width, so the panel is gone and only
                // the tab, which sits outside that box, is left on screen.
                ? { x: tucked ? "calc(100% + 16px)" : 0, opacity: 1 }
                : { y: 0, opacity: 1, scale: 1 }
            }
            exit={docked ? { x: 40, opacity: 0 } : { y: -8, opacity: 0, scale: 0.98 }}
            transition={{ duration: 0.32, ease: EASE }}
          >
            {docked && (
              <button
                className="pal-tab"
                onClick={() => setTucked((v) => !v)}
                title={tucked ? "Show Steven" : "Tuck away"}
                aria-label={tucked ? "Show Steven" : "Tuck away"}
                aria-expanded={!tucked}
              >
                <motion.span
                  animate={{ rotate: tucked ? 180 : 0 }}
                  transition={{ duration: 0.32, ease: EASE }}
                  style={{ display: "grid", placeItems: "center" }}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                       strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M9 6l6 6-6 6" />
                  </svg>
                </motion.span>
              </button>
            )}
            {body}
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
