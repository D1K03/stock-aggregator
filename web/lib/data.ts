/* Concept data, shaped by the schema: snapshot_daily + pillar_score_daily +
   alert_event. Deterministic, so the design is reviewable; nothing here reads
   the real database. */

export const PILLAR_KEYS = ["V", "Q", "M", "S", "I"] as const;
export const PILLAR_NAMES: Record<string, string> = {
  V: "Valuation", Q: "Quality", M: "Momentum", S: "Sentiment", I: "Insider",
};
export const THRESHOLD = 75;

export type Row = {
  sym: string; name: string; sector: string;
  score: number; prev: number; p: number[]; flags: string[];
};

export const ROWS: Row[] = [
  { sym: "PGR", name: "Progressive", sector: "Insurance", score: 84, prev: 82, p: [76, 88, 82, 54, 79], flags: [] },
  { sym: "NVDA", name: "NVIDIA", sector: "Semiconductors", score: 82, prev: 68, p: [44, 91, 88, 62, 71], flags: ["Earnings 6d"] },
  { sym: "LLY", name: "Eli Lilly", sector: "Healthcare", score: 79, prev: 80, p: [29, 90, 83, 71, 55], flags: ["FDA window"] },
  { sym: "AVGO", name: "Broadcom", sector: "Semiconductors", score: 78, prev: 75, p: [51, 84, 79, 58, 64], flags: [] },
  { sym: "MSFT", name: "Microsoft", sector: "Software", score: 76, prev: 75, p: [38, 93, 71, 60, 57], flags: [] },
  { sym: "JPM", name: "JPMorgan", sector: "Banks", score: 74, prev: 68, p: [66, 79, 77, 57, 62], flags: [] },
  { sym: "COST", name: "Costco", sector: "Retail", score: 72, prev: 72, p: [22, 86, 74, 68, 61], flags: [] },
  { sym: "MU", name: "Micron", sector: "Semiconductors", score: 71, prev: 73, p: [78, 55, 81, 66, 48], flags: [] },
  { sym: "CAT", name: "Caterpillar", sector: "Industrials", score: 68, prev: 59, p: [62, 74, 78, 51, 83], flags: [] },
  { sym: "AMD", name: "AMD", sector: "Semiconductors", score: 66, prev: 62, p: [49, 72, 69, 74, 52], flags: [] },
  { sym: "XOM", name: "Exxon", sector: "Energy", score: 63, prev: 67, p: [82, 61, 42, 49, 58], flags: ["Ex-div 3d"] },
  { sym: "DOW", name: "Dow", sector: "Chemicals", score: 38, prev: 45, p: [71, 41, 22, 35, 44], flags: ["Guidance cut"] },
];

export const MEDIANS: Record<string, number> = {
  Semiconductors: 57, Insurance: 52, Healthcare: 54, Software: 55, Banks: 51,
  Retail: 53, Industrials: 50, Energy: 48, Chemicals: 47,
};
export const PEERS: Record<string, number> = {
  Semiconductors: 38, Insurance: 24, Healthcare: 31, Software: 45, Banks: 29,
  Retail: 22, Industrials: 33, Energy: 27, Chemicals: 21,
};

export function agree(row: Row): number {
  return row.p.filter((x) => x >= 75).length;
}

/* A walk toward yesterday's score, then today's appended: the final step IS the
   move the alert fired on, so it must be the only jump. */
export function history(row: Row): number[] {
  let seed = [...row.sym].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7);
  const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  const n = 60;
  const out: number[] = [];
  let v = row.prev - (row.score - row.prev) * 0.9 - 3 + rnd() * 6;
  for (let i = 0; i < n - 1; i++) {
    const drift = (row.prev - v) * (i / (n - 1)) * 0.18;
    v = Math.min(97, Math.max(8, v + drift + (rnd() - 0.5) * 3.2));
    out.push(v);
  }
  out[n - 2] = row.prev;
  out.push(row.score);
  return out;
}

export type Alert = {
  sym: string; from: number; to: number; rule: string; why: string;
  chips: [string, number][]; flag?: string; muted?: boolean; cooldown?: string;
};

export const ALERTS: Alert[] = [
  { sym: "NVDA", from: 68, to: 82, rule: "crossed 75 ↑", why: "Driver: three upward revisions in 5 days.", chips: [["Q", 91], ["M", 88], ["S", 62], ["V", 44]], flag: "earnings in 6 days" },
  { sym: "CAT", from: 59, to: 68, rule: "crossed 65 ↑", why: "Driver: insider buying cluster — three open-market buys this week.", chips: [["I", 83], ["M", 78], ["Q", 74]] },
  { sym: "DOW", from: 45, to: 38, rule: "crossed 40 ↓", why: "Driver: peers re-ranked. Raw margins unchanged; the sector moved around it.", chips: [["M", 22], ["S", 35], ["Q", 41]] },
  { sym: "AMD", from: 62, to: 66, rule: "near 65", muted: true, cooldown: "cooldown · 9 days left", why: "Suppressed — same rule fired 5 days ago.", chips: [] },
];
