"use client";

import { useRef, useState } from "react";
import { type Walk, WALKS } from "@/lib/rupert-walk";

/* The toolbar under a diagram: take it away as a picture, or watch a decision
 * walk down it.
 *
 * **Nothing here touches the diagram.** The walk adds and removes classes on the
 * rendered SVG's nodes and the export serialises a clone — the mermaid source in
 * `docs/rupert.md` is the single definition and stays untouched, which is what
 * lets the doc, this page and the export never disagree.
 *
 * The backgrounds exist because a transparent PNG is what you want in a slide
 * and the worst possible thing to paste into a dark chat client, where the ink
 * vanishes. Three explicit choices beat one that is right a third of the time.
 */

const BACKGROUNDS = {
  transparent: { label: "transparent", fill: null as string | null, ink: null },
  light: { label: "light", fill: "#ffffff", ink: null },
  dark: { label: "dark", fill: "#1a1817", ink: "#f0efeb" },
} as const;

type Background = keyof typeof BACKGROUNDS;

// How long each node is lit for. Slow enough to read the label, fast enough
// that a ten-node walk is not a coffee break.
const STEP_MS = 850;
// The GIF holds each step for slightly longer, because a reader cannot scrub it.
const GIF_STEP_MS = 1000;
const SCALE = 2;

/* Mermaid wraps every node id in its own render id and a per-node counter:
   `mermaid-1789921840749-flowchart-unsure-21` for the node `unsure`. Both the
   timestamp and the counter change between renders, so the name has to be cut
   out of the middle rather than matched whole or by prefix.

   Measured against a real render, not guessed. A first attempt stripped only
   `flowchart-`, left the timestamp on, matched nothing, and dimmed the entire
   diagram at every step — which looks like a deliberate animation until you
   notice nothing is ever lit. */
const NODE_NAME = /-flowchart-(.+)-\d+$/;

function svgOf(host: HTMLElement | null): SVGSVGElement | null {
  return host?.querySelector("svg") ?? null;
}

