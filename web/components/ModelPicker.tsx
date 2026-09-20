"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Catalogue,
  INDEX_LABELS,
  INDICES,
  Index,
  ModelRow,
  ROUTER,
  ROUTER_LABEL,
  context,
  loadCatalogue,
  price,
  soleRow,
} from "@/lib/models";

const EASE = [0, 0, 0.2, 1] as const;

/* Which model answers you.
 *
 * Per person rather than per conversation, and that is what makes it worth more
 * than a toggle: the server records the choice against your identity, the
 * Discord bot reads it back through the same fold the spend cap uses, and so
 * picking a model here is picking the model your DMs come back on. One answer
 * to "which model", wherever the question is asked.
 *
 * It lapses after a day. A stronger model gets picked for one hard afternoon
 * and would otherwise quietly become what every message costs from then on, so
 * the choice expires and the recommendation takes over again — the expensive
 * case has to be re-chosen, the cheap one is where everybody ends up. The
 * footer says so, because a revert nobody was told about reads as a bug.
 *
 * The first row is not a model. Four hundred models is not a choice, it is a
 * research task, and nobody opening a chat window wants one — so the top of the
 * list is Steven himself, meaning "you pick". Selecting it stores the rule
 * rather than the answer: every question is then sent to whatever tops the
 * ranking at the moment it is asked, so a better or cheaper model arriving next
 * month is picked up without anybody revisiting this menu. It is the default,
 * and it is where a lapsed choice returns to.
 *
 * Pinning a specific model is the other option and is a real one — but it is
 * the one that goes stale, so it is second rather than first.
 *
 * The row shows what Steven currently resolves to, and why. That is not
 * decoration: this project does not ship a number without the inputs that
 * produced it, and "best value" is a number. A recommendation that cannot show
 * its working is the same thing as an alert that says STRONG BUY. */
