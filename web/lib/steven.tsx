"use client";

import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from "react";
import { describe, useScreenContext } from "@/lib/screen-context";
import {
  Thread, Turn, deleteThread, loadThreads, saveThread, titleFor,
} from "@/lib/threads";

/* One conversation, wherever you are having it.
 *
 * The palette and the Steven page are two views of the same thing, so the
 * state lives above both rather than inside either. Kept in each component it
 * would fork the moment you opened one while the other was mounted — ask a
 * question in the palette, walk over to the page, and find a conversation that
 * had not heard it.
 *
 * Mounted in the root layout, so it survives client-side navigation and the
 * reply to a question asked on one page arrives even after you have left it. */

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";
const STORAGE_ACTIVE = "screener.palette.active";

/* Two cues, for two different events.
 *
 * `reply` sounds when an answer lands — a "done" signal for someone who looked
 * away during a model call, not an alert. `handoff` sounds when the
 * conversation has actually reached Discord, which is the one moment the
 * dashboard cannot show you the result of: the confirmation is in another
 * application. Both quiet on purpose. */
const CHIMES = { reply: "/chime.mp3", handoff: "/handoff.mp3" } as const;
const CHIME_VOLUME = 0.4;

type Cue = keyof typeof CHIMES;

type Handoff = "idle" | "sending" | "sent" | "failed";

type Steven = {
  turns: Turn[];
  threads: Thread[];
  threadId: string;
  thinking: boolean;
  /* True for a moment after a reply, so the orb settles rather than snapping
     from fast to still. */
  settling: boolean;
  conversing: boolean;
  /** What the current page says it is showing, as one line, or "". */
  seeing: string;
  ask: (question: string) => Promise<void>;
  newChat: () => void;
  openThread: (thread: Thread) => void;
  removeThread: (id: string) => void;
  handoffState: Handoff;
  handoffNote: string;
  handoff: () => Promise<void>;
};

const Ctx = createContext<Steven | null>(null);

export function useSteven(): Steven {
  const value = useContext(Ctx);
  if (!value) throw new Error("useSteven outside StevenProvider");
  return value;
}

