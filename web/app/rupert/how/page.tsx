"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import DiagramTools from "@/components/DiagramTools";
import Sidebar from "@/components/Sidebar";
import { HOW_RUPERT_WORKS, type HowSection } from "@/lib/how-rupert";
import { RUPERT_VERSION } from "@/lib/rupert-version";

const EASE = [0, 0, 0.2, 1] as const;

/* How Rupert works, drawn rather than described.
 *
 * A route in the app rather than a link out, because the diagrams explain a
 * page you are standing on and leaving the dashboard to read them is a worse
 * answer than a back button. The same strings render on GitHub in
 * `docs/rupert.md`, and a test keeps the two in step.
 *
 * **mermaid is imported inside the effect, never at module scope.** It is the
 * largest dependency in this app by some way, and at module scope Next would
 * put it in the shared chunk that every page loads — so the Overview would pay
 * for a diagram library it never draws. Imported here it lands in this route's
 * own chunk and is fetched the first time somebody asks how this works. */

function bold(text: string, key: number) {
  // The doc is markdown and this is the one piece of it worth carrying over:
  // **emphasis** marks the sentence in each section that is the argument.
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return (
    <p key={key}>
      {parts.map((part, i) =>
        part.startsWith("**") ? (
          <strong key={i}>{part.slice(2, -2)}</strong>
        ) : part.startsWith("`") ? (
          <code key={i}>{part.slice(1, -1)}</code>
        ) : (
          part
        )
      )}
    </p>
  );
}

function Section({ section, index }: { section: HowSection; index: number }) {
  const host = useRef<HTMLDivElement>(null);
  return (
    <motion.section
      className="card rup-card how-section"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.06 + index * 0.05, duration: 0.45, ease: EASE }}
    >
      <h2>{section.title}</h2>
      {section.diagram && (
        <>
          {/* Above the diagram, not below it. The walk lights up nodes *in* the
              picture, so controls underneath mean pressing a chip and then
              scrolling back up to watch what it did — and the decision flowchart
              is tall enough that you would miss the first two steps.
              The walks only make sense on that one; the others have no path. */}
          <DiagramTools host={host} walkable={index === 0} />
          <div className="how-diagram" ref={host}>
            <pre className="mermaid">{section.diagram}</pre>
          </div>
        </>
      )}
      <div className="how-prose">
        {section.paragraphs.map((p, n) => bold(p, n))}
      </div>
    </motion.section>
  );
}

export default function HowRupertWorks() {
  const root = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          theme: "base",
          themeVariables: {
            fontFamily: "var(--font-geist-sans), Geist, system-ui, sans-serif",
            fontSize: "13px",
            primaryColor: "#ffffff",
            primaryTextColor: "#1a1817",
            primaryBorderColor: "#e5e7eb",
            lineColor: "#77716c",
            secondaryColor: "#f8f8f6",
            tertiaryColor: "#fffbeb",
          },
        });
        if (cancelled || !root.current) return;
        await mermaid.run({ nodes: root.current.querySelectorAll<HTMLElement>(".mermaid") });
      } catch {
        // A diagram library that will not load should cost the diagrams and not
        // the page: the prose around them still says what happens.
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="shell">
      <Sidebar active="Rupert" />
      <div className="content">
        <div className="wrap" ref={root}>
          <motion.header
            className="hero"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: EASE }}
          >
            <Link className="rup-back" href="/rupert">
              ← Rupert
            </Link>
            <div className="rup-title">
              <h1>How Rupert works</h1>
              {/* Which Rupert. The diagrams describe one version of the
                  decision, and a reader has to be able to tell whether it is
                  the one running. */}
              <span className="how-version" title={RUPERT_VERSION.note}>
                {RUPERT_VERSION.version}
                <small>since {RUPERT_VERSION.released}</small>
              </span>
            </div>
            <p>
              A regex shortlists candidate symbols out of a text and refuses to
              choose between them; a decision model that has the sentence in
              front of it picks which one — if any — the text is actually about;
              FinBERT reads what resolved. Everything below was measured on the
              live corpus, not estimated.
            </p>
          </motion.header>

          {failed && (
            <p className="rup-note">
              The diagrams could not be drawn, but the words below still describe
              what happens.
            </p>
          )}

          {HOW_RUPERT_WORKS.map((section, i) => (
            <Section key={section.title} section={section} index={i} />
          ))}

          <p className="how-foot">
            The same diagrams live in <code>docs/rupert.md</code>, which renders
            on GitHub. A test fails if the two ever stop matching.
          </p>
        </div>
      </div>
    </div>
  );
}
