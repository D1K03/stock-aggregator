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
| Infrastructure | Done — secrets, fetching, LLM router, alert delivery, status service, CI/CD |
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
| Tests | `pytest` |
| One test | `pytest tests/test_identity.py::test_a_retired_symbol_can_be_reissued_to_another_security -v` |
| Typecheck | `pyright` — must report zero errors |
| Apply migrations | `python -m screener.boot migrate` |
| Run the status service | `python -m screener.boot` |
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

## Documentation

| File | Contents |
|---|---|
| `DESIGN.md` | Decisions and the reasoning behind them. Read before design work. |
| `PLAN.md` | Current scope and what is being worked on next. |
| `docs/specs/` | Dated specifications. |
| `docs/plans/` | Dated implementation plans. |
| `deploy/README.md` | Deployment runbook: first-time setup, rolling back, spend limits. |
| `CLAUDE.md` | Short-form guidance for Claude Code. |

## Licence

MIT — see `LICENSE`.