/** The rendered SVG as a PNG blob, at `SCALE`, over the chosen background. */
async function toPng(
  svg: SVGSVGElement,
  background: Background
): Promise<Blob | null> {
  const clone = svg.cloneNode(true) as SVGSVGElement;
  const box = svg.getBoundingClientRect();
  const w = Math.max(1, Math.round(box.width));
  const h = Math.max(1, Math.round(box.height));
  clone.setAttribute("width", String(w));
  clone.setAttribute("height", String(h));

  const chosen = BACKGROUNDS[background];
  if (chosen.ink) {
    // Dark mode is a filter rather than a restyle: mermaid writes its colours
    // into a <style> block inside the SVG and rewriting them by hand would mean
    // knowing every selector it emits. Inverting and rotating the hue back is
    // one line and survives mermaid changing its mind.
    clone.style.filter = "invert(1) hue-rotate(180deg)";
  }

  const markup = new XMLSerializer().serializeToString(clone);
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
  const image = new Image();
  image.width = w;
  image.height = h;
  await new Promise<void>((res, rej) => {
    image.onload = () => res();
    image.onerror = () => rej(new Error("svg would not rasterise"));
    image.src = url;
  });

  const canvas = document.createElement("canvas");
  canvas.width = w * SCALE;
  canvas.height = h * SCALE;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  if (chosen.fill) {
    ctx.fillStyle = chosen.fill;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
  return new Promise((res) => canvas.toBlob((b) => res(b), "image/png"));
}

export default function DiagramTools({
  host,
  walkable,
}: {
  /** The element the diagram was rendered into. */
  host: React.RefObject<HTMLDivElement | null>;
  /** Only the decision diagram has walks; the others get the export only. */
  walkable: boolean;
}) {
  const [background, setBackground] = useState<Background>("transparent");
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [walk, setWalk] = useState<Walk | null>(null);
  const [step, setStep] = useState(-1);
  const timer = useRef<number | null>(null);

  const say = (text: string) => {
    setNote(text);
    window.setTimeout(() => setNote((n) => (n === text ? null : n)), 2600);
  };

  /* Everything below is a plain function rather than a `useCallback`. The React
     Compiler memoizes this component itself, and a manual dependency list that
     disagrees with the one it infers — `[host]` against `host.current` here —
     makes it skip optimising the component *entirely*. Hand-written memoization
     that costs you the compiler is a worse trade than none at all. */

  /* Light one node, mark the ones already visited, dim everything else. */
  const paint = (path: string[], upto: number) => {
    const svg = svgOf(host.current);
    if (!svg) return;
    svg.querySelectorAll(".rw-on, .rw-past, .rw-dim").forEach((n) =>
      n.classList.remove("rw-on", "rw-past", "rw-dim")
    );
    if (upto < 0) return;
    for (const node of svg.querySelectorAll<SVGGElement>("g.node")) {
      const id = NODE_NAME.exec(node.id)?.[1] ?? "";
      const at = path.indexOf(id);
      if (at < 0 || at > upto) node.classList.add("rw-dim");
      else if (at === upto) node.classList.add("rw-on");
      else node.classList.add("rw-past");
    }
  };

  const stop = () => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
    setWalk(null);
    setStep(-1);
    paint([], -1);
  };

  const play = (chosen: Walk) => {
    if (timer.current) window.clearInterval(timer.current);
    setWalk(chosen);
    setStep(0);
    paint(chosen.path, 0);
    let at = 0;
    timer.current = window.setInterval(() => {
      at += 1;
      if (at >= chosen.path.length) {
        if (timer.current) window.clearInterval(timer.current);
        timer.current = null;
        return;
      }
      setStep(at);
      paint(chosen.path, at);
    }, STEP_MS);
  };

  const copyPng = async () => {
    const svg = svgOf(host.current);
    if (!svg) return;
    setBusy(true);
    try {
      const blob = await toPng(svg, background);
      if (!blob) throw new Error("no image");
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      say(`Copied as PNG, ${BACKGROUNDS[background].label} background.`);
    } catch {
      // Clipboard image writes need a secure context and a permission some
      // browsers will not give. Falling back to a download means the button
      // always does something rather than failing silently.
      try {
        const blob = await toPng(svg, background);
        if (blob) {
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = `rupert-${background}.png`;
          a.click();
          URL.revokeObjectURL(a.href);
          say("Your browser would not let me use the clipboard, so I downloaded it.");
        }
      } catch {
        say("Could not turn that diagram into an image.");
      }
    } finally {
      setBusy(false);
    }
  };

  const copyGif = async () => {
    const svg = svgOf(host.current);
    if (!svg || !walk) return;
    setBusy(true);
    try {
      const { GIFEncoder, quantize, applyPalette } = await import("gifenc");
      const encoder = GIFEncoder();
      const box = svg.getBoundingClientRect();
      const w = Math.round(box.width);
      const h = Math.round(box.height);

      for (let i = 0; i < walk.path.length; i++) {
        paint(walk.path, i);
        // One rasterise per step. The SVG has to be re-serialised each time
        // because the classes the walk paints are what differ between frames.
        const blob = await toPng(svg, background === "transparent" ? "light" : background);
        if (!blob) continue;
        const bitmap = await createImageBitmap(blob);
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        if (!ctx) break;
        ctx.drawImage(bitmap, 0, 0, w, h);
        const { data } = ctx.getImageData(0, 0, w, h);
        const palette = quantize(data, 256);
        encoder.writeFrame(applyPalette(data, palette), w, h, {
          palette,
          delay: GIF_STEP_MS,
        });
      }
      encoder.finish();
      const gif = new Blob([encoder.bytesView()], { type: "image/gif" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(gif);
      a.download = `rupert-${walk.label.replace(/\s+/g, "-")}.gif`;
      a.click();
      URL.revokeObjectURL(a.href);
      // Downloaded rather than copied: no browser lets a page put image/gif on
      // the clipboard, and a button that claimed to would be lying.
      say("Saved the walk as a GIF.");
      paint(walk.path, step);
    } catch {
      say("Could not build that GIF.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dg-tools">
      <div className="dg-row">
        <button className="dg-btn" onClick={() => void copyPng()} disabled={busy}>
          Copy PNG
        </button>
        <select
          className="dg-select"
          value={background}
          onChange={(e) => setBackground(e.target.value as Background)}
          aria-label="Background for the copied image"
        >
          {Object.entries(BACKGROUNDS).map(([key, v]) => (
            <option key={key} value={key}>
              {v.label} background
            </option>
          ))}
        </select>
        {walkable && walk && (
          <button className="dg-btn" onClick={() => void copyGif()} disabled={busy}>
            Save walk as GIF
          </button>
        )}
        {walkable && walk && (
          <button className="dg-btn ghost" onClick={stop}>
            Clear
          </button>
        )}
      </div>

      {walkable && (
        <>
          <div className="dg-chips">
            <span className="dg-chips-label">Watch it decide:</span>
            {WALKS.map((w) => (
              <button
                key={w.label}
                className={`dg-chip${walk?.label === w.label ? " on" : ""}`}
                onClick={() => play(w)}
                title={w.text}
              >
                {w.label}
              </button>
            ))}
          </div>
          {walk && (
            <div className={`dg-walk ends-${walk.ends}`}>
              <p className="dg-walk-text">&ldquo;{walk.text}&rdquo;</p>
              <p className="dg-walk-step">
                step {Math.min(step + 1, walk.path.length)} of {walk.path.length}
                {step >= walk.path.length - 1 ? ` — ${walk.outcome}` : ""}
              </p>
            </div>
          )}
        </>
      )}

      {note && <p className="dg-note">{note}</p>}
    </div>
  );
}