export function StevenProvider({ children }: { children: React.ReactNode }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState<string>(() => String(Date.now()));
  const [thinking, setThinking] = useState(false);
  const [settling, setSettling] = useState(false);
  const [handoffState, setHandoffState] = useState<Handoff>("idle");
  const [handoffNote, setHandoffNote] = useState("");
  const { context } = useScreenContext();
  const chimesRef = useRef<Partial<Record<Cue, HTMLAudioElement>>>({});

  useEffect(() => {
    const saved = loadThreads();
    setThreads(saved);
    /* Reopen whatever was being discussed. This provider outlives client-side
       navigation, so it is only for a genuine reload — but losing a
       conversation to a refresh is the same annoyance either way. */
    const activeId = localStorage.getItem(STORAGE_ACTIVE);
    const active = activeId ? saved.find((t) => t.id === activeId) : undefined;
    if (active) {
      setThreadId(active.id);
      setTurns(active.turns);
    }
  }, []);

  useEffect(() => {
    // Built once, on the client: `Audio` does not exist while this renders on
    // the server, and a new element per event would re-fetch the file and play
    // a beat late, which for a "done" sound is the whole point missed.
    for (const [cue, src] of Object.entries(CHIMES)) {
      const audio = new Audio(src);
      audio.preload = "auto";
      audio.volume = CHIME_VOLUME;
      chimesRef.current[cue as Cue] = audio;
    }
  }, []);

  /* Saved on every change rather than on close: a tab shut mid-conversation is
     exactly when you would most want the history to have kept up. */
  useEffect(() => {
    if (turns.length === 0) return;
    setThreads(
      saveThread({ id: threadId, title: titleFor(turns), turns, updatedAt: Date.now() })
    );
    localStorage.setItem(STORAGE_ACTIVE, threadId);
  }, [turns, threadId]);

  const chime = useCallback((cue: Cue) => {
    const audio = chimesRef.current[cue];
    if (!audio) return;
    // Rewound first, so a second reply still sounds instead of being swallowed
    // while the first is mid-play.
    audio.currentTime = 0;
    // Autoplay is blocked until the page has been interacted with. Asking a
    // question is an interaction, so this normally succeeds; a chime that does
    // not is not worth an unhandled rejection in the console.
    void audio.play().catch(() => {});
  }, []);

  const ask = useCallback(
    async (raw: string) => {
      const question = raw.trim();
      if (!question || thinking) return;
      setTurns((t) => [...t, { role: "you", text: question }]);
      setThinking(true);
      try {
        const seeing = describe(context);
        const response = await fetch(
          `${BASE}/api/ask?q=${encodeURIComponent(question)}` +
            (seeing ? `&context=${encodeURIComponent(seeing)}` : "") +
            /* Steven remembers the last couple of exchanges, read back per
               person on the server rather than sent up with every question —
               so it also survives the walk over to Discord. The one thing the
               server cannot know is which conversation this is, so an empty
               transcript says so and New chat clears his memory as well as the
               screen. */
            (turns.length === 0 ? "&fresh=1" : ""),
          { credentials: "include", cache: "no-store" }
        );
        const body = await response.json();
        setTurns((t) => [
          ...t,
          {
            role: "steven",
            text: response.ok ? body.reply : body.error ?? "That failed.",
            tools: body.tools ?? [],
            // Drawn by a tool rather than described by the model, so it is kept
            // as data and re-rendered from history rather than rebuilt from
            // text.
            charts: body.charts ?? [],
          },
        ]);
      } catch {
        setTurns((t) => [...t, { role: "steven", text: "I could not reach the server." }]);
      } finally {
        setThinking(false);
        setSettling(true);
        // Both paths reach here, so a failure chimes too: the signal is "Steven
        // is finished", and waiting in silence for an answer that already
        // failed is the worse outcome.
        chime("reply");
        setTimeout(() => setSettling(false), 2600);
      }
    },
    [thinking, context, chime, turns.length]
  );

  const newChat = useCallback(() => {
    setTurns([]);
    const id = String(Date.now());
    localStorage.setItem(STORAGE_ACTIVE, id);
    setThreadId(id);
  }, []);

  const openThread = useCallback((thread: Thread) => {
    setTurns(thread.turns);
    setThreadId(thread.id);
    localStorage.setItem(STORAGE_ACTIVE, thread.id);
  }, []);

  const removeThread = useCallback(
    (id: string) => {
      setThreads(deleteThread(id));
      // Deleting the open conversation leaves the transcript on screen with
      // nothing behind it, so it starts a fresh one instead.
      if (id === threadId) newChat();
    },
    [threadId, newChat]
  );

  const handoff = useCallback(async () => {
    setHandoffState("sending");
    try {
      const response = await fetch(
        `${BASE}/api/handoff?context=${encodeURIComponent(describe(context))}`,
        { credentials: "include", cache: "no-store" }
      );
      setHandoffState(response.ok ? "sent" : "failed");
      // Only on success, unlike the reply cue. A failure is shown on the chip
      // right where you are looking; a success happened somewhere else, and
      // that is the case worth a sound.
      if (response.ok) chime("handoff");
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setHandoffNote(body.error ?? "Could not reach Discord.");
      }
    } catch {
      setHandoffState("failed");
      setHandoffNote("Could not reach the server.");
    }
    // Back to its resting label, so the chip stays usable rather than becoming
    // a permanent receipt.
    setTimeout(() => setHandoffState("idle"), 4000);
  }, [context, chime]);

  const value = useMemo<Steven>(
    () => ({
      turns, threads, threadId, thinking, settling,
      conversing: turns.length > 0 || thinking,
      seeing: context ? `${context.page} · ${context.summary}` : "",
      ask, newChat, openThread, removeThread,
      handoffState, handoffNote, handoff,
    }),
    [
      turns, threads, threadId, thinking, settling, context,
      ask, newChat, openThread, removeThread, handoffState, handoffNote, handoff,
    ]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
