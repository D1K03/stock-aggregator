"use client";

import { isNumeric } from "@/lib/playground";
import type { RowsSpec } from "@/lib/threads";

/* A result set Steven selected, in a chat message.
 *
 * The same bargain the chart makes: the rows never went through the model, so a
 * wide table costs nothing per round and arrives here intact rather than as the
 * model's description of it. What Steven says about it is a sentence; this is
 * the thing itself.
 *
 * Rows are arrays rather than objects because `select 1 as a, 2 as a` is legal
 * SQL and an object would silently lose a column. */
export default function ChatRows({ spec }: { spec: RowsSpec }) {
  return (
    <figure className="crows">
      <div className="crows-sql">{spec.sql}</div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {spec.columns.map((c, i) => (
                <th key={i} className={isNumeric(c.type) ? "num" : undefined}>
                  {c.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {spec.rows.map((row, r) => (
              <tr key={r}>
                {row.map((cell, c) => (
                  <td key={c} className={isNumeric(spec.columns[c].type) ? "num" : undefined}>
                    {/* An empty string and a null must be tellable apart. */}
                    {cell === null ? <span className="pg-null">NULL</span> : String(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <figcaption>
        {spec.rows.length} row{spec.rows.length === 1 ? "" : "s"}
        {spec.truncated ? ", cut" : ""} · {spec.ms}ms
      </figcaption>
    </figure>
  );
}
