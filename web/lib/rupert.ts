/* What the status service returns from /api/rupert. */

export type RupertDay = {
  day: string;
  /** Every item a decision was asked about. `crowded` never reached the model. */
  decisions: number;
  /** How many of those linked to a security. */
  resolved: number;
  cost_usd: number;
};

export type RupertStanding = {
  security_id: number;
  symbol: string;
  name: string;
  mentions: number;
  /** How many of those mentions have a tone reading. */
  read: number;
  /** Null below the minimum mention count — a count is not a reading. */
  tone: number | null;
  /** How many items the trimmed mean discarded from the ends. */
  trimmed: number | null;
  /** Volume against this security's own baseline, null without enough history. */
  attention: number | null;
};

export type RupertScored = {
  day: string;
  /** Mentions that day. The line is meaningless without this. */
  mentions: number;
  /** Mentions across the trailing window the tone was computed over. */
  window_mentions: number;
  /** Null when the window did not reach the floor. A gap, never a zero. */
  tone: number | null;
};

export type RupertDecision = {
  id: number;
  state: "resolved" | "none" | "unsure" | "crowded" | "failed";
  chosen: string | null;
  symbol: string | null;
  confidence: number | null;
  candidates: string[];
  claim_kind: string | null;
  injection: number | null;
  position_talk: number | null;
  tone: number | null;
  subreddit: string | null;
  excerpt: string;
  /** When the comment was written. */
  at: string;
  /** When we decided about it. Not the same clock. */
  observed_at: string;
};

export type RupertFrontier = {
  corpus: string;
  read_through: string;
  items_read: number;
  updated_at: string;
};

export type RupertPass = {
  endpoint: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  shortlisted: number | null;
  resolved: number | null;
  error: string | null;
};

export type Rupert = {
  /** False while RUPERT_DAILY_MAX_CALLS is 0, which is how it ships. */
  enabled: boolean;
  daily_max_calls: number;
  confidence_floor: number;
  counts: Record<string, number>;
  spend: {
    calls_today: number;
    cost_today: number;
    cost_window: number;
    decisions_total: number;
    per_decision: number | null;
  };
  coverage: {
    mentioned: number;
    scoreable: number;
    active: number;
    floor: number;
    days: number;
  };
  /* The schedule. `next_run_at` is computed from the last pass's *finish*,
     because the container sleeps its interval after a pass — anchoring on the
     start would promise a run that is already late. */
  paused: boolean;
  paused_by: string | null;
  paused_at: string | null;
  refresh_hours: number;
  last_run_at: string | null;
  next_run_at: string | null;
  daily: RupertDay[];
  scored: RupertScored[];
  scored_security: { security_id: number; symbol: string; name: string } | null;
  rolling_days: number;
  standings: RupertStanding[];
  review: RupertDecision[];
  review_page: number;
  review_pages: number;
  review_total: number;
  frontiers: RupertFrontier[];
  passes: RupertPass[];
};

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

/* The same call `lib/magpie.ts` and `lib/skybird.ts` make, and spelled the same
   way on purpose: the "unauthorised" sentinel every page branches on is the
   thing that ends up written three different ways when each file invents it. */
async function call<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    credentials: "include",
    cache: "no-store",
  });
  if (response.status === 401) throw new Error("unauthorised");
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error ?? `request failed: ${response.status}`);
  }
  return response.json();
}

export function fetchRupert(
  state?: string,
  page = 1,
  security?: number
): Promise<Rupert> {
  const query = new URLSearchParams({ page: String(page) });
  if (state) query.set("state", state);
  if (security) query.set("security", String(security));
  return call<Rupert>(`/api/rupert?${query}`);
}

export type RupertNarrative = {
  symbol: string;
  name: string;
  /** Null when there was nothing to read, which is not a failure. */
  text: string | null;
  mentions_used: number;
  window_days: number;
  /** True when today's was already written, so the second click is free. */
  cached: boolean;
};

/** What the corpus was saying about one security, in English.

    A POST because it spends money and writes a row — and because a GET that did
    either is one prefetch away from being expensive by accident. */
