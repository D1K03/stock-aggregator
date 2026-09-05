"use client";

import { motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import ChatChart from "@/components/ChatChart";
import ChatRows from "@/components/ChatRows";
import DiscordHandoff from "@/components/DiscordHandoff";
import MicButton from "@/components/MicButton";
import Orb, { OrbState } from "@/components/Orb";
import Sidebar from "@/components/Sidebar";
import ToolTrace from "@/components/ToolTrace";
import { renderMarkdown } from "@/lib/markdown";
import { useSteven } from "@/lib/steven";
import { SKILLS, whenever } from "@/lib/threads";

const EASE = [0, 0, 0.2, 1] as const;

/* Steven, with room to think.
 *
 * The same conversation as the palette — literally the same state, held above
 * both in `StevenProvider` — shown at a size where a chart is worth looking at
 * and the history is a column rather than a menu. Ask something here, open the
 * palette on another page, and it is mid-thread where you left it.
 *
 * There is deliberately no screen context published from this page. Steven
 * describing the page you are reading him on is a hall of mirrors, and
 * `usePublishScreen` clears on unmount, so arriving here correctly leaves him
 * with nothing to see. */
export default function StevenPage() {
  const {
    turns, threads, threadId, thinking, settling, conversing,
    ask, newChat, openThread, removeThread,
  } = useSteven();
  const [query, setQuery] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, thinking]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const orbState: OrbState = thinking ? "thinking" : settling ? "speaking" : "idle";

  const submit = () => {
    void ask(query);
    setQuery("");
  };

  return (
    <div className="shell">
      <Sidebar active="Steven" />
      <div className="content stv">
        <aside className="stv-chats">
          <div className="stv-chats-head">
            <span>Chats</span>
            <button onClick={newChat} title="New chat" aria-label="New chat">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <path d="M12 5v14M5 12h14" />
              </svg>
            </button>
          </div>
          <div className="stv-chats-list">
            {threads.length === 0 && (
              <p className="stv-chats-empty">Nothing yet. Ask him something.</p>
            )}
            {threads.map((thread) => (
              <div
                key={thread.id}
                className={`stv-chat${thread.id === threadId ? " on" : ""}`}
              >
                <button onClick={() => openThread(thread)}>
                  <span>{thread.title}</span>
                  <small>{whenever(thread.updatedAt)} · {thread.turns.length} messages</small>
                </button>
                <button
                  className="stv-chat-x"
                  onClick={() => removeThread(thread.id)}
                  title="Delete"
                  aria-label={`Delete ${thread.title}`}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                       strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </aside>

        <main className="stv-main">
          <div className="stv-scroll">
            <div className="stv-inner">
              {!conversing ? (
                <motion.div
                  className="stv-empty"
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.5, ease: EASE }}
                >
                  <Orb state="idle" size={54} />
                  <h1>Steven</h1>
                  <p>
                    Ask about the screener, the deployment, or a ticker&rsquo;s history.
                    He draws the chart and marks what you asked about. The figures
                    are illustrative until ingest lands, and he says so.
                  </p>
                  <div className="stv-skills">
                    {SKILLS.map((skill) => (
                      <button key={skill.label} onClick={() => void ask(skill.prompt)}>
                        {skill.label}
                      </button>
                    ))}
                  </div>
                </motion.div>
              ) : (
                <>
                  {turns.map((turn, i) => (
                    <motion.div
                      key={i}
                      className={`stv-line ${turn.role}`}
                      initial={{ opacity: 0, y: 8 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.3, ease: EASE }}
                    >
                      {turn.role === "steven" && (
                        <div className="stv-who">
                          <Orb state="idle" size={20} />
                          <span>Steven</span>
                        </div>
                      )}
                      {turn.tools && turn.tools.length > 0 && <ToolTrace tools={turn.tools} />}
                      <div className={`stv-turn ${turn.role}`}>
                        {turn.role === "steven" ? (
                          <div className="md">{renderMarkdown(turn.text)}</div>
                        ) : (
                          turn.text
                        )}
                      </div>
                      {turn.charts?.map((spec, c) => (
                        <ChatChart key={`${i}-${c}`} spec={spec} />
                      ))}
                      {turn.rows?.map((spec, c) => (
                        <ChatRows key={`r${i}-${c}`} spec={spec} />
                      ))}
                    </motion.div>
                  ))}
                  {thinking && (
                    <div className="stv-thinking">
                      <Orb state="thinking" size={22} />
                      <span>Steven is thinking</span>
                    </div>
                  )}
                </>
              )}
              <div ref={endRef} />
            </div>
          </div>

          <div className="stv-composer">
            <div className="stv-composer-inner">
              <Orb state={orbState} size={19} />
              <textarea
                ref={inputRef}
                rows={1}
                value={query}
                placeholder="Ask Steven…"
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  // Enter sends; shift-enter is how you write a second line.
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submit();
                  }
                }}
                aria-label="Ask Steven"
              />
              {/* Before send, because it fills the box rather than emptying
                  it: what it hears lands in the textarea for you to read and
                  correct, and enter is still what asks. */}
              <MicButton
                onTranscript={(text) =>
                  setQuery((current) => (current ? `${current} ${text}` : text))
                }
              />
              <button
                className="stv-send"
                onClick={submit}
                disabled={!query.trim() || thinking}
                aria-label="Send"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M5 12h14M13 6l6 6-6 6" />
                </svg>
              </button>
            </div>
            {/* Where the conversation can go next, under the box you type in:
                a few things to ask, and the way out to Discord. The handoff sits
                apart from the suggestions because it ends the conversation here
                rather than continuing it. */}
            {conversing && (
              <div className="stv-next">
                <div className="stv-skills">
                  {SKILLS.map((skill) => (
                    <button key={skill.label} onClick={() => void ask(skill.prompt)}>
                      {skill.label}
                    </button>
                  ))}
                </div>
                <DiscordHandoff />
              </div>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
