"use client";

import { useEffect, useState } from "react";

/* Who is signed in, and which build is serving them.
 *
 * Both come from /status, which already reported them and which nothing in the
 * dashboard was asking. The rail printed a login and a commit as literals in
 * the source instead — so it read "66371b6" while the bot in Discord reported
 * a different build entirely, and it would have greeted anyone who signed in
 * by somebody else's name.
 *
 * Fetched once per mount. It is two short strings that change when the
 * container is replaced, and the rail is mounted for as long as the tab is
 * open, so polling would be a request a minute to learn nothing. */

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export type Session = {
  login: string | null;
  gitSha: string | null;
  /** False until the first answer, so nothing renders a placeholder as fact. */
  loaded: boolean;
};

export function useSession(): Session {
  const [session, setSession] = useState<Session>({
    login: null,
    gitSha: null,
    loaded: false,
  });

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const response = await fetch(`${BASE}/status`, {
          credentials: "include",
          cache: "no-store",
        });
        if (!response.ok) throw new Error(String(response.status));
        const body = await response.json();
        // Guarded against unmount: /status queries the session table, so on a
        // slow database this can land after a navigation.
        if (live) {
          setSession({
            login: body.login ?? null,
            gitSha: body.git_sha ?? null,
            loaded: true,
          });
        }
      } catch {
        // Signed out, or the database is unreachable. Either way there is
        // nothing to show and inventing something is worse than a blank.
        if (live) setSession((s) => ({ ...s, loaded: true }));
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  return session;
}

/* The pseudo-login /auth/local issues. It is not a GitHub account — except
   that it is: somebody holds github.com/local-dev, so asking for their picture
   put a stranger's face in the rail during local development. */
const LOCAL_LOGIN = "local-dev";

/** Their GitHub picture. `null` when there is no GitHub account behind it. */
export function avatarUrl(login: string | null): string | null {
  if (!login || login === LOCAL_LOGIN) return null;
  return `https://github.com/${encodeURIComponent(login)}.png?size=64`;
}

/** Two letters, for before the picture loads or when there is not one. */
export function initials(login: string | null): string {
  return (login ?? "?").slice(0, 2);
}
