# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This file is the short form. `DESIGN.md` holds the decisions and the reasoning behind them —
read it before design work. `PLAN.md` holds current scope and the task in flight. Dated specs
live in `docs/specs/`; the database schema is
`docs/specs/2026-09-04-database-schema-design.md`. `docs/infrastructure.md` says what tooling
exists and when to reach for it — read it before adding a proxy, a secret or a model call.
`docs/architecture.md` draws the same system: containers and their edges, the package graph,
the schema, the pipeline and CI/CD.

## Status

The database schema, the infrastructure layer and daily ingest — **price and fundamentals** — are
built and tested; scoring is built for Momentum, Valuation and Quality and writes snapshots with
alerting switched off. The pipeline now runs on its own, once a night, rather than by hand. No
alerting code exists yet. Runtime dependencies are `psycopg`, `httpx` and `discord.py`, and
nothing else — check `pyproject.toml` before assuming a library is available. `faster-whisper`,
`yt-dlp`, `trafilatura` and FinBERT's `onnxruntime` are extras (`voice`, `stream`, `scrape`,
`sentiment`) that one image each installs, and each is imported inside a function so the rest of
the tree stays importable without them.

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

- Python. Data: Yahoo Finance **called directly, never yfinance** — a payload stored from the
  library is its DataFrame reshaping rather than the response, which breaks the content-hash
  restatement detector. Then Finnhub, Alpha Vantage, FINRA, SEC EDGAR, Reddit API.
- Sentiment: FinBERT (ONNX via `onnxruntime`) is built — `screener.sentiment`, its own
  container. CPU only, no GPU, no CUDA. Never use an LLM to emit a sentiment number. VADER was
  the planned cheap baseline and has not been needed: the classifier itself is free to run.
  **Nothing scores anything with it yet** — the Sentiment pillar is a separate piece of work.
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
  (the suite drops and recreates the `public` schema on every test). Parallel by
  default: one worker per core, each creating its own database in that server,
  because twenty migrations cost ~320ms and every database test pays it. `-n0`
  puts it back on one process for a debugger or a failure that needs reading in
  order. **Export the variable** — nothing loads `.env` into pytest, and an
  unset one skips every database test while still reporting green (CI sets it
  and fails rather than skipping, so this is a local trap only). The Postgres
  it points at is `compose.yaml`'s, which raises `max_locks_per_transaction`:
  one worker per core times a partitioned database exhausts the 64-lock default
  on any machine with more cores than a CI runner.
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
- Run the status service: `python -m screener.boot` — `/health`, `/ready`, `/status`,
  `/playground`'s API and the MCP connector, all on 8080
