import { shortDate } from "@/lib/chart-svg";
import type { ChartSpec } from "@/lib/threads";

/* The scored screen, as the status service serves it (ui-swap spec D9, D10).
 *
 * Every figure arrives as a decimal string. The service has already rounded
 * scores and percentiles half up, and sends raw values exactly, so that a stored
 * value and its reproduction are never two floats that print the same (D5).
 * This module turns those strings into what a person reads, and the page never
 * computes a score of its own. */

export type PillarKey = "V" | "Q" | "M";
export const PILLAR_KEYS: readonly PillarKey[] = ["V", "Q", "M"];
export const PILLAR_NAMES: Record<PillarKey, string> = {
  V: "Valuation", Q: "Quality", M: "Momentum",
};

export type Close = [date: string, close: string];
export type Sector = { code: string; name: string };

export type Run = {
  id: number;
  as_of: string;
  started_at: string;
  finished_at: string | null;
  git_sha: string;
  config_hash: string;
  weight_version: string;
  cutoff_offset_seconds: number;
  logic: string;
  emits_alerts: boolean;
};

export type ScreenRow = {
  symbol: string;
  name: string;
  sector: Sector;
  score: string;
  /** Null when there is no previous night under the same weights, or this security was not scored on it. */
  delta: string | null;
  pillars: Record<PillarKey, { score: string; coverage: string | null } | null>;
  agreement: number;
  min_coverage: string;
  partial: boolean;
  closes: Close[];
};

export type Tiles = {
  scored: number;
  active_now: number;
  partial: number;
  agreement_3: number;
  market_ranked_values: number;
};

export type Awaiting = { state: "awaiting_first_night" };

export type ScreenPage = {
  state: "ready";
  latest: boolean;
  run: Run;
  previous_as_of: string | null;
  tiles: Tiles;
  sectors: Sector[];
  total: number;
  rows: ScreenRow[];
};

export type Sort = "score" | "delta" | "V" | "Q" | "M";
export const SORTS: { value: Sort; label: string }[] = [
  { value: "score", label: "Score" },
  { value: "delta", label: "Δ 1d" },
  { value: "V", label: "Valuation" },
  { value: "Q", label: "Quality" },
  { value: "M", label: "Momentum" },
];

export const PAGE_SIZE = 50;

export type ScreenQuery = {
  run?: number;
  sector: string;
  agree: string;
  partial: string;
  sort: Sort;
  offset: number;
};

export type Status = "ok" | "mismatch" | "absent" | "unexpected" | "refreshed" | "unchecked";
export type Unit = "percent" | "multiple";

export type StoredMetric = {
  raw: string;
  percentile: string;
  peer_group: string;
  peer_count: number;
  market_ranked: boolean;
  period_basis: string | null;
  period_end: string | null;
};

export type Metric = {
  code: string;
  name: string;
  unit: Unit;
  higher_is_better: boolean;
  status: Status;
  stored: StoredMetric | null;
  reproduced: string | null;
  reason: string | null;
};

export type Pillar = {
  code: string;
  key: PillarKey;
  score: string | null;
  present: number;
  expected: number;
  metrics: Metric[];
};

export type Scored = {
  scored: true;
  run_id: number;
  latest: boolean;
  symbol: string;
  name: string;
  sector: Sector;
  industry: Sector | null;
  active: boolean;
  score: string;
  agreement: number;
  partial: boolean;
  pillars: Pillar[];
  reproduction: {
    visible_through: string;
    run_build: string;
    running_build: string;
    refreshed_inputs: string[];
  };
  closes: Close[];
};

export type Unscored = {
  scored: false;
  run_id: number;
  latest: boolean;
  symbol: string;
  name: string;
  sector: Sector;
  active: boolean;
  closes: Close[];
};

export type SecurityDetail = Scored | Unscored;

/** A refused request, with the service's error code when it sent one (spec §7). */
export class ScreenError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(status: number, code: string | null) {
    super(`screen request failed: ${status}${code ? ` ${code}` : ""}`);
    this.status = status;
    this.code = code;
  }
}

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

