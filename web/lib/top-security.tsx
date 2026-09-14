"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { fetchScreen } from "@/lib/screen";

/* Whichever security the screen currently ranks first.
 *
 * Steven's chart suggestion used to name NVDA. That was true of the concept
 * data and is a guess about the real thing: the screen is ranked nightly, so
 * the ticker worth offering is the one the ranking put at the top of the table,
 * not one written into the source a month ago.
 *
 * Two sources, because the suggestion appears on pages with no table. The
 * Overview knows its first row directly — under whatever sort and filters are
 * set — and publishes it. Everywhere else, the provider asks the screen for its
 * single leading row, which is the same table with nothing applied.
 *
 * Null when neither has an answer: signed out, no night scored yet, or the
 * database unreachable. The caller drops the suggestion rather than naming a
 * ticker nobody ranked — the point of this module is that the name comes from
 * the data. */

type Store = {
  top: string | null;
  publish: (symbol: string | null) => void;
};

const Ctx = createContext<Store>({ top: null, publish: () => {} });

export function TopSecurityProvider({ children }: { children: React.ReactNode }) {
  const [published, setPublished] = useState<string | null>(null);
  const [leading, setLeading] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    // One row, not a page: this asks who is first, and a page of fifty carries
    // fifty securities' worth of closes to answer it.
    fetchScreen({ sector: "", agree: "", partial: "", sort: "score", offset: 0, limit: 1 }).then(
      (page) => {
        if (live && page.state === "ready") setLeading(page.rows[0]?.symbol ?? null);
      },
      // Nothing else on the page wanted this, and a suggestion is not worth an
      // error message. It simply does not appear.
      () => {},
    );
    return () => {
      live = false;
    };
  }, []);

  // The page beats the fallback while it has an answer, so the suggestion
  // follows the table being looked at rather than the unfiltered screen.
  const value = useMemo(
    () => ({ top: published ?? leading, publish: setPublished }),
    [published, leading],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** The ticker a suggestion should name, or null when there is none to name. */
export function useTopSecurity(): string | null {
  return useContext(Ctx).top;
}

/** Publish the table's first row. Cleared on unmount, as the screen context is,
    so a filtered top never outlives the page that filtered it. */
export function usePublishTop(symbol: string | null) {
  const { publish } = useContext(Ctx);
  useEffect(() => {
    publish(symbol);
    return () => publish(null);
  }, [symbol, publish]);
}
