"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { avatarUrl, initials, useSession } from "@/lib/session";

const LOGOUT = process.env.NEXT_PUBLIC_API_BASE
  ? `${process.env.NEXT_PUBLIC_API_BASE}/auth/logout`
  : "/auth/logout";

/* Bounded, not free. Discord lets you drag its sidebar but only between two
   sensible widths, because a 40px or 900px sidebar is never what anyone meant.
   Below MIN the labels would clip; above MAX the content column starts losing
   table columns. */
const MIN_WIDTH = 200;
const MAX_WIDTH = 360;
const DEFAULT_WIDTH = 248;
const COLLAPSED_WIDTH = 64;

const STORAGE_WIDTH = "screener.sidebar.width";
const STORAGE_COLLAPSED = "screener.sidebar.collapsed";

/* Below this the rail cannot be expanded: a 200px sidebar on a phone leaves no
   room for what it navigates to. It is the same number as the CSS breakpoint
   and has to be, because the CSS pins the width while this decides what goes
   inside it — the two disagreeing is how "Screener" ended up spilling out of a
   64px rail. */
const NARROW = "(max-width: 860px)";
/* And below this it is not a rail at all: it moves to the bottom of the screen
   as a tab bar, so the content gets the full width. */
const PHONE = "(max-width: 760px)";

type Item = { label: string; href: string; icon: React.ReactNode; soon?: boolean };

