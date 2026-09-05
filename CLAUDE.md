# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This file is the short form. `DESIGN.md` holds the decisions and the reasoning behind them —
read it before design work. `PLAN.md` holds current scope and the task in flight. Dated specs
live in `docs/specs/`; the database schema is
`docs/specs/2026-09-04-database-schema-design.md`. `docs/infrastructure.md` says what tooling
exists and when to reach for it — read it before adding a proxy, a secret or a model call.

## Status

The database schema and the infrastructure layer are built and tested; no ingest, scoring or
alerting code exists yet. Runtime dependencies are `psycopg` and `httpx`, and nothing else —
check `pyproject.toml` before assuming a library is available.

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
- LLMs do narrative extraction (transcript summaries, guidance changes, risk-section flags) and
  answer questions in the Discord server, via OpenRouter. Never a sentiment number and never a
  score. The bot's system prompt forbids investment advice and forbids inventing a figure the
  screener does not have.
- Everything runs on one VPS: the always-on job, and Postgres in the same compose stack on a
  named volume. Serverless was considered and downgraded — too weak for the scoring job. Where
  raw payloads land is still open; `DESIGN.md` says why.
- Bright Data is available as an opt-in fetch strategy, off by default — the default strategy
  list is `("direct",)` and a test enforces it. Its other form is a `LanePool`: one long-lived
  session per exit address, pinned by `-ip-`, for endpoints where a cookie has to outlive the
  request. Yahoo is the one caller that starts on it, because a night's ingest is ~3,000
  requests off one address and a pinned lane measured 1.07x direct latency; `BRIGHTDATA_PROXY_IPS`
  is both the switch and the list. Apify remains rejected (per-result fees compound daily);
  make a case if you think it earns its place.
- Secrets come from Infisical at startup. Ingress is a Cloudflare Tunnel, SSH is Tailscale-only,
  and deploys are a GHCR image rolled out by `.github/workflows/deploy.yml`.

## Commands

- Install: `pip install -e ".[dev]"`
- Tests: `pytest` — needs `DATABASE_URL_TEST` pointing at a throwaway Postgres 16
  (the suite drops and recreates the `public` schema on every test).
- Single test: `pytest tests/test_identity.py::test_overlapping_symbol_periods_for_one_security_are_rejected -v`
- Typecheck: `pyright` — must report zero errors. psycopg types query parameters as
  `LiteralString`, so SQL assembled at runtime is rejected by design: build DDL with
  `psycopg.sql` composition, and reserve `cast(LiteralString, ...)` for trusted file
  content such as a migration.
- Apply migrations: `python -m screener.boot migrate` (takes an advisory lock, then pre-creates
  partitions a year ahead)
- Refresh the universe CSV (quarterly, manual, no database):
  `python -m screener.universe refresh` — goes out over the Bright Data lanes when
  `BRIGHTDATA_PROXY_IPS` is set; `--no-proxy` forces one direct lane.
- Load it (no network): `python -m screener.universe load --dry-run` then without `--dry-run`.
  Refuses if more than 10% of the active universe would be retired; `--force` overrides.
- Run the status service: `python -m screener.boot` — `/health`, `/ready`, `/status` on 8080
- Check every integration against the real world: `python -m screener.boot selftest`

Migrations are plain numbered SQL in `migrations/`, applied in filename order and
recorded in `schema_migration`. Each runs in its own transaction, so a failure leaves
it unrecorded and the fixed file can be re-run.

## Infrastructure layout

Each piece is its own package with a small public surface exposed through `__init__.py`;
nothing outside imports a submodule directly.

- `screener.config` — process configuration. Credentials do **not** live here. Each subsystem
  owns its own (`fetch.config`, `ai.config`, `notify.config`), so a process posting an alert
  does not need a database URL it will never use.
