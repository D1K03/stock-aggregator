"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Sidebar from "@/components/Sidebar";
import { usePublishScreen } from "@/lib/screen-context";
import {
  Catalog,
  QueryFailure,
  QueryResult,
  fetchCatalog,
  isNumeric,
  runQuery,
  suggestQuery,
} from "@/lib/playground";

/* Read-only SQL over the data this deployment allows.
 *
 * What may be read is decided by a Postgres role, not by this page and not by a
 * check in the server: the tree below is a rendering of that role's grants, so
 * a table it cannot see is one it also cannot list. The application's own
 * connection is the cluster superuser, which is exactly why the query does not
 * go through it.
 *
 * No editor dependency. A textarea on the mono font is what `.stv-composer`
 * already does, and syntax highlighting colours the input you are about to stop
 * looking at. Tab is deliberately not captured: it is the only way out of the
 * field with a keyboard. */
export default function Playground() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sql, setSql] = useState("select code, name from data_source");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [failure, setFailure] = useState<QueryFailure | null>(null);
  const [running, setRunning] = useState(false);
  const [filter, setFilter] = useState("");
  const [ask, setAsk] = useState("");
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    fetchCatalog()
      .then(setCatalog)
      .catch((exc) => setError(exc instanceof Error ? exc.message : "failed"));
  }, []);

  const run = useCallback(async () => {
    if (!sql.trim() || running) return;
    setRunning(true);
    try {
      const outcome = await runQuery(sql);
      if (outcome.ok) {
        setResult(outcome.result);
        setFailure(null);
      } else {
        setFailure(outcome.failure);
        setResult(null);
      }
    } catch (exc) {
      setFailure({ error: exc instanceof Error ? exc.message : "that failed" });
      setResult(null);
    } finally {
      setRunning(false);
    }
  }, [sql, running]);

  /* Writes the query and stops. It does not run it: the model suggests and the
     reader decides, so Run stays the only thing that reaches the database. */
  const write = useCallback(async () => {
    if (!ask.trim() || asking) return;
    setAsking(true);
    setAskError(null);
    try {
      setSql(await suggestQuery(ask));
      box.current?.focus();
    } catch (exc) {
      setAskError(exc instanceof Error ? exc.message : "that failed");
    } finally {
      setAsking(false);
    }
  }, [ask, asking]);

  /* Inserting at the cursor rather than replacing the box: the point of the
     tree is to help write a query, not to overwrite one. */
  const insert = (text: string) => {
    const field = box.current;
    if (!field) return setSql((s) => s + text);
    const { selectionStart: from, selectionEnd: to } = field;
    field.setRangeText(text, from, to, "end");
    setSql(field.value);
    field.focus();
  };

  usePublishScreen(
    "Playground",
    result
      ? `SQL console. Last query returned ${result.row_count} rows in ${result.ms}ms: ${sql.slice(0, 120)}`
      : failure
        ? `SQL console. Last query failed: ${failure.error}`
        : "SQL console over the read-only tables"
  );

  const schemas = (catalog?.schemas ?? []).map((schema) => ({
    ...schema,
    tables: schema.tables.filter((t) =>
      filter ? t.name.toLowerCase().includes(filter.toLowerCase()) : true
    ),
  }));

  if (error === "unauthorised") {
    return (
      <div className="shell">
        <Sidebar active="Playground" />
        <div className="content wrap">
          <section className="card" style={{ padding: "calc(var(--sp) * 8)", textAlign: "center" }}>
            <p style={{ color: "var(--ink-muted)" }}>
              This page needs a session.{" "}
              <a href="/auth/login" style={{ color: "var(--copper)" }}>Sign in with GitHub</a>.
            </p>
          </section>
        </div>
      </div>
    );
  }

  return (
    <div className="shell">
      <Sidebar active="Playground" />
      <div className="content pg">
        <aside className="pg-tree">
          <div className="pg-tree-head">
            <span>Tables</span>
            <span>{schemas.reduce((n, s) => n + s.tables.length, 0)}</span>
          </div>
          <input
            className="pg-tree-filter"
            value={filter}
            placeholder="Filter…"
            onChange={(e) => setFilter(e.target.value)}
            aria-label="Filter tables"
          />
          <div className="pg-tree-list">
            {schemas.map((schema) => (
              <details className="pg-schema" key={schema.name} open>
                <summary>{schema.name}</summary>
                {schema.tables.map((table) => (
                  <details className="pg-table" key={table.name}>
                    <summary onClick={(e) => {
                      if (e.altKey) { e.preventDefault(); insert(`${schema.name}.${table.name}`); }
                    }}>
                      {table.name}
                    </summary>
                    {table.columns.map((column) => (
                      <div
                        className="pg-col"
                        key={column.name}
                        onClick={() => insert(column.name)}
                        title={`${column.type}${column.nullable ? "" : " not null"}`}
                      >
                        <span>{column.name}</span>
                        <em>{column.type}</em>
                      </div>
                    ))}
                  </details>
                ))}
              </details>
            ))}
            {catalog && !catalog.enabled && (
              <div className="pg-off">{catalog.reason ?? "Not configured here."}</div>
            )}
          </div>
        </aside>

        <div className="pg-main">
          <div className="pg-editor">
            <div className="pg-ask">
              <input
                value={ask}
                placeholder="Describe a query — e.g. the ten most-mentioned tickers this week"
                onChange={(e) => setAsk(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void write();
                  }
                }}
                aria-label="Describe the query you want"
                disabled={catalog?.enabled === false}
              />
              {askError && <span className="pg-ask-note">{askError}</span>}
              <button
                onClick={write}
                disabled={asking || !ask.trim() || catalog?.enabled === false}
              >
                {asking ? "Writing…" : "Write it"}
              </button>
            </div>
            <textarea
              ref={box}
              className="pg-sql"
              value={sql}
              spellCheck={false}
              onChange={(e) => setSql(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void run();
                }
              }}
              aria-label="SQL query"
            />
            <div className="pg-bar">
              <span>
                {result
                  ? `${result.row_count} row${result.row_count === 1 ? "" : "s"} in ${result.ms}ms` +
                    (result.truncated ? ` · cut at ${result.limit}` : "") +
                    (result.shortened ? ` · ${result.shortened} value(s) shortened` : "")
                  : catalog?.enabled === false
                    ? "Switched off on this deployment"
                    : "⌘/Ctrl + Enter to run"}
              </span>
              <button
                className="pg-run"
                onClick={run}
                disabled={running || !sql.trim() || catalog?.enabled === false}
              >
                {running ? "Running…" : "Run"}
              </button>
            </div>
          </div>

          <div className="pg-out">
            {failure && (
              <div className="pg-err">
                <b>{failure.error}</b>
                {failure.position != null && (
                  <>
                    {sql.split("\n")[0].slice(0, 200)}
                    {"\n"}
                    {" ".repeat(Math.max(0, failure.position)) + "^"}
                  </>
                )}
                {failure.detail && <small>{failure.detail}</small>}
                {failure.hint && <small>Hint: {failure.hint}</small>}
              </div>
            )}
            {!failure && result && result.columns.length > 0 && (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      {result.columns.map((c, i) => (
                        <th key={i} className={isNumeric(c.type) ? "num" : undefined}>
                          {c.name}
                          <small style={{ display: "block", opacity: 0.55, fontWeight: 400 }}>
                            {c.type}
                          </small>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((row, r) => (
                      <tr key={r}>
                        {row.map((cell, c) => (
                          <td key={c} className={isNumeric(result.columns[c].type) ? "num" : undefined}>
                            {/* An empty string and a null must be tellable
                                apart, or the table lies about the data. */}
                            {cell === null ? <span className="pg-null">NULL</span> : String(cell)}
                          </td>
                        ))}
                      </tr>
                    ))}
                    {result.rows.length === 0 && (
                      <tr>
                        <td colSpan={result.columns.length}>No rows.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
            {!failure && !result && (
              <div className="pg-empty">Run a query to see what comes back.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
