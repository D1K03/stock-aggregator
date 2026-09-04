"use client";

import { motion } from "framer-motion";

export default function Sparkline({ h }: { h: number[] }) {
  const w = 84, ht = 26, pad = 2;
  const min = Math.min(...h), max = Math.max(...h);
  const x = (i: number) => (i / (h.length - 1)) * w;
  const y = (v: number) => ht - pad - ((v - min) / (max - min || 1)) * (ht - 2 * pad);
  const d = h.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  return (
    <svg width={w} height={ht} aria-hidden="true">
      <motion.path
        d={d}
        fill="none"
        stroke="var(--copper)"
        strokeWidth={1.5}
        strokeLinecap="round"
        initial={{ pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={{ duration: 1.1, ease: [0, 0, 0.2, 1] }}
      />
    </svg>
  );
}
