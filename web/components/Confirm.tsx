"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef } from "react";

const EASE = [0, 0, 0.2, 1] as const;

/* Ask before doing something that cannot be undone.
 *
 * `window.confirm` is what the rest of this dashboard has used, and it is the
 * one control on the page that is not ours: it renders in the browser's chrome,
 * ignores every token in `globals.css`, and on some platforms is a sheet that
 * slides out of the address bar. For a destructive action — the one moment the
 * interface most needs to look like it knows what it is doing — that is the
 * wrong thing to hand over.
 *
 * Cancel is focused rather than the destructive button, so a return pressed out
 * of habit does nothing. Escape closes, the scrim closes, and the confirming
 * button is the only path to the action. */
export default function Confirm({
  open,
  title,
  body,
  confirmLabel = "Delete",
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body?: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    // After the entrance, so focus lands once the dialog is actually there.
    const focus = setTimeout(() => cancelRef.current?.focus(), 80);
    return () => {
      window.removeEventListener("keydown", onKey);
      clearTimeout(focus);
    };
  }, [open, onCancel]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            className="ask-scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            onClick={onCancel}
          />
          <motion.div
            className="ask"
            role="dialog"
            aria-modal="true"
            aria-label={title}
            initial={{ opacity: 0, y: -10, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -6, scale: 0.98 }}
            transition={{ duration: 0.26, ease: EASE }}
          >
            <h2>{title}</h2>
            {body && <p>{body}</p>}
            <div className="ask-acts">
              <button ref={cancelRef} className="ask-no" onClick={onCancel}>
                No, keep it
              </button>
              <button className="ask-yes" onClick={onConfirm}>
                {confirmLabel}
              </button>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
