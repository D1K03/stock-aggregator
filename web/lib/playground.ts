/* The shape the status service returns from /api/playground.
 *
 * Read-only SQL over the tables a separate Postgres role is allowed to see.
 * The tree below is a rendering of that role's grants rather than a list kept
 * here, so what is shown and what is readable cannot drift apart. */

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export type Field = { name: string; type: string; nullable: boolean };
export type Table = { name: string; kind: string; columns: Field[] };
export type Schema = { name: string; tables: Table[] };

export type Catalog = {
  enabled: boolean;
  schemas: Schema[];
  reason?: string;
  limits?: {
    max_rows: number;
    default_rows: number;
    max_sql: number;
    timeout_ms: number;
  };
};

export type Column = { name: string; type: string };

export type QueryResult = {
  columns: Column[];
  /* Arrays rather than objects: `select 1 as a, 2 as a` is legal SQL and an
     object would silently lose a column. */
  rows: (string | number | boolean | null)[][];
  row_count: number;
  truncated: boolean;
  shortened: number;
  ms: number;
  limit: number;
};

export type QueryFailure = {
  error: string;
  sqlstate?: string | null;
  position?: number | null;
  detail?: string | null;
  hint?: string | null;
};

/* Postgres spells its numeric types out; the page right-aligns them. */
const NUMERIC = /^(smallint|integer|bigint|numeric|real|double precision)/;
export const isNumeric = (type: string) => NUMERIC.test(type);

export async function fetchCatalog(): Promise<Catalog> {
  const response = await fetch(`${BASE}/api/playground`, {
    credentials: "include",
    cache: "no-store",
  });
  if (response.status === 401) throw new Error("unauthorised");
  if (!response.ok) throw new Error(`catalogue request failed: ${response.status}`);
  return response.json();
}

/* Returns the result, or the failure. A refused query is not an exception here:
   a message with a position under the offending character is most of what makes
   a SQL box usable, so it is part of the shape rather than a thrown error. */
export async function runQuery(
  sql: string,
  limit?: number
): Promise<{ ok: true; result: QueryResult } | { ok: false; failure: QueryFailure }> {
  const response = await fetch(`${BASE}/api/playground/query`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sql, limit }),
  });
  if (response.status === 401) throw new Error("unauthorised");
  const body = await response.json();
  if (response.ok) return { ok: true, result: body as QueryResult };
  if (response.status === 400) return { ok: false, failure: body as QueryFailure };
  throw new Error(body.error ?? `query failed: ${response.status}`);
}

/* Ask for SQL rather than writing it. What comes back goes in the editor and is
   not run: the model suggests, the reader decides, and the Run button stays the
   only thing that touches the database. */
export async function suggestQuery(ask: string): Promise<string> {
  const response = await fetch(`${BASE}/api/playground/suggest`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ask }),
  });
  if (response.status === 401) throw new Error("unauthorised");
  const body = await response.json();
  if (!response.ok) throw new Error(body.error ?? `that failed: ${response.status}`);
  return body.sql as string;
}
