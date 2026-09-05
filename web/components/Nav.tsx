"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";

/* The status service owns the session: it deletes the row, clears the cookie
   and redirects to /login. Same origin behind Caddy, so a relative href is the
   entire integration. */
const LOGOUT = process.env.NEXT_PUBLIC_API_BASE
  ? `${process.env.NEXT_PUBLIC_API_BASE}/auth/logout`
  : "/auth/logout";

const LINKS = ["Overview", "Universe", "Alerts", "Runs", "Weights"];

export default function Nav() {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <nav className="top" ref={menuRef}>
      <div className="wrap nav-in">
        <motion.span
          className="wordmark"
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: [0, 0, 0.2, 1] }}
        >
          Screener
        </motion.span>
        <div className="nav-links">
          {LINKS.map((l, i) => (
            <motion.a
              key={l}
              href="#"
              className={i === 0 ? "on" : undefined}
              initial={{ opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.05 + i * 0.05, duration: 0.4 }}
            >
              {l}
            </motion.a>
          ))}
        </div>
        <div className="nav-right">
          <span className="sha" title="scoring_run.git_sha">66371b6</span>
          <div className="user-menu">
            <button
              className="user"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              aria-haspopup="menu"
            >
              <span className="avatar">eh</span>
              ehewes
              <motion.svg
                width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"
                animate={{ rotate: open ? 180 : 0 }}
                transition={{ duration: 0.2, ease: [0, 0, 0.2, 1] }}
              >
                <path d="M2 4l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
              </motion.svg>
            </button>
            <AnimatePresence>
              {open && (
                <motion.div
                  className="menu"
                  role="menu"
                  initial={{ opacity: 0, y: -6, scale: 0.98 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: -6, scale: 0.98 }}
                  transition={{ duration: 0.16, ease: [0, 0, 0.2, 1] }}
                >
                  <div className="menu-head">
                    Signed in as <b>ehewes</b>
                  </div>
                  {/* A plain link, not fetch(): the status service clears the
                      cookie and redirects, and letting the browser follow that
                      is the whole of it. */}
                  <a className="menu-item danger" href={LOGOUT} role="menuitem">
                    Sign out
                  </a>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </div>
    </nav>
  );
}