function Icon({ d }: { d: string }) {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

/* `soon` marks a destination that does not exist yet. It stays listed, because
   the shape of the thing is worth showing, but it is rendered as text rather
   than a link — a nav item that looks clickable and does nothing is worse than
   one that says it is not ready. */
const ITEMS: Item[] = [
  { label: "Overview", href: "/", icon: <Icon d="M3 12h5l2 6 4-14 2 8h5" /> },
  { label: "Steven", href: "/steven", icon: <Icon d="M21 12a9 9 0 01-9 9 9 9 0 01-4-1l-5 1 1-5a9 9 0 01-1-4 9 9 0 019-9 9 9 0 019 9z" /> },
  { label: "Audit", href: "/audit", icon: <Icon d="M4 4h11l5 5v11H4zM15 4v5h5M8 13h8M8 17h5" /> },
  { label: "Universe", href: "#", soon: true, icon: <Icon d="M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18" /> },
  { label: "Alerts", href: "#", soon: true, icon: <Icon d="M18 8a6 6 0 10-12 0c0 7-3 8-3 8h18s-3-1-3-8M13.7 21a2 2 0 01-3.4 0" /> },
  { label: "Runs", href: "#", soon: true, icon: <Icon d="M12 8v4l3 2M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /> },
];

export default function Sidebar({ active = "Overview" }: { active?: string }) {
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const [collapsed, setCollapsed] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [ready, setReady] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [narrow, setNarrow] = useState(false);
  const [phone, setPhone] = useState(false);
  // github.com serves a picture for any login without an API call, but not for
  // one that does not exist — `local-dev` among them — so a failed load falls
  // back to initials rather than leaving a broken image in the rail.
  const [pictureFailed, setPictureFailed] = useState(false);
  const { login, gitSha, loaded } = useSession();
  const menuRef = useRef<HTMLDivElement>(null);

  /* Read once on mount rather than during render: the server has no
     localStorage, and reading it while rendering would hydrate to a different
     width than the server sent. `ready` suppresses the transition for that
     first paint so a restored width does not animate in from the default. */
  useEffect(() => {
    const saved = Number(localStorage.getItem(STORAGE_WIDTH));
    if (saved >= MIN_WIDTH && saved <= MAX_WIDTH) setWidth(saved);
    setCollapsed(localStorage.getItem(STORAGE_COLLAPSED) === "1");
    setReady(true);
  }, []);

  /* Watched rather than measured once: a tablet rotating crosses both of these
     without reloading, and a rail that only checked at mount would keep the
     shape of the orientation it was born in. */
  useEffect(() => {
    const queries = [
      [window.matchMedia(NARROW), setNarrow] as const,
      [window.matchMedia(PHONE), setPhone] as const,
    ];
    const stops = queries.map(([query, set]) => {
      const sync = () => set(query.matches);
      sync();
      query.addEventListener("change", sync);
      return () => query.removeEventListener("change", sync);
    });
    return () => stops.forEach((stop) => stop());
  }, []);

  // Narrow means collapsed whatever the stored preference says, so the labels
  // and the wordmark match the width the stylesheet is pinning.
  const shut = collapsed || narrow;
  // Labels are dropped when the rail is a 64px column, but a bottom bar has
  // room for a small one under each icon — and a row of bare icons is a
  // guessing game.
  const labelled = !shut || phone;

  const startDrag = useCallback(
    (event: React.PointerEvent) => {
      if (shut) return;
      event.preventDefault();
      setDragging(true);
      const onMove = (e: PointerEvent) => {
        const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, e.clientX));
        setWidth(next);
      };
      const onUp = (e: PointerEvent) => {
        const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, e.clientX));
        localStorage.setItem(STORAGE_WIDTH, String(next));
        setDragging(false);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [shut]
  );

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(STORAGE_COLLAPSED, next ? "1" : "0");
  };

  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenuOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const shown = shut ? COLLAPSED_WIDTH : width;
  const picture = pictureFailed ? null : avatarUrl(login);

  return (
    <>
      {/* The rail is fixed, so a spacer keeps the content column off it. Both
          read the same width, so they cannot drift apart mid-drag. */}
      {/* On a phone the rail is a bar along the bottom, so it takes no width
          and the spacer would only push the content sideways. */}
      <div
        className="rail-spacer"
        style={{ width: phone ? 0 : shown }}
        aria-hidden="true"
      />

      <aside
        className={`rail${shut ? " collapsed" : ""}${phone ? " bar" : ""}${dragging ? " dragging" : ""}`}
        style={{ width: phone ? undefined : shown, transition: ready && !dragging ? undefined : "none" }}
      >
        <div className="rail-head">
          <span className="wordmark">{shut ? "S" : "Screener"}</span>
          <button
            className="rail-toggle"
            onClick={toggle}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand" : "Collapse"}
          >
            <motion.svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                        stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                        animate={{ rotate: collapsed ? 180 : 0 }}
                        transition={{ duration: 0.22, ease: [0, 0, 0.2, 1] }}>
              <path d="M15 6l-6 6 6 6" />
            </motion.svg>
          </button>
        </div>

        <nav className="rail-nav">
          {ITEMS.map((item) =>
            item.soon ? (
              <span
                key={item.label}
                className="soon"
                title={`${item.label} — not built yet`}
                aria-disabled="true"
              >
                <span className="rail-icon">{item.icon}</span>
                {labelled && <span className="rail-label">{item.label}</span>}
              </span>
            ) : (
              <Link
                key={item.label}
                href={item.href}
                className={item.label === active ? "on" : undefined}
                title={shut ? item.label : undefined}
                aria-label={item.label}
              >
                <span className="rail-icon">{item.icon}</span>
                {labelled && <span className="rail-label">{item.label}</span>}
              </Link>
            )
          )}
        </nav>

        <div className="rail-foot" ref={menuRef}>
          {/* Nothing until it is known. A placeholder commit here is what made
              the rail disagree with the build the bot reports. */}
          {!shut && gitSha && (
            <span className="sha" title={`Running ${gitSha}`}>{gitSha.slice(0, 7)}</span>
          )}
          <button className="rail-user" onClick={() => setMenuOpen((v) => !v)} aria-expanded={menuOpen}>
            {picture ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                className="avatar as-picture"
                src={picture}
                alt=""
                width={26}
                height={26}
                onError={() => setPictureFailed(true)}
              />
            ) : (
              <span className="avatar">{initials(login)}</span>
            )}
            {!shut && <span className="rail-label">{login ?? (loaded ? "signed out" : "…")}</span>}
          </button>
          {menuOpen && (
            <motion.div
              className="menu rail-menu"
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.16 }}
            >
              <div className="menu-head">
                Signed in as <b>{login ?? "nobody"}</b>
              </div>
              <a className="menu-item danger" href={LOGOUT}>Sign out</a>
            </motion.div>
          )}
        </div>

        {/* Hit area is wider than the visible line, because a 1px target is a
            fiddly thing to grab. */}
        {!shut && (
          <div
            className="rail-grip"
            onPointerDown={startDrag}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize sidebar"
          />
        )}
      </aside>
    </>
  );
}