async function get<T>(path: string, query: URLSearchParams): Promise<T> {
  const response = await fetch(`${BASE}${path}?${query}`, {
    // Same origin behind Caddy, but explicit: the session cookie is the whole
    // authorisation and a default that omitted it would 401 confusingly.
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    const body: { error?: unknown } | null = await response.json().catch(() => null);
    throw new ScreenError(response.status, typeof body?.error === "string" ? body.error : null);
  }
  return (await response.json()) as T;
}

export function fetchScreen(q: ScreenQuery): Promise<ScreenPage | Awaiting> {
  const query = new URLSearchParams();
  if (q.run !== undefined) query.set("run", String(q.run));
  if (q.sector) query.set("sector", q.sector);
  if (q.agree) query.set("agree", q.agree);
  if (q.partial) query.set("partial", q.partial);
  query.set("sort", q.sort);
  query.set("offset", String(q.offset));
  query.set("limit", String(PAGE_SIZE));
  return get("/api/screen", query);
}

export function fetchSecurity(symbol: string, run: number): Promise<SecurityDetail | Awaiting> {
  return get("/api/screen/security", new URLSearchParams({ symbol, run: String(run) }));
}

export function screenErrorText(exc: unknown): string {
  if (exc instanceof ScreenError) {
    if (exc.status === 401) return "Your session has ended. Reload the page to sign in again.";
    if (exc.code === "run_changed") return "The night you were browsing is no longer served.";
    if (exc.status === 503) return "The screen cannot be read: the database is unreachable.";
    return `The screen could not be read (${exc.message}).`;
  }
  return "The screen could not be read. Check the connection and try again.";
}

export function count(n: number): string {
  return n.toLocaleString("en-GB");
}

/** "1–50 of 1,499". */
export function pageRange(offset: number, shown: number, total: number): string {
  if (shown === 0) return `0 of ${count(total)}`;
  return `${count(offset + 1)}–${count(offset + shown)} of ${count(total)}`;
}

/** An `as_of` date in full. UTC, because a scoring night is a calendar date, not a moment. */
export function longDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
}

export function clock(iso: string): string {
  const time = new Date(iso).toLocaleTimeString("en-GB", {
    hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  });
  return `${time} UTC`;
}

/** D5: a fraction as a percent to one decimal, or a multiple to two decimals with ×. */
export function metricValue(value: string, unit: Unit): string {
  const n = Number(value);
  return unit === "percent" ? `${(n * 100).toFixed(1)}%` : `${n.toFixed(2)}×`;
}

/** D5: percentiles are whole numbers on screen. */
export function percentile(value: string): number {
  return Math.round(Number(value));
}

function decimals(value: string): number {
  const dot = value.indexOf(".");
  return dot < 0 ? 0 : value.length - dot - 1;
}

/* Reproduced minus stored, computed on the decimal strings rather than as floats:
   a difference in the seventh digit is the whole point of showing one, and float
   subtraction would bury it in noise such as 0.00020000000000000573 (D5). */
export function difference(stored: string, reproduced: string): string {
  const places = Math.max(decimals(stored), decimals(reproduced));
  const scaled = (value: string) => {
    const negative = value.startsWith("-");
    const digits = value.replace("-", "").replace(".", "") + "0".repeat(places - decimals(value));
    return BigInt(digits) * BigInt(negative ? -1 : 1);
  };
  const d = scaled(reproduced) - scaled(stored);
  const zero = BigInt(0);
  const sign = d < zero ? "-" : d > zero ? "+" : "";
  const digits = (d < zero ? -d : d).toString().padStart(places + 1, "0");
  return places ? `${sign}${digits.slice(0, -places)}.${digits.slice(-places)}` : `${sign}${digits}`;
}

/** "TTM · Dec 2025", "annual · Sep 2025", or "price" for a momentum metric, which has no period. */
export function period(stored: StoredMetric): string {
  if (!stored.period_basis && !stored.period_end) return "price";
  const end = stored.period_end
    ? new Date(`${stored.period_end}T00:00:00Z`).toLocaleDateString("en-GB", {
        month: "short", year: "numeric", timeZone: "UTC",
      })
    : null;
  return [stored.period_basis, end].filter(Boolean).join(" · ");
}

/** The Overview's price chart, drawn by the one renderer in price mode (D15, D16). */
export function priceSpec(symbol: string, name: string, closes: Close[]): ChartSpec {
  return {
    kind: "price",
    ticker: symbol,
    title: `${symbol} — adjusted close, ${closes.length} trading days`,
    subtitle: `${name} · stored end-of-day bars, adjusted for splits and dividends · ${shortDate(closes[0][0])} to ${shortDate(closes[closes.length - 1][0])}`,
    series: closes.map(([, close]) => Number(close)),
    dates: closes.map(([day]) => day),
    marks: [],
  };
}

/* What the palette sends with a question about the Overview (D18). Most
   important first, because `describe()` cuts at 400 characters and should lose
   only the tail. */
export function summarise(row: ScreenRow | undefined, filters: string[]): string {
  if (!row) return "the scored screen, with no security selected";
  const delta =
    row.delta === null
      ? "new since the previous night"
      : `Δ ${Number(row.delta) > 0 ? "+" : ""}${row.delta} on the previous night`;
  return [
    `${row.symbol} (${row.name})`,
    `blended score ${row.score}${row.partial ? ", partial" : ""}`,
    delta,
    PILLAR_KEYS.map((key) => `${key} ${row.pillars[key]?.score ?? "—"}`).join(" "),
    `${row.agreement} of 3 pillars top-quartile`,
    row.sector.name,
    filters.length ? `filters: ${filters.join(", ")}` : null,
  ]
    .filter((part): part is string => part !== null)
    .join(", ");
}
