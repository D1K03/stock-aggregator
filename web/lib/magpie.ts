/* What the status service returns from /api/magpie. */

export type MagpieDocument = {
  id: number;
  url: string;
  host: string;
  title: string;
  author: string | null;
  published: string | null;
  word_count: number;
  strategy: string;
  /** Every rung tried, in order, ending in the one that answered. */
  attempts: string[];
  cost_usd: number;
  fetched_at: string;
  /** The opening of the article. Never the whole thing — that is /playground. */
  lead: string;
};

export type MagpieAttempt = {
  id: number;
  url: string;
  host: string;
  state: string;
  reason: string | null;
  strategy: string | null;
  cost_usd: number;
  requested_at: string;
  requested_by: string | null;
  document_id: number | null;
};

/* Two lists, paged independently. Every scrape adds an attempt — including the
   refused ones, which produce no document — so they do not grow at the same
   rate and sharing one page number would make the shorter list jump about. */
export type MagpieListing = {
  documents: MagpieDocument[];
  documents_page: number;
  documents_pages: number;
  documents_total: number;
  attempts: MagpieAttempt[];
  attempts_page: number;
  attempts_pages: number;
  attempts_total: number;
};

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export async function fetchGathered(docs = 1, tries = 1): Promise<MagpieListing> {
  const response = await fetch(`${BASE}/api/magpie?docs=${docs}&tries=${tries}`, {
    credentials: "include",
    cache: "no-store",
  });
  if (response.status === 401) throw new Error("unauthorised");
  if (!response.ok) throw new Error(`magpie request failed: ${response.status}`);
  return response.json();
}

export type ScrapeResult =
  | { ok: true; document: MagpieDocument }
  | { ok: false; reason: string; detail: string; attempts: string[] };

export async function scrape(url: string): Promise<ScrapeResult> {
  const response = await fetch(`${BASE}/api/magpie/scrape`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 201 && body.document) {
    return { ok: true, document: body.document };
  }
  return {
    ok: false,
    reason: body.reason ?? "failed",
    detail: body.detail ?? body.error ?? "That did not work.",
    attempts: body.attempts ?? [],
  };
}

export async function forget(id: number): Promise<boolean> {
  const response = await fetch(`${BASE}/api/magpie/delete`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  return response.ok;
}

/* Why a page was not kept, in words rather than in the enum the database
   stores. A refusal is not a fault and should not read like one. */
export const WHY: Record<string, string> = {
  robots: "the site's robots.txt disallows it",
  paywall: "the publisher marks it as not free to read",
  not_a_page: "that address is a file, not a web page",
  too_short: "the page came back, but there was no article in it",
  all_strategies_failed: "every route to it failed",
  unlocker_capped: "every free route failed and the paid one is at its daily limit",
  unavailable: "the scraper is not running",
};

export function when(iso: string): string {
  const mins = (Date.now() - new Date(iso).getTime()) / 60000;
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}
