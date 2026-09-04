# Design decisions

Why the system is shaped the way it is. Stable — this changes only when a decision is reversed,
and a reversal should replace the entry rather than append to it. Current scope and open
questions live in `PLAN.md`. Full table definitions live in
`docs/specs/2026-09-04-database-schema-design.md`.

## Value proposition

Transparent evidence aggregation. Every score traces back to visible raw inputs. The tool
surfaces candidates and the evidence behind them; it does not recommend trades.

Price forecasting was considered and deliberately deferred. If added later it sits *on top* of
the screener as another aggregated input — reference data to compound with pillar scores, not a
prediction the system stands behind. Consensus from reputable forecasters is a candidate input
in the same spirit: reference, not advice.

## Pillars

Signals are grouped into pillars that measure genuinely different things. Stacking ten
correlated signals (analyst target + rating + momentum + news tone are all downstream of "price
went up") produces one signal with ten names and false confidence.

| Pillar | Example metrics | Question it answers |
|---|---|---|
| Valuation | P/E vs sector median, EV/EBITDA, FCF yield, PEG | Is it cheap for what it is? |
| Quality | ROIC, gross margin trend, debt/equity, interest cover, earnings consistency | Is this a good business? |
| Momentum | 3/6/12m relative return, earnings revision trend, distance from 52w high | Is the market agreeing? |
| Sentiment / expectations | Analyst consensus, target upside, short interest, news tone | What is priced in? (noisiest) |
| Insider / institutional | Net insider buying, 13F holding changes | What are informed holders doing? |
| Event risk | Days to earnings, litigation, dividend cut history | Penalty/flag layer, not a score |

## Scoring rules

- Normalise each metric to a **percentile within its sector** — 15x P/E means opposite things
  for a utility and a chipmaker. Average within pillar, then weight pillars into a blend.
- Pillar weights live **in the database**, so they can be tuned without a redeploy.
- Sentiment is weighted **low**. It is noisy and mostly lags price; treat it as a crowding
  warning rather than a buy signal.
- Surface **pillar agreement** — how many pillars are top-quartile simultaneously — as its own
  column. Strength in three pillars at once is rarer and more interesting than a top decile in
  one.
- Log every score daily so weights can be backtested against forward returns later.
- **A missing metric is dropped from its pillar average, never imputed.** Free-tier API coverage
  is ragged and a percentile for an absent value is undefined. Imputing to the sector median
  invents data; refusing to score loses the ticker. Dropping changes a pillar's meaning
  silently, so coverage is stored alongside every pillar score and alerting is gated on a
  coverage floor.

Three consequences that constrain the schema:

- **Percentiles need peers, and v1 scores at sector level only.** Measured against the S&P 1500
  with real yfinance labels: at 500 tickers, 95% of names sit in an industry group holding fewer
  than 20 peers; even at 1,500 it is 59%. Sector groups clear 20 members at every universe size.
  Industry-level scoring is a later refinement, not a v1 feature, and `min_peers = 20` is a
  safety check rather than a routine mechanism.

  Mixing levels is the trap worth avoiding: "top decile among 8 industry peers" and "top decile
  among 238 sector peers" produce the same number from very different evidence, and a blended
  score would compare them as though they were equivalent — a quieter version of the problem the
  pillar split exists to prevent. The schema already carries `peer_group`, `fallback_level` and
  `peer_count`, so adding an industry rung later is additive.

  Note also that yfinance offers exactly two levels — 11 sectors and ~140 industries, the latter
  at GICS *sub-industry* granularity. There is no intermediate rung to fall back through; a
  middle grouping would have to be built by hand.
- **A score can move because peers moved.** Persist the raw metric next to its percentile, or
  an alert cannot distinguish "revisions came in" from "the sector re-rated around it".
- **The blended score is derivable**, from per-pillar scores plus the weight set in effect.
  Stamp each snapshot with a weight version and recompute rather than storing the blend as
  ground truth; historical snapshots then stay interpretable after a weight change, and weight
  backtesting becomes a query instead of a re-run.

Caveat on backtesting: yfinance fundamentals are restated, not point-in-time, so history cannot
be backfilled into a credible backtest. Only the forward log of daily snapshots counts.

## Data sources

APIs first. They are legal, stable, and cheaper to maintain. Scrape only where no API exists,
respect robots.txt and ToS, and treat scrapers as the fragile layer.

- **Yahoo Finance, called directly** — prices, fundamentals, analyst recommendations (free).
  Two endpoints with different requirements, both measured:
  `/v8/finance/chart` (prices) needs no authentication at all, just a User-Agent;
  `/v10/finance/quoteSummary` (everything else) needs a crumb, obtained by fetching a cookie
  from `fc.yahoo.com` then a token from `/v1/test/getcrumb` and appending it to the query.

  A single `quoteSummary` call returns profile, all three financial statements, key statistics,
  financial data, earnings trend and recommendation trend together — 19 KB raw, 5 KB gzipped.
  A year of daily prices is 28 KB raw, 8 KB gzipped.

  **Direct beats the library, but on structural grounds rather than a throughput race.** One
  request per ticker instead of several, a persistent session, and a crumb fetched once and
  reused are wins that hold regardless of what any timing run shows. The first comparison drawn
  here — eight concurrent yfinance workers losing 43% of requests against 30 sequential direct
  ones losing none — was not evidence for anything: it varied concurrency and request count at
  the same time.

  **Measured at full universe scale:** 1,506 sequential direct `quoteSummary` requests from one
  IP completed in 182 seconds, 0.121s each, with 1,505 succeeding. The single failure was a 404
  on a share-class symbol, not a limit. Mean latency per 300-request block stayed flat at
  0.119–0.122s from the first block to the last, so nothing throttled or degraded across the run.

  **What that still does not establish**, and should not be quoted as though it does:

  - It ran from a development machine, not the VPS that will run the nightly job. Per-IP limits
    attach to that IP, not to this code.
  - A night's work is roughly double this: fundamentals *and* prices for every ticker. Prices
    need no crumb and may sit in a different bucket, but 3,000 requests is untested.
  - The run took three minutes, so the crumb never expired and the refresh path never executed.
    Over a longer job it will, routinely.

  Throttle as a safeguard — the measurements say it is not needed at this scale, and they were
  taken in conditions the real job will not exactly reproduce.
- **Finnhub** — ratings, news, short interest (free tier, ~60 calls/min)
- **Alpha Vantage** — news endpoint carries a per-article sentiment score (free tier)
- **FINRA** — short interest, twice monthly, downloadable
- **SEC EDGAR / Companies House** — filings
- **Reddit official API** — free tier covers low-volume r/stocks, r/wallstreetbets pulls

**Bright Data — available, off by default.** Previously rejected outright, on the grounds that
an enterprise proxy network exists to defeat aggressive anti-bot defences at scale and API-first
ingest has no such problem. That reasoning still holds, and the default fetch strategy is a
direct request with no proxy anywhere in it. What reversed the rejection is price, not need: the
proportionate answer the original entry named — a cheap residential proxy — is already
provisioned and paid for on the same VPS for a sibling project, so having it available and
switched off costs nothing. It is a per-source opt-in, never a default, and the spend cap is set
in Bright Data's dashboard rather than enforced in code.

**Rejected for now — Apify.** Free plan is $5/month of credits; Starter is $29. The real risk is
that many Store Actors charge per-result or per-event fees *on top of* compute — exactly the
pattern that bites when scraping social sentiment daily across hundreds of tickers. Light
scrapers with `httpx` + `selectolax` on a timer cost effectively nothing.

Both rejections are open to a case being made.

## Data storage

**Universe: the S&P 1500 is the v1 ceiling**, not a waypoint to 3,000. The three constituent
lists are the only free complete source, they stop at 1,500, and everything below the S&P 600's
~$1bn floor spreads into the thin tail of the sector distribution rather than the fat middle —
so growth past 1,500 would worsen peer counts per sector at the bottom, not improve them. A
larger universe is a post-v1 decision that needs a paid constituents source to be worth making.

Within that, start nearer 1,000 than 500: at 500 the thinnest sector holds exactly 20 names,
sitting on the minimum rather than above it.

Classification comes from yfinance and only yfinance. Its sector labels agree with GICS for just
92% of the S&P 1500: yfinance "Technology" absorbs payment processors that GICS files under
Financials, and "Consumer Cyclical" takes names GICS puts in Materials and Industrials. A peer
group built from one taxonomy and scored against metrics grouped by another would disagree with
itself. US-only initially, UK planned — so identity is a
synthetic `security_id` with symbol history, and `currency`/`mic` exist from day one even while
every row says USD. Symbols are attributes, never keys: FB became META, and retired symbols get
reissued.

**"Raw metrics" and "raw payloads" are different things, and the retention rules differ.**
Parsed metric values are canonical, immutable and stay in Postgres forever (~2–3 GB/yr at full
universe). Unparsed source JSON is gzipped and stored outside the database, with its own
retention. Never conflate the two under "raw data".

Payloads stay out of Postgres regardless of where the database runs. Deleted rows do not return
their space without `VACUUM FULL`, so a payload table peaking at 15 GB permanently raises the
floor even after expiry — on a managed service that is a bill that never goes down, and on the
VPS volume it is disk that never comes back. `ingest_observation.blob_path` records where a
payload went, which keeps that decision out of the schema.

**Where payloads actually land is open.** Azure Blob was the answer while the database was
Azure; it no longer is. The candidates are a volume on the VPS with a pruning job, or an
object store billed per GB. The ingest spec has to settle it, because that is the first thing
that will write one.

Ingest hashes each response and skips the blob write when unchanged, since most fundamentals
endpoints return full history on every call. The observation row is still written every time —
otherwise the record of what was known on a date is lost — and a changed hash for a period
already held is the restatement detector.

**Facts are bitemporal and append-only.** Every fundamental carries `period_end` (what it
describes) and `observed_at` (when it was learned); a restatement is an insert, never an update.
For the same reason prices are stored raw with a separate corporate-actions table: providers
rewrite adjusted prices retroactively after every split.

Which observations a scoring date may see is bounded by an **interval offset from that date**,
stamped on the run — not by a fixed timestamp, which across a multi-day backfill would let an
old date see knowledge acquired years later. Live and backfill therefore evaluate the identical
expression, which is the property that makes backtesting the scoring logic mean anything. Note
that `observed_at` records when *we* learned something, not when it became public, so the log
lags publication by up to a day and biases backtests pessimistic — the safe direction.

**Scores are versioned, not overwritten.** Pillar scores are stored per scoring run; a fixed
scoring bug lands as a new run beside the old one. The decisive reason is specific to alerting —
a crossing is a property of an adjacent *pair* of scores, so recomputing in place retroactively
changes whether a crossing ever happened, turning fired alerts into alerts that shouldn't have
been with no record of either.

Two scores are comparable only under the same scoring-logic version and weight version, so
crossings compare against the most recent prior snapshot sharing both. Comparability keys on a
deliberately bumped version integer rather than the git sha, which would otherwise let an
unrelated commit silently suppress a night of alerts. Changing either version means backfilling
the previous day with alerting disabled before resuming live.

## Sentiment scoring

- **VADER** — dictionary lookup, not a model. Thousands of texts/sec on any CPU. Cheap baseline
  tone.
- **FinBERT** (`ProsusAI/finbert`, ~110M params) — finance-tuned BERT-base, ~500 MB RAM loaded,
  roughly 20–50 short texts/sec batched on a modern desktop CPU. ~10,000 headlines a day is a
  few minutes of CPU.
- **Export FinBERT to ONNX** and run under `onnxruntime` — typically 2–3x faster on CPU than
  PyTorch, no accuracy loss, easier to package.
- **No GPU.** The dev machine has an AMD RX 9060 XT; CUDA is unavailable and ROCm is more faff
  than the speedup justifies at this volume.
- **Never use an LLM to output a sentiment number.** LLMs are inconsistent at numeric scoring and
  cost money for something a free classifier does better.

## LLM usage

Narrative extraction only — the unstructured work FinBERT cannot do: condensing earnings-call
transcripts, extracting guidance changes from filings, turning 10-K risk sections into structured
flags.

**OpenRouter** as the front door: one API across many models, easy to swap and compare on
cost/quality. A cheap model (Gemini Flash / GPT-4o-mini / Haiku class) is fractions of a penny
per document.

## Alerting

**Threshold crossings, not buy timing.** Intraday timing ("buy at 14:30 Tuesday") was rejected —
the inputs are daily fundamentals, revisions and filings, none of which supports hour-level
precision. Inventing it would repeat the price-forecasting mistake.

Alert on conditions, not clocks: a score crossing a threshold, a pillar flipping neutral to
strong, an analyst revision cluster, insider buying appearing, entry into a target valuation
band.

Alerts carry their reasoning. Good:
`NVDA 68 -> 82. Quality 91, Valuation 44, Momentum 88. Driver: three upward revisions in 5 days. Flag: earnings in 6 days.`
Bad: `NVDA: 82`. This requires per-snapshot pillar scores and a per-pillar delta so the alert can
name what moved.

Wording is always "score crossed threshold", never "STRONG BUY" — partly honesty, partly because
a tool that shouts BUY reads as a toy while one that shows evidence reads as engineering.

**Deduplication and a cooldown window are required from day one.** Fire on the crossing, not the
state, and do not re-alert the same ticker for N days. Without this a score hovering near a
threshold fires daily and the channel is muted within a week.

Delivery is a single HTTP POST to a webhook. No OAuth, no bot hosting.

## Infrastructure

- **VPS** (Hetzner CX22 / Netcup / small droplet, ~£4–6/month) for always-on work: cron,
  scrapers, FinBERT resident in memory, no cold starts.
- **Postgres on the VPS**, in the same compose stack, on a named volume. Previously Azure —
  Postgres for snapshots and Blob for raw payloads, chosen partly for CV relevance. Reversed
  because the deployment it has to fit is one VPS that already runs the tunnel, the tailnet and
  the secrets client: adding a managed database to that buys a firewall allowlist, a second bill
  and a network hop, for a workload measured in single-digit gigabytes per year. The cost of the
  reversal is real and is not yet paid — nothing takes a backup on our behalf any more.
- **Azure Functions consumption plan considered and downgraded**: 1.5 GB RAM and a slow vCPU
  gives ~5–10 texts/sec for FinBERT plus a cold-start model load. Fine for light orchestration,
  wrong for the scoring job.
- Running cost target ~£5–10/month.
- Acceptable v1 shortcut: run the daily job locally on cron / Task Scheduler and push results to
  the cloud database.

**Secrets live in Infisical**, fetched into the process environment at startup by a machine
identity. The only credentials on the server are the three that authenticate that exchange;
everything else is fetched with them and never touches disk. A missing identity is a silent
no-op so local development and CI read a `.env` as normal, but a *failed* fetch is fatal —
starting with half a configuration means failing later, somewhere less obvious.

**Ingress is a Cloudflare Tunnel**, which dials outward. Nothing listens on the public interface
and there is no firewall rule to maintain. The cost is that the hostname mapping lives in
Cloudflare's dashboard rather than in the repository; accepted because there is one rule, and
the alternative puts a credentials file on the box.

**Deploys reach the box over Tailscale.** The workflow joins the tailnet as an ephemeral tagged
node for the length of the job, so no CI credential is usable from the public internet. The
credential is a tagged, reusable, ephemeral auth key, because Tailscale does not expose OAuth
client creation through its API and an auth key is the strongest thing that can be provisioned
without clicking through the admin console. Auth keys expire, and the sibling project has already
lost time to one failing as a connection timeout that reads like a network fault — so the deploy
workflow checks the expiry date and fails with an explicit message before the join is attempted.
An OAuth client is a strict improvement whenever someone makes one. Note that the VPS still accepts SSH on its public
interface; closing port 22 to everything but the tailnet is a hardening step this deploy is ready
for but does not depend on.

**Deployment is a container image, built by CI and pulled by the VPS.** The commit is baked in at
build time as `SCREENER_GIT_SHA`, because a container has no git history and `scoring_run.git_sha`
is `not null`. Migrations run in the container's entrypoint under a Postgres advisory lock, so
the running image is authoritative about its own schema and two containers starting at once
cannot race each other into a duplicate-table crash loop. Rolling back across a migration is not
supported: there are no down migrations.

## Standing constraints

- No price prediction. Weak/Strong buy/sell recommendations come later. Surface candidates and evidence.
- Every score traces to visible raw inputs.
- APIs before scrapers; scrapers are the fragile layer.
- Prefer boring, cheap and honest over impressive-sounding. Where a better tool exists for a
  specific job, take it — the stack is not the point of the project.
