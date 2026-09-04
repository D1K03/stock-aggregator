"use client";

import { motion } from "framer-motion";

const LINKS = ["Overview", "Universe", "Alerts", "Runs", "Weights"];

export default function Nav() {
  return (
    <nav className="top">
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
          <span className="user"><span className="avatar">eh</span>ehewes</span>
        </div>
      </div>
    </nav>
  );
}
