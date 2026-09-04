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

- **yfinance** — prices, fundamentals, analyst recommendations (free). Rate-limits hard: eight
  concurrent workers lost 43% of 1,506 requests inside 45 seconds to `YFRateLimitError` and
  Yahoo's anti-bot crumb check, while sequential requests at ~0.35s spacing recovered 99.4%.
  Budget ~0.8s per symbol — 40 minutes for 3,000 — and throttle with backoff rather than
  parallelising. Classification changes rarely and does not need pulling daily.
- **Finnhub** — ratings, news, short interest (free tier, ~60 calls/min)
- **Alpha Vantage** — news endpoint carries a per-article sentiment score (free tier)
- **FINRA** — short interest, twice monthly, downloadable
- **SEC EDGAR / Companies House** — filings
- **Reddit official API** — free tier covers low-volume r/stocks, r/wallstreetbets pulls

**Rejected — Bright Data.** An enterprise proxy network for defeating aggressive anti-bot
defences at scale. API-first ingest means there is no bot problem to solve. If a target later
hard-blocks, a cheap residential proxy or ScrapingBee-tier service is the proportionate answer.

**Rejected for now — Apify.** Free plan is $5/month of credits; Starter is $29. The real risk is
that many Store Actors charge per-result or per-event fees *on top of* compute — exactly the
pattern that bites when scraping social sentiment daily across hundreds of tickers. Light
scrapers with `httpx` + `selectolax` on a timer cost effectively nothing.

Both rejections are open to a case being made.

## Data storage

**Universe:** 500 tickers growing to ~3,000, with 1,000 a better starting floor than 500 — at
500 the thinnest sector holds exactly 20 names, sitting on the minimum rather than above it.

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
universe). Unparsed source JSON goes to Azure Blob as gzipped files, with retention set by a
storage-account lifecycle rule. Never conflate the two under "raw data".

Payloads stay out of Postgres because Azure Flexible Server provisioned storage ratchets — it
grows, never shrinks, and deleted rows do not return space without `VACUUM FULL`. A payload
table peaking at 15 GB permanently raises the floor even after expiry. AWS was considered and
offers no advantage: RDS has the same one-way ratchet, and only serverless-storage engines
(Aurora, Neon) escape it. Once payloads are in Blob the limit is never approached, which
removes the reason to migrate.

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
- **Azure** for database and storage — Postgres for snapshots, Blob for raw payloads. Chosen
  partly for CV relevance, since Azure is used at work.
- **Azure Functions consumption plan considered and downgraded**: 1.5 GB RAM and a slow vCPU
  gives ~5–10 texts/sec for FinBERT plus a cold-start model load. Fine for light orchestration,
  wrong for the scoring job.
- Running cost target ~£5–10/month.
- Acceptable v1 shortcut: run the daily job locally on cron / Task Scheduler and push results to
  the cloud database.

## Standing constraints

- No price prediction. Weak/Strong buy/sell recommendations come later. Surface candidates and evidence.
- Every score traces to visible raw inputs.
- APIs before scrapers; scrapers are the fragile layer.
- Prefer boring, cheap and honest over impressive-sounding. Where a better tool exists for a
  specific job, take it — the stack is not the point of the project.