export async function fetchNarrative(security: number): Promise<RupertNarrative> {
  const response = await fetch(`${BASE}/api/rupert/narrative`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ security }),
  });
  if (response.status === 401) throw new Error("unauthorised");
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error ?? `request failed: ${response.status}`);
  return body;
}

export type RupertFull = {
  id: number;
  state: string;
  rupert_version: string;
  version_is_current: boolean;
  current_version: string;
  source: {
    corpus: string;
    subreddit: string | null;
    author: string | null;
    permalink: string | null;
    created_utc: string | null;
    title: string | null;
    body: string;
  };
  shortlist: { candidates: string[]; names: Record<string, string>; sent_text: string };
  jev: {
    model: string;
    /** Rebuilt from the shortlist, not stored — see the page for why. */
    asked: { state: unknown; questions: Record<string, unknown> } | null;
    reconstructed: boolean;
    chosen: string | null;
    confidence: number | null;
    probabilities: Record<string, number> | null;
    own_business: number | null;
    position_talk: number | null;
    injection: number | null;
    claim_kind: string | null;
    claim_confidence: number | null;
    input_tokens: number;
    cost_usd: number;
    decided_at: string;
  };
  /** What FinBERT was handed, and every cap between the comment and the tokens
      it saw. The excerpt is not the comment — it is a trimmed copy. */
  finbert_input: {
    text: string;
    chars: number;
    excerpt_cap: number;
    client_cap: number;
    batch_cap: number;
    token_cap: number;
    same_as_jev: boolean;
  };
  finbert: {
    model: string;
    positive: number;
    negative: number;
    neutral: number;
    /** Derived, never stored: one definition, computed where the inputs are. */
    score: number;
    read_at: string;
  }[];
  security: { id: number; symbol: string; name: string } | null;
};

export function fetchDecision(id: number): Promise<RupertFull> {
  return call<RupertFull>(`/api/rupert/decision?id=${id}`);
}

export async function setPaused(paused: boolean): Promise<{ paused: boolean; paused_by: string | null }> {
  const response = await fetch(`${BASE}/api/rupert/pause`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paused }),
  });
  if (response.status === 401) throw new Error("unauthorised");
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error ?? `request failed: ${response.status}`);
  return body;
}

/** How long until the next pass, said definitely rather than hedged.

    "Runs in 4h" and "would run in 4h" are both statements; "next run may be
    around 4h" is the kind of wording that makes somebody go and check the logs,
    which is the thing this line exists to save them. */
export function until(iso: string | null, paused: boolean): string {
  if (!iso) return paused ? "Paused. No pass has run yet." : "No pass has run yet.";
  const mins = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  const verb = paused ? "Would run" : "Runs";
  if (mins <= 0) {
    return paused
      ? "Would run on the next wake — it is due now."
      : "Due now — the next wake picks it up.";
  }
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  const span = h > 0 ? `${h}h ${m}m` : `${m}m`;
  return `${verb} in ${span}.`;
}

/* What each decision means, in words rather than in the enum the database
   stores. `none` is the one worth spelling out: it is the answer that makes the
   whole layer honest, and a bare "none" reads like a failure rather than like
   the resolver declining to invent a link. */
export const MEANING: Record<string, string> = {
  resolved: "linked to a security",
  none: "not about any of them — ordinary English, or the market rather than a company",
  unsure: "a company, but below the confidence floor — kept, not linked",
  crowded: "too many tickers to be about one — never sent to the model",
  failed: "the decision could not be made",
};

export const TONE_WORD = (tone: number): string =>
  tone > 0.15 ? "positive" : tone < -0.15 ? "negative" : "level";

/** Money at these prices. Four decimals, because $0.0002 renders as "$0.00". */
export function usd(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  if (value >= 0.01) return `$${value.toFixed(3)}`;
  return `$${value.toFixed(5)}`;
}

export function when(iso: string): string {
  const mins = (Date.now() - new Date(iso).getTime()) / 60000;
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function shortDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return `${d.getDate()} ${d.toLocaleDateString("en-GB", { month: "short" }).slice(0, 3)}`;
}
