# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This file is the short form. `DESIGN.md` holds the decisions and the reasoning behind them —
read it before design work. `PLAN.md` holds current scope and the task in flight. Dated specs
live in `docs/specs/`; the database schema is
`docs/specs/2026-09-04-database-schema-design.md`.

## Status

Skeleton — no source, dependencies, or tests on disk yet. Do not assume tooling that is not
present. The database schema is specified but not implemented.

## What it does

Daily ingest for a ticker universe → score each ticker across independent pillars → persist a
dated snapshot → diff against yesterday → post threshold crossings to Discord with the pillar
breakdown. Everything else (web UI, backtesting, LLM summarisation, 13F) is additive.

## Hard constraints

- No price prediction, no buy/sell calls. The tool surfaces candidates and evidence. Alerts say
  "score crossed threshold", never "STRONG BUY".
- Every score traces back to visible raw inputs.
- APIs before scrapers. Scrapers are the fragile layer; respect robots.txt and ToS.
- Boring, cheap and honest over impressive. Target ~£5–10/month running cost.

## Scoring model

Pillars measure deliberately different things (Valuation, Quality, Momentum, Sentiment,
Insider/Institutional, plus Event risk as a flag layer) so that correlated signals don't become
one signal with six names.

- Normalise each metric to a **percentile within its sector**, then average within pillar.
- Pillar weights live **in the database**, not in code.
- Persist **per-pillar scores and their raw inputs** per snapshot. The blended score is derivable
  from pillar scores + a `weight_version_id` stamp — don't treat it as ground truth.
- Sector percentiles need peers: enforce a minimum peer count per sector or fall back to a
  broader grouping, otherwise thin sectors produce noise.
- A score can move because peers moved. Keep raw metrics alongside percentiles so an alert can
  say which happened.
- Surface **pillar agreement** (how many pillars are top-quartile at once) as its own column.
- Weight Sentiment low — it is noisy and lags price; treat it as a crowding warning.
- Log scores daily so weights can be backtested later. Fundamentals from yfinance are restated,
  not point-in-time, so history cannot be backfilled into a credible backtest.

## Alerting

Fire on the **crossing**, not the state. Deduplication and a per-ticker cooldown are required
from day one or the channel gets muted. Alerts carry the reasoning: score delta, pillar scores,
the driver, and event-risk flags. Delivery is a single HTTP POST to a Discord webhook.

## Stack decisions

- Python. Data: yfinance, Finnhub, Alpha Vantage, FINRA, SEC EDGAR, Reddit API.
- Sentiment: VADER for a cheap baseline, FinBERT (ONNX via `onnxruntime`) for the real score.
  CPU only — no GPU, no CUDA. Never use an LLM to emit a sentiment number.
- LLMs are for narrative extraction only (transcript summaries, guidance changes, risk-section
  flags), via OpenRouter with a cheap model.
- VPS for the always-on job; Azure Postgres for snapshots, Blob for large raw payloads. Azure
  Functions consumption plan is too weak for the scoring job. Running the daily job locally and
  pushing to the cloud DB is an acceptable v1 shortcut.
- Rejected: Bright Data (enterprise anti-bot, no bot problem here), Apify (per-result fees
  compound daily). Make a case if you think either earns its place.

## Commands

- Install: `pip install -e ".[dev]"`
- Tests: `pytest` — needs `DATABASE_URL_TEST` pointing at a throwaway Postgres 16
  (the suite drops and recreates the `public` schema on every test).
- Single test: `pytest tests/test_identity.py::test_overlapping_symbol_periods_for_one_security_are_rejected -v`
- Typecheck: `pyright` — must report zero errors. psycopg types query parameters as
  `LiteralString`, so SQL assembled at runtime is rejected by design: build DDL with
  `psycopg.sql` composition, and reserve `cast(LiteralString, ...)` for trusted file
  content such as a migration.
- Apply migrations: `python -c "import psycopg, pathlib; from screener.migrate import apply_migrations; from screener.config import settings; conn = psycopg.connect(settings().database_url, autocommit=True); print(apply_migrations(conn, pathlib.Path('migrations')))"`

Migrations are plain numbered SQL in `migrations/`, applied in filename order and
recorded in `schema_migration`. Each runs in its own transaction, so a failure leaves
it unrecorded and the fixed file can be re-run.
