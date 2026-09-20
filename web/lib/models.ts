/* What the model picker knows, read from the server once per page.
 *
 * Every figure here was computed by `screener.ai.catalogue` from OpenRouter's
 * live catalogue, and none of it is recomputed in the browser. That is the same
 * rule the chart tool follows: a number the interface prints should be the
 * number something worked out, not one the display arrived at on its own, or
 * the two drift and the screen is the one that gets believed.
 *
 * The one thing this file does decide is what to show when the server has
 * nothing — OpenRouter unreachable, or the catalogue still cold. The picker
 * falls back to the conversation's current model as a single row, because the
 * honest empty state for "which models can I choose" is "the one you are on". */

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

/** The three Artificial Analysis indices, in the order the picker shows them. */
export const INDICES = ["agentic_index", "intelligence_index", "coding_index"] as const;
export type Index = (typeof INDICES)[number];

/* Short enough for the column they sit over — "Reasoning" ran into "Code" and
   the header read REASONINGCODE. "Intel" is also what OpenRouter's own value
   board calls this one, so the two are describing the same number by the same
   name. The full sense is in the tooltip. */
export const INDEX_LABELS: Record<Index, string> = {
  agentic_index: "Tools",
  intelligence_index: "Intel",
  coding_index: "Code",
};

export type ModelRow = {
  slug: string;
  label: string;
  author: string;
  context: number;
  input_per_m: number;
  output_per_m: number;
  /** Weighted mean of the percentiles below, 0–100. */
  capability: number;
  /** Capability per dollar of a blended turn. What the list is sorted by. */
  value: number;
  /** How many of the three benchmarks this model has been run on. */
  coverage: number;
  /** Whether there is enough evidence to offer it as the recommendation. */
  recommendable: boolean;
  /** Percentile within the eligible field, or null where nobody has measured. */
  percentiles: Record<Index, number | null>;
};

/* The router, as a row you can select.
 *
 * `slug` is what gets stored when you pick it; `resolves` is the model it comes
 * out as at this moment and is shown rather than saved, because the reason to
 * choose it is that tomorrow's answer may be a different model. */
export type Router = {
  slug: string;
  resolves: string | null;
  label: string | null;
  why: string | null;
};

export type Catalogue = {
  /** What is selected: a model slug, or the router's sentinel. */
  current: string;
  router: Router;
  recommended: { slug: string; why: string } | null;
  weights: Record<string, number>;
  models: ModelRow[];
};

/* The router's sentinel, as the server spells it. Compared against `current`
   rather than inferred from the absence of a model, so "nobody has chosen" and
   "chose the router" stay one state in the browser as they are on the server. */
export const ROUTER = "router";

/* What the router is called on screen. It is Steven's own pick — the assistant
   choosing what to answer on — so it is named after him rather than after the
   mechanism. Nobody wants to select "best-value auto-router"; they want Steven
   to sort it out. */
export const ROUTER_LABEL = "Steven";

/** Dollars per million tokens, at the precision the difference is visible. */
export function price(perM: number): string {
  if (perM === 0) return "free";
  // Below a cent, two significant figures: $0.036 and $0.04 are a real
  // difference here and `toFixed(2)` renders both as $0.04.
  return perM < 0.1 ? `$${perM.toPrecision(2)}` : `$${perM.toFixed(2)}`;
}

/** 262144 → "262k". A context window is a magnitude, not a measurement. */
export function context(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(tokens % 1_000_000 === 0 ? 0 : 2)}M`;
  return `${Math.round(tokens / 1000)}k`;
}

/* One row standing in for a catalogue nobody could fetch. Not an error state:
   the conversation works, it is on this model, and the only thing missing is
   the ability to change it. Saying that quietly beats an error banner over a
   feature that is decoration on a working chat. */
export function soleRow(slug: string): Catalogue {
  return {
    current: slug,
    router: { slug: ROUTER, resolves: null, label: null, why: null },
    recommended: null,
    weights: {},
    models: [
      {
        slug,
        label: slug.split("/").pop() ?? slug,
        author: slug.split("/")[0] ?? "",
        context: 0,
        input_per_m: 0,
        output_per_m: 0,
        capability: 0,
        value: 0,
        coverage: 0,
        recommendable: false,
        percentiles: {
          agentic_index: null,
          intelligence_index: null,
          coding_index: null,
        },
      },
    ],
  };
}

export async function loadCatalogue(current: string): Promise<Catalogue | null> {
  try {
    const response = await fetch(
      `${BASE}/api/models${current ? `?current=${encodeURIComponent(current)}` : ""}`,
      { credentials: "include", cache: "no-store" }
    );
    if (!response.ok) return null;
    const body = (await response.json()) as Catalogue;
    return Array.isArray(body.models) && body.models.length > 0 ? body : null;
  } catch {
    return null;
  }
}

/* Record which model this person wants to be answered on.
 *
 * Per person and server-side, not per conversation and not in this browser.
 * That is what makes the choice mean the same thing in Discord: the bot reads
 * it back out of the audit trail with the same identity fold the spend cap
 * uses, so picking a model here is picking the model your DMs come back on.
 *
 * It lapses. The server stops honouring a choice after a day and falls back to
 * whatever the ranking recommends, so a stronger model picked for one
 * afternoon does not quietly become what everything costs from then on. The
 * returned `hours` is that window, and the picker says it out loud rather than
 * letting the revert look like a bug. */
export async function chooseModel(
  slug: string
): Promise<{ model: string; hours: number } | null> {
  try {
    const response = await fetch(
      `${BASE}/api/model?slug=${encodeURIComponent(slug)}`,
      { method: "POST", credentials: "include", cache: "no-store" }
    );
    if (!response.ok) return null;
    return (await response.json()) as { model: string; hours: number };
  } catch {
    return null;
  }
}
