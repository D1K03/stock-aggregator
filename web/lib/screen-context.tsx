"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";

/* What the person is currently looking at.
 *
 * Pages publish a short description of their own state; the palette reads it
 * and sends it with the question, so "what am I looking at?" and "explain
 * this" have an answer. It is a description, not a data feed: a sentence or
 * two that a model can read, assembled by the page that actually knows what is
 * on screen.
 *
 */

export type ScreenContext = {
  page: string;
  /** A short prose summary of the current view. Kept brief; it is sent as tokens. */
  summary: string;
};

type Store = {
  context: ScreenContext | null;
  setContext: (context: ScreenContext | null) => void;
};

const Ctx = createContext<Store>({ context: null, setContext: () => {} });

export function ScreenContextProvider({ children }: { children: React.ReactNode }) {
  const [context, setContext] = useState<ScreenContext | null>(null);
  const value = useMemo(() => ({ context, setContext }), [context]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useScreenContext() {
  return useContext(Ctx);
}

/** Publish this page's state. Pass a stable string; it re-sends on change. */
export function usePublishScreen(page: string, summary: string) {
  const { setContext } = useScreenContext();
  useEffect(() => {
    setContext({ page, summary });
    // Cleared on unmount so a stale view never travels with a later question.
    return () => setContext(null);
  }, [page, summary, setContext]);
}

/** The single line sent to the model. Bounded, because it is paid for. */
export function describe(context: ScreenContext | null): string {
  if (!context) return "";
  return `${context.page}: ${context.summary}.`.slice(0, 400);
}
