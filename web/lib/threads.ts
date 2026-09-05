/* Conversation history.
 *
 * Kept in localStorage, not the database, and that is a decision rather than
 * laziness: a conversation is the user's own text, the audit trail already
 * records that a reply happened and what it cost without keeping what was
 * said, and storing chat content server-side is a privacy question nobody has
 * asked for. The cost is that history is per-browser. */

export type ToolRun = { name: string; ms: number };

/* Something Steven drew rather than said.
 *
 * It comes from the chart tool and never passed through the model, so the
 * numbers here are the ones the tool computed rather than the ones a model
 * repeated back. `marks` is where it drew: a point, or a span between two
 * indices, each already labelled by the tool that worked it out. */
export type Mark = {
  kind: "point" | "span";
  index: number;
  end: number | null;
  label: string;
  tone: "copper" | "blue" | "amber";
};
export type ChartSpec = {
  ticker: string;
  title: string;
  subtitle: string;
  series: number[];
  dates: string[];
  median: number;
  threshold: number;
  marks: Mark[];
};

export type Turn = {
  role: "you" | "steven";
  text: string;
  tools?: ToolRun[];
  charts?: ChartSpec[];
};
export type Thread = { id: string; title: string; turns: Turn[]; updatedAt: number };

const KEY = "screener.palette.threads";
/* Enough to find last week's question, few enough that localStorage never
   becomes something to manage. */
const MAX_THREADS = 25;

export function loadThreads(): Thread[] {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    // A corrupt entry should cost you your history, not the whole palette.
    return [];
  }
}

export function saveThread(thread: Thread): Thread[] {
  const others = loadThreads().filter((t) => t.id !== thread.id);
  const next = [thread, ...others]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_THREADS);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // Quota, most likely. Losing the write is better than losing the reply
    // that is on screen.
  }
  return next;
}

export function deleteThread(id: string): Thread[] {
  const next = loadThreads().filter((t) => t.id !== id);
  localStorage.setItem(KEY, JSON.stringify(next));
  return next;
}

/* The first thing asked, which is what someone scanning the list is looking
   for. Trimmed to a line so the list stays scannable. */
export function titleFor(turns: Turn[]): string {
  const first = turns.find((t) => t.role === "you")?.text ?? "New chat";
  return first.length > 46 ? `${first.slice(0, 46)}…` : first;
}

export function whenever(ts: number): string {
  const mins = (Date.now() - ts) / 60000;
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return new Date(ts).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/* Suggestions, not commands. Each is a question Steven can genuinely answer
   today: one uses a tool, the rest are things he knows about himself or the
   design. Nothing here asks for a number, because there is no ingest and the
   answer would have to be invented. */
export const SKILLS: { label: string; prompt: string }[] = [
  { label: "Deployment status", prompt: "Is the deployment healthy?" },
  { label: "What can you do?", prompt: "What can you do and what do you have access to?" },
  { label: "How scoring works", prompt: "How does the scoring model work?" },
  { label: "Why alerts fire", prompt: "When does an alert fire, and why on the crossing?" },
  { label: "Chart NVDA", prompt: "Show me NVDA's 60-day chart and mark its biggest surge." },
];