- Supervise live stream captures: `python -m screener.skybird` (the container's command).
  `start <url>`, `stop <id>`, `list` and `delete <id>` are the dashboard's writes from a
  terminal.
- Check every integration against the real world: `python -m screener.boot selftest`
- Ingest prices: `python -m screener.ingest prices` — fetches missing daily bars per
  security, backfilling to 2020 on first sight. `sweep` is a hand-run diagnostic that
  compares six years against what is stored and writes nothing.
- Ingest fundamentals: `python -m screener.ingest fundamentals` — every line item
  Yahoo reports per period, appended only where a value changed. Safe to re-run:
  an unchanged night writes observations and no facts.
- Score a night: `python -m screener.scoring run` — every active security for today, into
  `metric_daily`, `peer_group_stat`, `pillar_score_daily` and `snapshot_daily`, in one
  transaction. `--as-of` overrides the date and refuses one in the past. Takes an advisory lock,
  so a second process refuses rather than scoring the same night twice. **A failed night needs
  no operator action: re-run the date.** A failure it can catch marks the run `failed`, which
  (migration 020) is what stops it holding the date; one it cannot — a kill, an OOM — leaves
  `outcome = 'running'`, and the next run's `reconcile` settles it, the way skybird settles a
  capture left by a dead supervisor.
- Serve sentiment: `python -m screener.sentiment` (the container's command) — FinBERT on
  `/score`, weights loaded before the socket opens. No arguments, no credentials, no database.
- Ingest insider transactions: `python -m screener.edgar` (the container's command) — walks the
  EDGAR daily indexes it has not walked yet, newest first, `EDGAR_DAYS_PER_PASS` a pass. No
  arguments and no `backfill` subcommand: widening `EDGAR_BACKFILL_DAYS` offers more days and the
  running container drains them. Unset `EDGAR_CONTACT_EMAIL` exits 0 without opening a socket.
- Run the scheduler: `python -m screener.nightly` (the container's command) — waits for 23:00
  UTC, runs prices, fundamentals and scoring in order, and posts to Discord only when a night is
  given up. `NIGHTLY_ENABLED=false` exits the process; `restart: unless-stopped` brings the
  container back to exit again, so re-enabling needs the container recreated
  (`docker compose up -d nightly`) rather than just a restart.

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
  limiting stays with the caller. `across()` is the only concurrency: one worker per lane, no
  argument to run more, because the safe claim is "one request in flight per exit address"
  rather than "concurrency is fine". Reach for `fetch()` unless you are holding a cookie, and
  read `docs/specs/2026-09-05-yahoo-exit-lanes.md` before changing it.
- `screener.ai` — OpenRouter. Narrative extraction only, never a sentiment number.
- `screener.notify` — a `NotificationChannel` protocol and a Discord webhook. Delivery only.
- `screener.bot` — the Discord gateway bot, its own process (`python -m screener.bot`) and its
  own container. A command surface, not a delivery channel: `/ping`, a reply when mentioned in
  the server, and a reply to anything at all in a DM — one rule, "answer when spoken to", which
  needs saying explicitly in a shared channel and does not in a two-person conversation.
  A DM shortly after a handoff picks up what the person was looking at, read back from the
  audit trail rather than held in memory: the handoff is sent by the status service and answered
  by the bot, and those are two processes.
  He remembers the conversation the same way — the last two exchanges, read back per person
  with Discord folded onto GitHub, so a dashboard thread carries over when you press Continue
  in Discord. Bounded on every axis at once (two exchanges, `MEMORY_CHARS`, final text only,
  half an hour, and nothing when the dashboard sends `fresh=1` for a new chat) because a
  remembered turn is re-sent on every round of every message after it.
  The reply runs through `asyncio.to_thread`, because every other layer here is synchronous and
  blocking the event loop stalls the gateway heartbeat rather than just one command.
  Tools live in `bot/tools`; a tool that draws rather than speaks registers its artifact with
  `collecting()` so a 60-point series never enters the model's context, and the point it marks
  is computed from the data rather than chosen by the model. The `chart` tool draws a security's
  real adjusted closes, read as `playground_bot` through the `screener.screen` statements the
  dashboard's endpoints use, captioned with the latest scored night's scores. A chart is one SVG
  string built by `web/lib/chart-svg.ts`, in score or price mode: the browser shows it and
  `bot/render.py` posts it to `/api/render` in the web container, which rasterises the same
  string to PNG for Discord. One renderer, so the two surfaces cannot drift — do not add a second
  way to draw a chart.
- `screener.edgar` — SEC Form 4, the first data behind the Insider pillar, in a container that
  wakes every `EDGAR_REFRESH_HOURS`. Two halves sharing nothing but a dataclass, as `reddit` and
  `universe` do. **The unambiguous API case**: no key, no session, no proxy, `robots.txt` does not
  disallow `/Archives/`, and SEC publishes the rate it will serve. What it does require is a
  contact address in the User-Agent, so `EDGAR_CONTACT_EMAIL` is the credential and the off switch
  at once — deliberately *not* `SEC_CONTACT_EMAIL`, or setting an address for the quarterly
  universe refresh would start a daily crawler.
  Unlike `reddit` and `magpie` this does **not** stop short of a security: a Form 4 names its
  issuer's CIK and `universe` already matches identity on CIK, so the question that blocked both of
  those does not arise. Three facts decide the whole design and each was measured, not inferred.
  **The filing's XML filename is the filer agent's choice** — `form4.xml`, `ownership.xml`,
  `wk-form4_1789156901.xml` and `tm2624595-6_4seq1.xml` in a sample of seven — so what is fetched is
  the complete submission the daily index already names, and guessing `form4.xml` 404s six times in
  seven. **EDGAR writes one index line per filer, not per filing**: 921 Form 4 rows on 2026-09-11
  were 435 filings, one listed eleven times — and because the issuer is always one of those lines,
  the universe filter runs against the *index*, so the rest are never opened. That filter matches
  on any filer rather than on the issuer, so a company we hold filing as a ten percent owner of one
  we do not is fetched and then dropped: one transaction in 437 on 11 September.
  **A missing daily index is 403, not 404**, indistinguishable from SEC refusing us once
  `screener.fetch` has collapsed it to a string, so the quarter's `index.json` is read first and a
  day it does not name is never requested. A refusal therefore **ends the pass** rather than
  narrowing and retrying, which is the opposite of Arctic Shift's 422: SEC blocks the address for
  about ten minutes and every further request extends it.
  One row per transaction, with the reporting owners as arrays — a Form 4 can be filed jointly, and
  on 2026-09-11 ten owners shared a single 79,649-share trade, so a row per owner would have read
  as ten times the volume with every figure looking plausible. The key is
  `(accession, table_kind, transaction_seq)` and the sequence is what makes it safe: two grants on
  the same day at the same price under different plans are two real transactions, and a key over
  the contents would silently merge them. The frontier is `ingest_run`, not a new table and not
  `max(filed_date)` — a day when nobody we hold filed produces no rows, and "walked it, found
  nothing" must not read as "never walked it". Widening `EDGAR_BACKFILL_DAYS` **is** the backfill;
  there is no subcommand. Ingest only: nothing scores anything, and the pillar needs a
  weight-version bump rather than a table.
- `screener.magpie` — the scraper, in a container of its own. Paste a link and the article is
  fetched over the cheapest route that works, read out of the page and kept. **The escalating
  ladder is not here**: `screener.fetch` already had `direct -> isp_proxy -> unlocker`, and nothing
  in the project had ever named the billed rung. What this adds is the policy around it — whether
  the site permits it, what counts as a page rather than a block, what it cost, and where it lands.
  Three refusals deliberately do **not** escalate, which is the opposite of the rule everywhere
  else: robots.txt saying no (reaching for a proxy would turn "respect robots.txt" into "route
  around" it), a body that is not a web page (`FetchResult` has no Content-Type, so a PDF arrives
  as mojibake and the ladder would climb to the billed rung to fail again), and a publisher marking
  a page as not free to read. `magpie.attempt` records every try including the refusals, so a dead
  link and an untried link are not the same thing and the same one is not paid for twice; it is
  also the meter for the billed rung, which is capped by a **count of its own** and never by
  `DAILY_SPEND_CAP_USD` — a scrape is forty times a reply, and one counter for both would let
  scraping silence Steven. `content_hash` is over the extracted headline and body, never the HTML,
  for the reason `screener.reddit` hashes per item.
  Like `screener.reddit`, it stops at keeping the text: nothing here connects a document to a
  security or scores it, and `screener.sentiment` is the scorer `magpie.document` is waiting for.
  Opening a document reads its stored page **back out of the blob store** for the sites it points
  at, which is the first thing here to read a payload rather than only write one, and it opens no
  socket. The links come from trafilatura's extracted body, not the raw HTML: every anchor on a
  page includes the site's own furniture, and on one Wikipedia article that is the difference
  between 248 links led by *Donate* and *Privacy Policy* and 214 real citations. Read once per
  document and stamped by `links_read_at`, so a document nobody opens is never read.
  `magpie.link.scraped_id` is the crawler's frontier, `where scraped_id is null` being the queue,
  and it exists before anything drains it so that cycle is a new process rather than a migration
  over live rows. **Nothing crawls**: every fetch is still one somebody clicked.
- `screener.mcp` — claude.ai reading this data as a custom connector, over the
  Model Context Protocol. The transport is Streamable HTTP answered in plain
  JSON: the spec allows a single object in reply to a POST instead of an SSE
  stream, and **cloudflared buffers server-sent events**, so a transport that
  never opens a stream is the one that works through this tunnel. Stateless, no
  `Mcp-Session-Id`, `GET /mcp` is 405. Hand-rolled rather than the SDK, which
  would bring pydantic and starlette in to replace a five-entry dispatch table.
  Auth is OAuth 2.1 with dynamic client registration, because claude.ai offers
  nothing simpler — its API-key mode is beta and limited to some organisations,
  and authless would leave a database open to whoever learned the URL. The
  consent step is the GitHub session the dashboard already issues, so there is
  no second identity. `_redirect_allowed` is where the security actually lives:
  registration is unauthenticated by necessity, so a callback address is checked
  against a list rather than accepted from whoever registered it. Reads as
  `playground_mcp` (018), a third role whose grant list adds the skybird
  transcripts — which means transcripts leave the box, said out loud in the
  migration. **Two things are not in this repo and will fail invisibly:** the
  Caddy handles for `/mcp`, `/oauth/*` and `/.well-known/*`, and Cloudflare's
  "Block AI bots" rule, which answers `Claude-User` with 403 at the edge.
- `screener.playground` — read-only SQL from the dashboard and from Steven's `sql` tool, over
  one engine so the bounds cannot differ between them. **The enforcement is a Postgres role, not
  a check in Python**: the app connects as the cluster superuser, on which a SQL box would be
  `pg_read_file` and `COPY FROM PROGRAM`, so queries go through a read-only role holding `select`
  on the tables listed in `migrations/013_playground.sql` and nothing else — not sign-in and not
  the audit trail. **Two roles, differing in one schema**: the dashboard connects as `playground`,
  which can read skybird, and Steven as `playground_bot`, which cannot — he works a capture's
  controls and cannot read a transcript back. Neither is chosen in Python; both processes read
  `PLAYGROUND_DATABASE_URL` and compose hands them different ones, so the split is a credential
  rather than a branch, and the bot container never holds the console's password. Every query goes through a *named* cursor, because psycopg uses the simple protocol
  when a query has no parameters and a plain execute would run `select 1; drop table security`.
  Unset `PLAYGROUND_DB_PASSWORD` is the off switch and the page says so.
- `screener.reddit` — social ingest for the Sentiment pillar, in a container that wakes every
  `REDDIT_REFRESH_HOURS`. Two halves sharing nothing but a dataclass, as `universe` does:
  `source` never opens a database connection, `store` never opens a socket. **Not Reddit's own
  API** — it answers unauthenticated requests with 403 whatever User-Agent is sent, its
  robots.txt disallows every agent, and an OAuth client needs manual approval; the mirror also
  has the date-range search that Reddit's thousand-item listing cap does not, without which a
  week of r/wallstreetbets is unreachable. Ingest only: nothing here connects an item to a
  security or scores it — `screener.sentiment` is the scorer it is waiting for, and the join
  between them is unbuilt. `content_hash` is taken per item rather than per response, which is the
  remedy `DESIGN.md` proposes for Yahoo applied where it works — a comment body almost never
  changes, so a re-fetch writes nothing.
  The mirror's **422 is its query timing out, not a rate limit** — it reproduces from an address
  with no history and `limit=10` fails as `limit=100` does — so a refusal halves how much of the
  timeline one request asks for rather than sleeping and asking again, which measurably does not
  work. 429 is the real throttle and still backs off. A span that dies anyway lands in
  `social_gap`: the walk runs backwards, so an interruption banks everything newer than the point
  it died and `max(created_utc)` jumps to the present, and **two aggregates cannot describe a hole
  between them** — that is how 36 of 168 hours of r/stocks went missing while the mirror still
  held every one. Gaps drain *after* the catch-up span, never before, because a repair has no
  upper bound and fresh comments should not wait behind one. `python -m screener.reddit backfill
  [days]` queues a stretch and exits; the running container drains it.
- `screener.sentiment` — FinBERT, in a container of its own, split the way `transcribe` is: the
  client half is `httpx` and nothing else, the server half holds `onnxruntime` and is the only
  thing that installs the `sentiment` extra. A separate image is earned here by the **weights**
  rather than the libraries — those are a subset of the transcriber's — because 438 MB of BERT
  would otherwise be pulled by every container on every deploy. The checkpoint is converted to
  ONNX once in a builder stage that is thrown away, and that stage **runs the exported graph
  beside the original and fails the build if they disagree**, which is the only place torch and
  the checkpoint are both present to ask. Three probabilities come back, never one number:
  `score` is `positive - negative`, derived in the client, so a reading always carries its
  inputs. **The column order is the one thing not assumed anywhere** — FinBERT's `id2label` is
  `positive, negative, neutral`, which is neither alphabetical nor guessable, and reading it
  wrong scores every beat as a miss with nothing to notice; it is written beside the weights as
  `labels.json`, the service refuses to start without it, and the deploy and the self-test both
  assert it. Measured on the box, which has AVX but no AVX2: **150 to 180 words a second** on two
  threads, which is the unit that holds steady, because texts/s varies tenfold with length.
  **That is the number the Sentiment pillar has to be designed around** — a week of
  r/wallstreetbets is ten hours, so whatever consumes this samples. Chunks are bounded by
  *tokens*, not rows: attention is quadratic in length, so 32 rows at the 512-token cap
  reached 2,091 MB and was killed by the cgroup while 32 headlines is nothing. `plan_chunks`
  is pure and at module scope so CI checks it without the model.
  **Nothing consumes it yet**, deliberately: a new input moves a pillar for every ticker on the
  night it lands, so it goes in behind a weight-version bump, not beside one. The two corpora it
  is *for* are already stored and already readable through the playground — `social_item` from
  `screener.reddit` and `magpie.document` from `screener.magpie` — and joining either to this is
  the next piece of work. The hard part is not the tone, it is deciding which security a text is
  about; `PLAN.md` holds that and the three other decisions it needs.
- `screener.transcribe` — speech to text, in a container of its own. The client half is
  `httpx` and nothing else and is what the bot and the status service import; the server half
  holds faster-whisper and is the only thing that installs the `voice` extra, so the three
  runtime dependencies are unchanged. Not WhisperX: its forced alignment and diarization are
  what need torch, and neither means anything for one person talking into a phone. A Discord
  voice message is transcribed and answered as though it had been typed; the dashboard's mic
  button puts the transcript in the composer so a misheard ticker is fixed before it is paid
  for. The audio is never written anywhere: it is held for one request and dropped, and
  the transcription's own audit row records a length rather than the words. The question
  itself does reach the trail on the reply row, exactly as a typed one does, because that
  is where Steven's memory lives; the `voice` flag is what tells the two apart.
- `screener.skybird` — live stream capture, in a container of its own. Paste a YouTube or
  Twitch URL and the audio is pulled continuously, cut into 15-second chunks, transcribed by
  the same service the bot and the dashboard use, and stored with the second each phrase was
  said at. Its own `skybird` schema, on the grounds `auth` and `audit` have one. Three shapes
  carry it: **the database is the control plane** — the status service writes a row in
  'requested' and the supervisor polls for it, so there is no internal HTTP surface to
  authenticate and a capture outlives the process running it; **yt-dlp is the platform layer**,
  so a module in `skybird/platforms` only recognises a URL and builds an embed and never
  touches the network, which is what makes the third platform one module plus one row; and
  **audio is never written to a disk**, exactly as in `transcribe`. Watching happens through
  the platform's own player in an iframe — no restreaming. `supervisor` and `capture` are not
  re-exported, because reaching them is one import away from yt-dlp and a subprocess.
  A capture can be **paused**: the ffmpeg goes, the row stays, and it keeps its stream and its
  transcript while freeing its slot against the session cap. `captured_seconds` on the session
  is what lets a resumed capture carry on counting rather than laying a second timeline over
  the first, which is why the clock is in the database and not in the supervisor's memory.
  Steven controls all of this through `bot/tools/skybird.py` — `watch`, `captures`, `hold` —
  and deliberately **cannot read a transcript**: he starts and stops captures, nothing more.
- `screener.auth` — GitHub sign-in for the status service. Sessions live in
  their own `auth` schema, never in `public` with the screener's own tables.
- `screener.health` — stdlib status service; the Cloudflare Tunnel's origin.
  `/health` and `/ready` stay open because the container healthcheck and the
  deploy smoke test cannot hold a session. Everything else requires one
  **unconditionally** — never `config.enabled and login is None`, which makes an
  unconfigured sign-in open the endpoints rather than close them.
  `/api/screen` and `/api/screen/security` serve the scored screen through `screener.screen`,
  on the application's own connection: the SQL is fixed and only bound parameters vary, which
  is not what the playground's read-only roles guard.
- `screener.bot.budget` — the daily spend cap, per person, from `DAILY_SPEND_CAP_USD`
  (default $0.10). Checked before the model is called, folds Discord onto GitHub through
  `DISCORD_USER_MAP` so it cannot be doubled by switching surface, and **fails open** when the
  trail cannot be read — refusing everyone because Postgres blinked is the worse failure.
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
- `screener.blobs` — the payload store. `local` for tests, Cloudflare R2 in production,
  SigV4 hand-rolled because two verbs against one bucket with static credentials is the
  narrow case where that is tractable. Nothing prunes: `ingest_observation.blob_path` is
  `not null` and every score traces back to a stored response.
- `screener.ingest` — daily bars and corporate actions from Yahoo. Backfill is not a mode;
  a security with no rows gets 2020. Two windows: the fetch window widens to close a gap,
  the settling window (7 days) never does, and inside it is the one place the ingest path
  mutates an existing row.
  Fundamentals are the second half, from Yahoo's `fundamentals-timeseries`
  endpoint rather than `quoteSummary`, whose statement modules are now empty.
  It needs no crumb and no cookie, so the client is `ChartClient`'s shape.
  Twenty-eight reported line items are stored; anything Yahoo *computes* from
  lines already stored — EBITDA, free cash flow — is derived at scoring time
  instead. A fact is appended only when its value differs from the latest held,
  and every fact from one payload carries its observation's `fetched_at` as
  `observed_at`, because a per-row clock would let a restatement chain point
  forwards in time. `read_facts` is the point-in-time read and **includes
  `period_type`**: a fiscal Q4 ends when its fiscal year does, so without it a
  year collapses into its own last quarter.
- `screener.nightly` — the scheduler, in a container of its own. One pass of the pipeline a
  night at 23:00 UTC: prices, fundamentals, then scoring, in that order, because ordering by
  construction beats ordering by clock arithmetic. **23:00 rather than the schema spec's 02:00**
  — `--as-of` refuses a past date, so a run after midnight scores a day whose market has not
  opened rather than the one that just closed. Clock-aligned rather than a bare interval, which
  would drift five hours a month past the hour the design turns on, and it asks on boot whether
  tonight is already scored, so a deploy at 23:03 finishes the night instead of losing that date
  for good. Scoring is skipped when prices wholly failed, because `cutoff_offset` filters on
  `observed_at` and yesterday's bars would otherwise produce a complete-looking snapshot of stale
  data. A partial ingest is a success: `CWEN-A` fails every night, and a channel that cries wolf
  nightly is one nobody reads.
- `screener.scoring` — bars and facts into percentiles, three pillar scores and a dated snapshot.
  Eight pure modules and two that open a connection, as `screener.ingest` splits `parse` from
  `load`. Three pillars: Momentum from bars, Valuation and Quality from ten ratios over stored
  line items, under `weight_version` v2 at equal weight. **A ratio never mixes two periods**: flow
  items are four consecutive quarters or one annual figure for *every* input to that ratio,
  balance items are read at a date, and anything outside the bounds is absent, never imputed
  (`basis.py`, spec `docs/specs/2026-09-13-ratios.md`). Ratios are yields, so a loss-maker ranks
  as expensive rather than on top. Which ratios apply is decided by industry — banks and insurers
  get book yield and ROE, REITs get FFO yield — while percentiles stay at sector level. Yahoo
  reports capex negative, so free cash flow adds it; and it restates share counts for splits, so
  no split factor is applied and market cap is absent for a week after one, or when the latest
  close is more than a week old. The peer floor is counted per metric, and a thin bucket ranks
  against every producer in the market. `min_coverage` counts a weighted pillar that produced
  nothing as 0. The market-cap, ratio and momentum functions each have an explaining form
  (`explain_market_cap`, `explain_ratios`, `explain_momentum`) that returns a value or
  `Absent(reason)`; the scoring functions are filters over them, so a reason can never drift
  from the rule that produced the absence, and `tests/test_scoring_golden.py` pins that what
  scoring writes did not change. An empty 52-week bar window is one such absence rather than the
  `ValueError` it used to raise, so it no longer fails the whole night. **Every run writes
  `emits_alerts = false`** — deduplication and the per-ticker cooldown do not exist yet.
  Percentiles are computed within a security's *sector* group, reached
  by walking `sector_node.parent_id` up from the level-2 industry node every `security_sector`
  row points at. The whole night is one transaction,
  deliberately unlike ingest's per-security commits: a half-scored day would read as a crossing
  for every security that never got scored. Adjustment is total return — splits and dividends,
  anchored at the present — and is the one piece of arithmetic here where a wrong answer looks
  entirely plausible, so it is a pure function with its own tests. **Yahoo's close is already
  split-adjusted as of the fetch**, so a split adjusts only bars whose `observed_at` precedes it;
  applied to every earlier bar it double-counted, and APH's +9% read as +118.6%. A run that dies
  is settled rather than left to block its date: `reconcile` under an advisory lock, on skybird's
  terms.
  The three read-only roles already hold `select` on all four derived tables from 013, 017 and 018, so the console,
  Steven's `sql` tool and the claude.ai connector see real scores the night this first runs,
  with no migration and no code change.
- `screener.screen` — the scored screen, read for the dashboard's two endpoints and, through
  `playground.select`, for Steven's chart. `queries` holds every statement as a literal; `rows`
  parses a psycopg row or a `playground.Result` row into one typed record, because the
  playground's cells come back as JSON-safe strings; `params` refuses a wrong query-string value
  by name; `shape` builds JSON whose every figure is a decimal string, never a float. `read`
  serves the latest good v2 night, keeps serving a pinned one after a newer lands, and measures
  Δ against the previous night under the same weights. `explain` re-runs scoring's explaining
  forms for one security under `least(cutoff, started_at)` — the run's actual view — and gives
  each metric one status: ok, mismatch, absent, unexpected, refreshed or unchecked. **Display
  closes ignore the cutoff**, because ingest re-stamps the last week of bars every night; a
  reproduction never does. Spec `docs/specs/2026-09-13-ui-swap.md`. A symbol that several
  securities hold resolves to the only active one, because `universe load` leaves a departed
  security's symbol open; two active is ambiguous. Only prices can be `refreshed`: a fact is
  appended, never rewritten.
