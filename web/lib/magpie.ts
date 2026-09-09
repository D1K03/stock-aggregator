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

export type MagpieSite = {
  host: string;
  /** How many distinct links point at this site. */
  links: number;
  /** How many times the article mentions them in total. */
  mentions: number;
  /** How many of those links have been followed. */
  followed: number;
  /** What the article called one of them, as a hint at what the site is. */
  example: string;
};

export type MagpieLink = {
  id: number;
  url: string;
  host: string;
  anchor: string;
  occurrences: number;
  /** The document this produced, once somebody followed it. */
  scraped_id: number | null;
};

export type MagpieDetail = {
  document: MagpieDocument & { text: string };
  sites: MagpieSite[];
  links: MagpieLink[];
  /** False when the stored page could not be read, so there are no sources. */
  links_read: boolean;
};

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

/* One place that knows how a call to the status service goes wrong.
 *
 * `lib/skybird.ts` has had this and this file had three inlined copies, which
 * is how the "unauthorised" sentinel every page branches on ends up being
 * spelled differently in one of them. */
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    // Same origin behind Caddy, but explicit: the session cookie is the whole
    // authorisation and a default that omitted it would 401 confusingly.
    credentials: "include",
    cache: "no-store",
    ...init,
  });
  if (response.status === 401) throw new Error("unauthorised");
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error ?? `request failed: ${response.status}`);
  }
  return response.json();
}

export function fetchGathered(docs = 1, tries = 1): Promise<MagpieListing> {
  return call<MagpieListing>(`/api/magpie?docs=${docs}&tries=${tries}`);
}

/** One document, the sites it points at, and the links to them.

    All three together, because a detail view that also had to hold a copy from
    the list would show a stale one the moment anything changed. */
export function fetchDocument(id: number, host?: string): Promise<MagpieDetail> {
  const query = new URLSearchParams({ id: String(id) });
  if (host) query.set("host", host);
  return call<MagpieDetail>(`/api/magpie/document?${query}`);
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
  try {
    await call(`/api/magpie/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    return true;
  } catch {
    return false;
  }
}

/** Scrape one link a document points at. The same path as one you pasted. */
export async function follow(linkId: number): Promise<ScrapeResult> {
  const response = await fetch(`${BASE}/api/magpie/follow`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ link_id: linkId }),
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

/* Why a page was not kept, in words rather than in the enum the database
   stores. A refusal is not a fault and should not read like one. */
export const WHY: Record<string, string> = {
  robots: "the site's robots.txt disallows it",
  paywall: "the publisher marks it as not free to read",
  not_a_page: "that address is a file, not a web page",
  too_short: "the page came back, but there was no article in it",
  all_strategies_failed: "every route to it failed",
  restarted: "the scraper was replaced before it could fetch this, so try again",
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
