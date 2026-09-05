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
 * `illustrative` travels with it and is not decoration. The dashboard renders
 * invented, schema-shaped numbers, and without saying so every question about
 * a row would get an answer that treats them as real market data, which is the
 * one thing Steven is most carefully told not to do. */

export type ScreenContext = {
  page: string;
  /** A short prose summary of the current view. Kept brief; it is sent as tokens. */
  summary: string;
  /** Whether the figures in `summary` are the concept's invented data. */
  illustrative?: boolean;
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
export function usePublishScreen(page: string, summary: string, illustrative = false) {
  const { setContext } = useScreenContext();
  useEffect(() => {
    setContext({ page, summary, illustrative });
    // Cleared on unmount so a stale view never travels with a later question.
    return () => setContext(null);
  }, [page, summary, illustrative, setContext]);
}

/** The single line sent to the model. Bounded, because it is paid for. */
export function describe(context: ScreenContext | null): string {
  if (!context) return "";
  const note = context.illustrative
    ? " These figures are illustrative dashboard data, not live market data."
    : "";
  return `${context.page}: ${context.summary}.${note}`.slice(0, 400);
}
