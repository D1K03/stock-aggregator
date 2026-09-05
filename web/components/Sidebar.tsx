"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

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

type Item = { label: string; href: string; icon: React.ReactNode };

function Icon({ d }: { d: string }) {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

const ITEMS: Item[] = [
  { label: "Overview", href: "/", icon: <Icon d="M3 12h5l2 6 4-14 2 8h5" /> },
  { label: "Steven", href: "/steven", icon: <Icon d="M21 12a9 9 0 01-9 9 9 9 0 01-4-1l-5 1 1-5a9 9 0 01-1-4 9 9 0 019-9 9 9 0 019 9z" /> },
  { label: "Audit", href: "/audit", icon: <Icon d="M4 4h11l5 5v11H4zM15 4v5h5M8 13h8M8 17h5" /> },
  { label: "Universe", href: "#", icon: <Icon d="M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18" /> },
  { label: "Alerts", href: "#", icon: <Icon d="M18 8a6 6 0 10-12 0c0 7-3 8-3 8h18s-3-1-3-8M13.7 21a2 2 0 01-3.4 0" /> },
  { label: "Runs", href: "#", icon: <Icon d="M12 8v4l3 2M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /> },
];

export default function Sidebar({ active = "Overview" }: { active?: string }) {
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const [collapsed, setCollapsed] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [ready, setReady] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
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

  const startDrag = useCallback(
    (event: React.PointerEvent) => {
      if (collapsed) return;
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
    [collapsed]
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

  const shown = collapsed ? COLLAPSED_WIDTH : width;

  return (
    <>
      {/* The rail is fixed, so a spacer keeps the content column off it. Both
          read the same width, so they cannot drift apart mid-drag. */}
      <div className="rail-spacer" style={{ width: shown }} aria-hidden="true" />

      <aside
        className={`rail${collapsed ? " collapsed" : ""}${dragging ? " dragging" : ""}`}
        style={{ width: shown, transition: ready && !dragging ? undefined : "none" }}
      >
        <div className="rail-head">
          <span className="wordmark">{collapsed ? "S" : "Screener"}</span>
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
          {ITEMS.map((item) => (
            <Link
              key={item.label}
              href={item.href}
              className={item.label === active ? "on" : undefined}
              title={collapsed ? item.label : undefined}
            >
              <span className="rail-icon">{item.icon}</span>
              {!collapsed && <span className="rail-label">{item.label}</span>}
            </Link>
          ))}
        </nav>

        <div className="rail-foot" ref={menuRef}>
          {!collapsed && <span className="sha" title="scoring_run.git_sha">66371b6</span>}
          <button className="rail-user" onClick={() => setMenuOpen((v) => !v)} aria-expanded={menuOpen}>
            <span className="avatar">eh</span>
            {!collapsed && <span className="rail-label">ehewes</span>}
          </button>
          {menuOpen && (
            <motion.div
              className="menu rail-menu"
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.16 }}
            >
              <div className="menu-head">Signed in as <b>ehewes</b></div>
              <a className="menu-item danger" href={LOGOUT}>Sign out</a>
            </motion.div>
          )}
        </div>

        {/* Hit area is wider than the visible line, because a 1px target is a
            fiddly thing to grab. */}
        {!collapsed && (
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
