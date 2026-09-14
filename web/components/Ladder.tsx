"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";

const EASE = [0, 0, 0.2, 1] as const;

/* The climb, as it happens.
 *
 * The rungs are not evenly priced and this is the one place that is visible:
 * `direct` is free, the ISP lanes are bandwidth against a flat monthly plan
 * already paid for, and only the Web Unlocker costs per request. So the thing
 * worth showing is not "it worked" but how far up it had to go — and equally
 * the rungs it never needed, which is why those are drawn greyed rather than
 * left out.
 *
 * **The reveal is a replay, not a live feed, and that distinction is kept
 * honest.** The scrape is one blocking request, so nothing here can know which
 * rung is being tried while it is being tried. While it is in flight the first
 * rung pulses and says "trying"; when the answer lands, the rungs it really
 * climbed are stepped through in the order they really happened. Every frame is
 * something that occurred — the pacing is a reveal of the sequence, not an
 * animation invented over a result that arrived all at once. `ToolTrace` makes
 * the same promise about Steven's tools for the same reason. */

export type Rung = { key: string; label: string; price: string };

export const RUNGS: Rung[] = [
  { key: "direct", label: "Direct", price: "free" },
  { key: "isp_proxy", label: "ISP lanes", price: "free" },
  { key: "unlocker", label: "Web Unlocker", price: "$0.002" },
];

export type Climb = {
  /** Every rung tried, in order. */
  attempts: string[];
  /** The one that answered, or null when nothing did. */
  won: string | null;
  costUsd: number;
  /** The headline that was kept, so the outcome is stated once, here. */
  kept?: string | null;
  /** Set when the page was not kept, in words. */
  refusedWhy?: string | null;
};

/* Slow enough to read a rung before the next one lights, fast enough that a
   two-rung climb is not a wait. */
const STEP_MS = 520;
const LINGER_MS = 4200;

export default function Ladder({
  busy,
  climb,
  onDone,
  compact = false,
}: {
  busy: boolean;
  climb: Climb | null;
  onDone: () => void;
  /** Under a single link rather than under the page. Same rungs, less chrome. */
  compact?: boolean;
}) {
  // How many of the real attempts have been revealed. -1 is "still in flight".
  const [shown, setShown] = useState(-1);

  /* `onDone` is held in a ref rather than depended on.
   *
   * It arrives as an inline arrow from the page, so it is a different function
   * on every render — and the page re-renders whenever the list reloads, which
   * is immediately after a scrape. With it in the dependency list the whole
   * reveal restarted each time: `shown` went back to zero, the finished ladder
   * dropped to its opening state, and the result appeared to vanish and come
   * back. The effect now depends only on the climb it is revealing. */
  const doneRef = useRef(onDone);
  useEffect(() => {
    doneRef.current = onDone;
  });

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reads browser state or starts a load on mount; predates lint in CI, and new code must pass the rule
    if (busy) setShown(-1);
  }, [busy]);

  useEffect(() => {
    if (!climb) return;
    const steps = Math.max(climb.attempts.length, 1);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reads browser state or starts a load on mount; predates lint in CI, and new code must pass the rule
    setShown(0);
    const timers = Array.from({ length: steps }, (_, i) =>
      setTimeout(() => setShown(i + 1), (i + 1) * STEP_MS)
    );
    const away = setTimeout(() => doneRef.current(), steps * STEP_MS + LINGER_MS);
    return () => {
      timers.forEach(clearTimeout);
      clearTimeout(away);
    };
  }, [climb]);

  const open = busy || climb !== null;
  const settled = climb !== null && shown >= climb.attempts.length;

  /* Which rung is being worked on right now.
   *
   * There must always be one until everything is resolved, and getting that
   * wrong is what made this flicker: when the answer arrived, `climb` stopped
   * being null so the first rung's "trying" pulse switched off — but nothing
   * had been revealed yet, so for half a second every rung was grey and the
   * thing you were watching appeared to vanish and come back.
   *
   * While in flight it is the first rung. Once the answer is here it is
   * whichever rung the reveal has reached, so the pulse moves up the ladder
   * rather than going out and relighting. */
  const active = climb
    ? shown < climb.attempts.length
      ? climb.attempts[shown]
      : null
    : RUNGS[0].key;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className={compact ? "lad lad-small" : "lad"}
          // Height as well as opacity, so this pushes the page down as it
          // arrives rather than covering what is under it.
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.3, ease: EASE }}
        >
          <div className="lad-card">
            <div className="lad-rungs">
              {RUNGS.map((rung, i) => {
                const at = climb ? climb.attempts.indexOf(rung.key) : -1;
                const revealed = at >= 0 && at < shown;
                const won = revealed && climb?.won === rung.key;
                const trying = rung.key === active;

                let state = "spare";
                if (won) state = "won";
                else if (revealed) state = "tried";
                else if (trying) state = "trying";

                return (
                  <div key={rung.key} className="lad-step">
                    <motion.div
                      className={`lad-rung ${state}`}
                      animate={{
                        scale: state === "trying" ? [1, 1.03, 1] : 1,
                        opacity: state === "spare" ? 0.38 : 1,
                      }}
                      transition={
                        state === "trying"
                          ? { duration: 1.3, repeat: Infinity, ease: "easeInOut" }
                          : { duration: 0.3, ease: EASE }
                      }
                    >
                      <span className="lad-dot" />
                      <span className="lad-label">{rung.label}</span>
                      <span className="lad-price">
                        {won
                          ? "got it"
                          : revealed
                          ? "blocked"
                          : state === "trying"
                          ? "trying…"
                          : rung.price}
                      </span>
                    </motion.div>
                    {i < RUNGS.length - 1 && (
                      <span className={`lad-arrow${revealed && !won ? " lit" : ""}`}>→</span>
                    )}
                  </div>
                );
              })}
            </div>

            <AnimatePresence>
              {settled && climb && (
                <motion.div
                  className="lad-sum"
                  // Height, not just opacity. The outer `.lad` has finished
                  // animating to `auto` by the time this mounts, so fading in a
                  // full-height line grew the card by its whole height between
                  // two frames and shoved the list below it.
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.3, ease: EASE }}
                >
                  {climb.refusedWhy ? (
                    <span className="lad-refused">{climb.refusedWhy}</span>
                  ) : (
                    <span>
                      {climb.kept && (
                        <>
                          kept <b>{climb.kept}</b> ·{" "}
                        </>
                      )}
                      {climb.costUsd > 0 ? (
                        <>
                          cost <b>${climb.costUsd.toFixed(4)}</b>, it had to reach a paid route
                        </>
                      ) : (
                        <>
                          cost <b>nothing</b>, it never reached a paid route
                        </>
                      )}
                    </span>
                  )}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
