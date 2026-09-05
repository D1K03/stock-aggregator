"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useState } from "react";
import type { ToolRun } from "@/lib/threads";

const EASE = [0, 0, 0.2, 1] as const;

/* What Steven consulted before answering.
 *
 * Collapsed to one line by default, because the answer is the point and the
 * tools are the working. Expanded it lists each call and how long it took.
 *
 * The rows stagger in, and it is worth being clear about what that is: the
 * request is not streamed, so these arrive together with the reply. The
 * stagger is a reveal of things that really ran, in the order they ran, not a
 * live feed pretending to be one. */
export default function ToolTrace({ tools }: { tools: ToolRun[] }) {
  const [open, setOpen] = useState(false);
  if (tools.length === 0) return null;

  const total = tools.reduce((sum, t) => sum + t.ms, 0);

  return (
    <div className="trace">
      <button className="trace-head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <motion.span
          className="trace-caret"
          animate={{ rotate: open ? 90 : 0 }}
          transition={{ duration: 0.18, ease: EASE }}
        >
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round">
            <path d="M9 5l7 7-7 7" />
          </svg>
        </motion.span>
        <span className="trace-dot" />
        {tools.length === 1 ? "Used 1 tool" : `Used ${tools.length} tools`}
        <span className="trace-ms">{total}ms</span>
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.ul
            className="trace-list"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: EASE }}
          >
            {tools.map((tool, i) => (
              <motion.li
                key={`${tool.name}-${i}`}
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.06, duration: 0.22, ease: EASE }}
              >
                <code>{tool.name}</code>
                <span>{tool.ms}ms</span>
              </motion.li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}