- `screener.secrets` — Infisical into `os.environ`, stdlib `urllib` only.
- `screener.fetch` — `fetch(url, strategies)` over a `direct -> isp_proxy -> unlocker` chain,
  plus `LanePool`, which is the one thing that chain structurally cannot be: a session held
  across requests so a cookie outlives the call that fetched it. A lane is one client, one jar,
  one exit; the pool rotates over them and **never sleeps, retries or throttles**, so rate
  limiting stays with the caller. Rotation is not concurrency. Reach for `fetch()` unless you
  are holding a cookie, and read `docs/specs/2026-09-05-yahoo-exit-lanes.md` before changing it.
- `screener.ai` — OpenRouter. Narrative extraction only, never a sentiment number.
- `screener.notify` — a `NotificationChannel` protocol and a Discord webhook. Delivery only.
- `screener.bot` — the Discord gateway bot, its own process (`python -m screener.bot`) and its
  own container. A command surface, not a delivery channel: `/ping`, a reply when mentioned in
  the server, and a reply to anything at all in a DM — one rule, "answer when spoken to", which
  needs saying explicitly in a shared channel and does not in a two-person conversation.
  A DM shortly after a handoff picks up what the person was looking at, read back from the
  audit trail rather than held in memory: the handoff is sent by the status service and answered
  by the bot, and those are two processes.
  The reply runs through `asyncio.to_thread`, because every other layer here is synchronous and
  blocking the event loop stalls the gateway heartbeat rather than just one command.
  Tools live in `bot/tools`; a tool that draws rather than speaks registers its artifact with
  `collecting()` so a 60-point series never enters the model's context, and the point it marks
  is computed from the data rather than chosen by the model. A chart is one SVG string built by
  `web/lib/chart-svg.ts`: the browser shows it and `bot/render.py` posts it to `/api/render` in
  the web container, which rasterises the same string to PNG for Discord. One renderer, so the
  two surfaces cannot drift — do not add a second way to draw a chart.
- `screener.transcribe` — speech to text, in a container of its own. The client half is
  `httpx` and nothing else and is what the bot and the status service import; the server half
  holds faster-whisper and is the only thing that installs the `voice` extra, so the three
  runtime dependencies are unchanged. Not WhisperX: its forced alignment and diarization are
  what need torch, and neither means anything for one person talking into a phone. A Discord
  voice message is transcribed and answered as though it had been typed; the dashboard's mic
  button puts the transcript in the composer so a misheard ticker is fixed before it is paid
  for. The transcript is never written to the audit trail or to disk.
- `screener.auth` — GitHub sign-in for the status service. Sessions live in
  their own `auth` schema, never in `public` with the screener's own tables.
- `screener.health` — stdlib status service; the Cloudflare Tunnel's origin.
  `/health` and `/ready` stay open because the container healthcheck and the
  deploy smoke test cannot hold a session. Everything else requires one
  **unconditionally** — never `config.enabled and login is None`, which makes an
  unconfigured sign-in open the endpoints rather than close them.
- `screener.bot.budget` — the daily spend cap, per person, from `DAILY_SPEND_CAP_USD`
  (default $0.10). Checked before the model is called, folds Discord onto GitHub through
  `DISCORD_USER_MAP` so it cannot be doubled by switching surface, and **fails open** when the
  trail cannot be read — refusing everyone because Postgres blinked is the worse failure.
- `screener.concept` — invented, schema-shaped sample data mirroring
  `web/lib/data.ts`, so the dashboard and the chart tool draw the same line. A
  test parses the TypeScript and fails when the two drift. Delete it when
  ingest lands.
- `screener.provenance` — `git_sha()` and `config_hash()`, the two `not null` columns on
  `scoring_run` that had no producer. `config_hash` takes the caller's *scoring* parameters; it
  is not derived from process configuration.
- `screener.universe` — the ticker universe, as two commands that share nothing but a file.
  `refresh` builds `data/universe.csv` from Wikipedia, SEC and Yahoo and never opens a database
  connection; `load` reconciles that CSV into the identity tables and never opens a socket. The
  committed CSV is the review surface: a sector reclassification moves a ticker between peer
  groups, so it has to show up in a diff before it can move a score. Identity is matched on CIK,
  not symbol — match on symbol and a rename reads as a departure plus an unrelated arrival.
- `screener.boot` — secrets, then migrations under an advisory lock, then serve.