export default function ModelPicker({
  value,
  onChange,
  onAdopt,
  compact = false,
}: {
  /** The person's model, or "" until the catalogue has said what it is. */
  value: string;
  onChange: (slug: string) => void;
  /** What the server says is answering, when that differs from what was shown.
      Separate from `onChange` because adopting is not choosing: it must not
      write the value back, which would restart the clock on an expiry that had
      just run out. */
  onAdopt: (slug: string) => void;
  /** The palette is narrow; the Steven page is not. */
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [cursor, setCursor] = useState(0);
  /* Which way the menu opens, decided when it opens rather than fixed in CSS.
     The two surfaces sit at opposite ends of the window — the Steven page's
     composer is pinned to the bottom, the palette's chip row is near the top —
     so a single direction is off-screen on one of them. Measured rather than
     passed in as a prop, because it is a fact about where the trigger happens
     to be on this render, not about which surface is hosting it. */
  const [drop, setDrop] = useState<"up" | "down">("up");
  /* A counter rather than a flag, so choosing twice runs the confirmation twice.
     A CSS animation only restarts when its element is new, and incrementing this
     is what gives the sweep a fresh `key`. */
  const [confirmed, setConfirmed] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  /* Loaded on mount, not on first open.
     
     It used to wait for a click, on the grounds that most visits never touch
     this control. That stopped being true when the chip started naming the
     model Steven resolves to: the closed trigger now displays catalogue data,
     so deferring the fetch means the chip reads "Steven" with nothing beside it
     until somebody opens the menu — which is precisely the state where nobody
     knows what they are paying for. One cached request per page load is the
     cheaper mistake. */
  useEffect(() => {
    if (catalogue) return;
    let live = true;
    void loadCatalogue(value).then((loaded) => {
      if (!live) return;
      setCatalogue(loaded ?? soleRow(value || "the configured model"));
      /* The server is the authority on which model is answering, so its
         `current` is adopted rather than compared. Until this lands the browser
         has no way to know what a choice made in Discord — or an expiry that
         happened overnight — left you on. */
      if (loaded?.current && loaded.current !== value) onAdopt(loaded.current);
    });
    return () => {
      live = false;
    };
  }, [catalogue, value, onAdopt]);

  /* Memoised because `?? []` is a new array on every render, and two effects
     below depend on it — without this they tear down and re-add a keydown
     listener on each one. */
  const models = useMemo(() => catalogue?.models ?? [], [catalogue]);
  const selected = models.find((m) => m.slug === value);
  // Only to put a dot beside the row Steven currently lands on, so pinning it
  // by hand is visibly the same choice made the rigid way.
  const recommended = catalogue?.recommended ?? null;

  useEffect(() => {
    if (!open) return;
    const rect = rootRef.current?.getBoundingClientRect();
    if (!rect) return;
    // Whichever side has more room. The menu is taller than either gap on a
    // short window, so this is "least bad" rather than "fits".
    setDrop(rect.top > window.innerHeight - rect.bottom ? "up" : "down");
  }, [open]);

  const choose = useCallback(
    (slug: string) => {
      onChange(slug);
      setOpen(false);
      /* Fired here rather than watched off `value`, because the flourish is
         confirming *your* click. `value` also moves when the server tells the
         picker what it was already on, and lighting up for that would be the
         interface congratulating you on something you did not do. */
      setConfirmed((n) => n + 1);
    },
    [onChange]
  );

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setCursor((c) => {
          const next = e.key === "ArrowDown" ? c + 1 : c - 1;
          return Math.max(0, Math.min(models.length - 1, next));
        });
        return;
      }
      if (e.key === "Enter" && models[cursor]) {
        e.preventDefault();
        choose(models[cursor].slug);
      }
    };
    const onClick = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    // Captured, so a click that lands on something which stops propagation
    // still closes this. A dropdown left open behind another control is the
    // classic version of this bug.
    window.addEventListener("mousedown", onClick, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onClick, true);
    };
  }, [open, models, cursor, choose]);

  /* Keep the keyboard cursor in view. The list scrolls and the highlighted row
     is the only thing telling you where enter would land. */
  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelectorAll(".mp-row")
      [cursor]?.scrollIntoView({ block: "nearest" });
  }, [cursor, open]);

  // Opening on the current selection rather than the top: the first arrow key
  // should move from where you are, not jump to the cheapest thing on the list.
  useEffect(() => {
    if (!open) return;
    const at = models.findIndex((m) => m.slug === value);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reads browser state or starts a load on mount; predates lint in CI, and new code must pass the rule
    setCursor(at >= 0 ? at : 0);
  }, [open, models, value]);

  const onRouter = value === ROUTER;
  const routesTo = catalogue?.router?.label ?? null;
  // The row behind the router, so the chip can price what is actually going to
  // be billed rather than only naming it.
  const routedRow = models.find((m) => m.slug === catalogue?.router?.resolves);
  const triggerLabel = onRouter
    ? ROUTER_LABEL
    : selected?.label ?? (value ? value.split("/").pop() : ROUTER_LABEL);

  return (
    <div className={`mp${compact ? " compact" : ""}`} ref={rootRef}>
      <button
        className={`mp-trigger${open ? " on" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Which model answers"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 2l2.4 6.4L21 10l-5.2 4.2L17 21l-5-3.4L7 21l1.2-6.8L3 10l6.6-1.6z" />
        </svg>
        <span className="mp-trigger-name">{triggerLabel}</span>
        {/* On the full page the chip carries all three: whose choice it is,
            the model that choice currently lands on, and what that costs.
            "Steven" alone reads as a mode with no price attached, which is how
            somebody ends up not knowing what they are paying for, and the model
            underneath can change without anybody touching this menu.

            In the palette it is the name alone. That row also holds the chip
            saying what Steven can see, and three pieces of copy here pushed it
            down to "Magpi…" — which is the chip losing the only thing it exists
            to say. The menu is one click away and shows all of it. */}
        {!compact && onRouter && routesTo && (
          <span className="mp-trigger-via">{routesTo}</span>
        )}
        {!compact && (
          <span className="mp-trigger-price mono">
            {onRouter
              ? routedRow && price(routedRow.input_per_m)
              : selected && price(selected.input_per_m)}
          </span>
        )}
        <motion.svg
          width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
          animate={{ rotate: open ? 180 : 0 }}
          transition={{ duration: 0.22, ease: EASE }}
        >
          <path d="M6 9l6 6 6-6" />
        </motion.svg>
        {/* One lap of light around the chip when a choice lands. The menu has
            already closed by then, so without it the only evidence anything
            happened is a word quietly changing behind the click. Keyed on the
            counter so a second choice re-runs it, and removed by `onAnimationEnd`
            rather than a timer, so the element's life and the animation's are
            the same thing. */}
        {confirmed > 0 && (
          <span
            key={confirmed}
            className="mp-sweep"
            aria-hidden="true"
            onAnimationEnd={() => setConfirmed(0)}
          />
        )}
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            className={`mp-menu ${drop}`}
            role="listbox"
            initial={{ opacity: 0, y: drop === "up" ? 8 : -8, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: drop === "up" ? 6 : -6, scale: 0.98 }}
            transition={{ duration: 0.2, ease: EASE }}
          >
            {/* The same lap of copper as the chip, around the menu as it
                arrives. No key is needed: `AnimatePresence` builds this element
                fresh on every open, so the animation runs once each time by
                construction rather than by being reset. */}
            <span className="mp-menu-sweep" aria-hidden="true" />
            {!catalogue ? (
              <div className="mp-loading">
                <span className="mp-dot" />
                <span className="mp-dot" />
                <span className="mp-dot" />
                Reading the catalogue
              </div>
            ) : (
              <>
                {/* Steven, meaning "you choose". Selectable like any other
                    row, and first because it is the answer for almost
                    everybody — what it stores is the rule rather than the
                    model, so it keeps up with the ranking on its own. */}
                <motion.button
                  className={`mp-best${onRouter ? " on" : ""}`}
                  onClick={() => choose(ROUTER)}
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.24, ease: EASE, delay: 0.02 }}
                >
                  <div className="mp-best-head">
                    <span className="mp-best-tag">
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                           strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                        <path d="M12 2l2.4 6.4L21 10l-5.2 4.2L17 21l-5-3.4L7 21l1.2-6.8L3 10l6.6-1.6z" />
                      </svg>
                      {ROUTER_LABEL}
                    </span>
                    <span className="mp-best-name">Best value, automatically</span>
                    {onRouter && (
                      <span className="mp-best-using">
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
                             stroke="currentColor" strokeWidth="3" strokeLinecap="round"
                             strokeLinejoin="round" aria-hidden="true">
                          <path d="M20 6L9 17l-5-5" />
                        </svg>
                        In use
                      </span>
                    )}
                  </div>
                  {/* What it comes out as today, and the server's own sentence
                      for why — not one assembled here, or the button could
                      claim something the ranking did not conclude. Naming the
                      model matters: "automatic" with nothing under it is how
                      people end up not knowing what they are paying for. */}
                  <p className="mp-best-why">
                    {catalogue?.router?.resolves
                      ? <>Currently <strong>{catalogue.router.label}</strong> — {catalogue.router.why}</>
                      : "Follows the ranking. No model qualifies right now, so this falls back to the configured one."}
                  </p>
                </motion.button>

                <div className="mp-head">
                  <span>Or pin one</span>
                  <span className="mp-head-cols">
                    {INDICES.map((index) => (
                      <span key={index} title={`${INDEX_LABELS[index]} percentile`}>
                        {INDEX_LABELS[index]}
                      </span>
                    ))}
                    <span className="mp-head-price">$/M in</span>
                  </span>
                </div>

                <div className="mp-list" ref={listRef}>
                  {models.map((model, i) => (
                    <Row
                      key={model.slug}
                      model={model}
                      i={i}
                      current={model.slug === value}
                      cursor={i === cursor}
                      best={model.slug === recommended?.slug}
                      onPick={() => choose(model.slug)}
                      onHover={() => setCursor(i)}
                    />
                  ))}
                </div>

                {/* What the ranking is, in one line, under the list that used
                    it. Not a tooltip: someone deciding between two rows is
                    exactly the person who needs to know what "value" meant. */}
                <p className="mp-foot">
                  Ranked by capability percentile per dollar, weighted for tool
                  use. Prices and benchmarks are OpenRouter&rsquo;s, read live.
                  Your choice answers you in Discord too, and a pinned model
                  lapses back to {ROUTER_LABEL} after a day.
                </p>
              </>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Row({
  model,
  i,
  current,
  cursor,
  best,
  onPick,
  onHover,
}: {
  model: ModelRow;
  i: number;
  current: boolean;
  cursor: boolean;
  best: boolean;
  onPick: () => void;
  onHover: () => void;
}) {
  return (
    <motion.button
      className={`mp-row${current ? " on" : ""}${cursor ? " cursor" : ""}`}
      role="option"
      aria-selected={current}
      onClick={onPick}
      onMouseEnter={onHover}
      initial={{ opacity: 0, y: 5 }}
      animate={{ opacity: 1, y: 0 }}
      /* Staggered, but capped: past a dozen rows the delay stops reading as one
         list arriving and starts reading as a slow list. */
      transition={{ duration: 0.2, ease: EASE, delay: Math.min(i, 12) * 0.018 }}
    >
      <span className="mp-row-id">
        <span className="mp-row-name">
          {model.label}
          {best && <span className="mp-pip-best" title="Recommended" />}
        </span>
        <small>
          {model.author}
          {model.context > 0 && ` · ${context(model.context)}`}
        </small>
      </span>
      <span className="mp-row-cols">
        {INDICES.map((index) => (
          <Meter key={index} value={model.percentiles[index as Index]} />
        ))}
        <span className="mp-row-price mono">{price(model.input_per_m)}</span>
      </span>
      {current && (
        <motion.span
          className="mp-check"
          layoutId="mp-check"
          transition={{ duration: 0.22, ease: EASE }}
          aria-hidden="true"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 6L9 17l-5-5" />
          </svg>
        </motion.span>
      )}
    </motion.button>
  );
}

/* One percentile as a bar, or a dash where nobody has measured.
 *
 * The dash matters more than the bar. A model with no agentic benchmark is not
 * a model that scored zero at tool use, and drawing an empty bar for it would
 * say exactly that — the same reason the screener keeps "absent" and "zero"
 * apart everywhere else. */
function Meter({ value }: { value: number | null }) {
  if (value === null) {
    return (
      <span className="mp-meter none" title="Not measured">
        –
      </span>
    );
  }
  return (
    <span className="mp-meter" title={`Beats ${Math.round(value)}% of the field`}>
      <motion.span
        className="mp-meter-fill"
        initial={{ scaleX: 0 }}
        animate={{ scaleX: value / 100 }}
        transition={{ duration: 0.5, ease: EASE }}
        style={{ originX: 0 }}
      />
      <span className="mp-meter-num mono">{Math.round(value)}</span>
    </span>
  );
}
