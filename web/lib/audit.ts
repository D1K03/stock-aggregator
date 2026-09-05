/* The shape the status service returns from /api/audit. */

export type AuditEvent = {
  id: number;
  occurred_at: string;
  kind: string;
  operation: string;
  actor: string;
  actor_kind: string;
  outcome: string;
  model: string | null;
  tokens: number;
  cost_usd: number;
  duration_ms: number | null;
  detail: Record<string, unknown>;
};

export type Spend = {
  events: number;
  total_cost_usd: number;
  total_tokens: number;
  events_24h: number;
  cost_24h_usd: number;
  tokens_24h: number;
};

/* What one person cost, across every surface they used.
 *
 * Folded server-side through DISCORD_USER_MAP — the same mapping that lets a
 * dashboard conversation be handed to Discord, read the other way round — so
 * someone who used both is one row rather than two. Anyone unmapped still
 * appears, under whatever identity the trail has, with `known` false and no
 * picture. */
export type Person = {
  login: string;
  known: boolean;
  avatar: string | null;
  events: number;
  cost_usd: number;
  tokens: number;
  cost_24h_usd: number;
  last_seen: string;
  surfaces: { kind: string; cost_usd: number }[];
};

export type AuditPage = {
  events: AuditEvent[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
  spend: Spend;
  people: Person[];
  /** What any one person may spend in a day, in dollars. */
  daily_cap_usd: number;
  operations: { kind: string; operation: string; count: number }[];
};

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export async function fetchAudit(params: {
  kind?: string;
  operation?: string;
  page: number;
}): Promise<AuditPage> {
  const query = new URLSearchParams();
  if (params.kind) query.set("kind", params.kind);
  if (params.operation) query.set("operation", params.operation);
  query.set("page", String(params.page));

  const response = await fetch(`${BASE}/api/audit?${query}`, {
    // Same origin behind Caddy, but explicit: the session cookie is the whole
    // authorisation and a default that omitted it would 401 confusingly.
    credentials: "include",
    cache: "no-store",
  });
  if (response.status === 401) throw new Error("unauthorised");
  if (!response.ok) throw new Error(`audit request failed: ${response.status}`);
  return response.json();
}

/* Costs here run to millionths of a dollar, so the usual two decimal places
   would render every row as $0.00 and the totals as nothing at all. */
export function money(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(6)}`;
  return `$${usd.toFixed(2)}`;
}

export function compact(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
