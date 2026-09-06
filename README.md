# stock-aggregator

Multi-signal equity screener. Aggregates fundamentals, analyst revisions, insider activity and
news sentiment into transparent, sector-relative pillar scores, then alerts on threshold
crossings via Discord.

**Not investment advice.** The tool surfaces candidates and the evidence behind them. It makes no
price predictions and no buy or sell recommendations — alerts say "score crossed threshold", never
"strong buy". Every score traces back to the raw inputs that produced it.

## Status

Early. The database schema is built and tested; nothing ingests data yet.

| Piece | State |
|---|---|
| Database schema | Done — 9 migrations, 44 tests |
| Infrastructure | Done — secrets, fetching, LLM router, alert delivery, status service, GitHub sign-in, CI/CD |
| Universe and identity | Not started |
| Ingest | Not started |
| Scoring | Not started |
| Diff and alerting | Not started |

## Getting started

Requires Python 3.11+ and Docker.

```bash
git clone git@github.com:D1K03/stock-aggregator.git
cd stock-aggregator

# Postgres 16 for the test suite. Same image and credentials CI uses.
docker compose up -d

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export DATABASE_URL_TEST="postgresql://postgres:screener@localhost:5432/screener_test"
pytest
```

You should see `101 passed`. If most of them skip, `DATABASE_URL_TEST` is unset —
the tests needing a database skip. The suite skips rather than fails so it stays usable without
one, which is convenient locally and would be dangerous in CI, so it fails there instead.

Port 5432 already taken by a local Postgres install? Set `POSTGRES_PORT` before
`docker compose up` and adjust the URL to match.

## Commands

| | |
|---|---|
| Tests | `pytest` (parallel; `-n0` for one process) |
| One test | `pytest tests/test_identity.py::test_a_retired_symbol_can_be_reissued_to_another_security -v` |
| Typecheck | `pyright` — must report zero errors |
| Apply migrations | `python -m screener.boot migrate` |
| Refresh universe CSV | `python -m screener.universe refresh` |
| Load universe | `python -m screener.universe load --dry-run` |
| Run the status service | `python -m screener.boot` (the dashboard API, the playground and the MCP connector) |
| Check every integration | `python -m screener.boot selftest` |
| Stop the database | `docker compose down` |

`screener.boot` takes a Postgres advisory lock before migrating and pre-creates partitions a year
ahead, so it is safe to run from two places at once — which a container restarting during a
deploy does routinely.

`selftest` reports one line per integration: the database and its migration count, the build SHA,
a direct fetch, a proxied fetch and whether its exit IP actually differs, and an OpenRouter round
trip with its cost. Anything unconfigured reports `SKIP`. It never posts to Discord.

Migrations are plain numbered SQL in `migrations/`, applied in filename order and recorded in
`schema_migration`. Each runs in its own transaction, so a failure leaves it unrecorded and the
fixed file can be re-run. **Never edit an applied migration** — add a new one.

## How it fits together

A daily job ingests data for a ticker universe, scores each ticker across pillars that measure
deliberately different things, persists a dated snapshot, diffs it against the previous
comparable one, and posts threshold crossings to a Discord webhook.

Two properties shape most of the schema:

- **Scores are sector-relative.** Each metric becomes a percentile within its sector peer group,
  because a 15x P/E means opposite things for a utility and a chipmaker. Thin sectors fall back to
  a broader grouping, and which grouping was used is recorded.
- **History is append-only.** Fundamentals get restated after the fact, so facts carry both the
  period they describe and when they were learned; a restatement inserts rather than overwrites.
  Scores are versioned per scoring run for the same reason — a crossing is a property of an
  adjacent *pair* of scores, so recomputing in place would retroactively change which alerts ever
  fired.

## Reading it from Claude

The same read-only tables are available to claude.ai as a custom connector. The
address is at the foot of the table tree on `/playground`, with a copy button;
paste it into **Customize → Connectors**, approve once with GitHub, and Claude
can query prices, fundamentals, scores, alerts, Reddit and the live transcripts
in one context. It reads and never writes, and every call is recorded against
the login that authorised it.

Switched off unless `PLAYGROUND_MCP_DB_PASSWORD` is set, on the same terms as
the SQL console beside it. `docs/infrastructure.md` covers the OAuth flow, the
third Postgres role, and the two pieces of Cloudflare configuration that live
outside this repository.

## Documentation

| File | Contents |
|---|---|
| `DESIGN.md` | Decisions and the reasoning behind them. Read before design work. |
| `PLAN.md` | Current scope and what is being worked on next. |
| `docs/specs/` | Dated specifications. |
| `docs/plans/` | Dated implementation plans. |
| `docs/infrastructure.md` | What tooling exists and when to reach for it. |
| `docs/architecture.md` | Diagrams: containers, packages, schema, pipeline, CI/CD. |
| `deploy/README.md` | Deployment runbook: first-time setup, rolling back, spend limits. |
| `CLAUDE.md` | Short-form guidance for Claude Code. |

## Licence

MIT — see `LICENSE`.
