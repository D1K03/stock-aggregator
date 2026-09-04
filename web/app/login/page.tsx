"use client";

import { motion } from "framer-motion";

/* The real flow lives on the status service: /auth/login sends you to GitHub,
   /auth/callback checks the login against ALLOWED_GITHUB_LOGINS and sets the
   session cookie. Served same-origin behind the tunnel, so a relative href is
   the whole integration. */
const AUTH_LOGIN = process.env.NEXT_PUBLIC_API_BASE
  ? `${process.env.NEXT_PUBLIC_API_BASE}/auth/login`
  : "/auth/login";

function GithubMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.53 7.53 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

export default function Login() {
  return (
    <div className="login-shell">
      <motion.div
        className="login-card"
        initial={{ opacity: 0, y: 16, scale: 0.985 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.55, ease: [0, 0, 0.2, 1] }}
      >
        <span className="wordmark">Screener</span>
        <h1>Sign in to the status service</h1>
        <p className="lede">
          Transparent, sector-relative pillar scores. Alerts fire on threshold
          crossings — never a recommendation.
        </p>
        <motion.a
          className="gh-btn"
          href={AUTH_LOGIN}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.25, duration: 0.45, ease: [0, 0, 0.2, 1] }}
        >
          <GithubMark />
          Continue with GitHub
        </motion.a>
        <p className="login-note">
          Access is limited to named GitHub accounts. Nothing else can sign in,
          and no scopes are requested — the only question asked is who you are.
        </p>
        <div className="login-foot">
          run <span className="mono">66371b6</span> · sessions live in their own
          schema · rotate the secret to sign everyone out
        </div>
      </motion.div>
    </div>
  );
}
