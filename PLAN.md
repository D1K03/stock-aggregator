# Plan

Current scope and what is being worked on. Mutable — update as milestones land. Rationale for
the decisions referenced here lives in `DESIGN.md`.

## v1 — the spine

Build this and nothing else first:

1. Daily ingest for a defined ticker universe
2. Compute pillar scores (sector-relative percentiles)
3. Persist a dated snapshot with per-pillar scores and raw inputs
4. Diff today's snapshot against yesterday's
5. Post threshold crossings to Discord with the pillar breakdown and event-risk flags

## Deferred

Additive once the spine works, in no fixed order: web UI, backtesting harness, LLM
summarisation, 13F ingestion, expanded universe, forecaster-consensus aggregation.

### Steven draws charts — built on concept data

Built, on the explicit understanding that the numbers are invented. Ask when
something happened, how a ticker has moved, or when it crossed the threshold,
and the `chart` tool draws the 60-day line under the reply with that point
marked and dated.

Two decisions are worth keeping when this meets real data:

- **The series never enters the model's context.** Sixty points exceed the whole
  tool-result budget and a tool result is re-sent on every following round, so
  the data would be paid for repeatedly to tell the model something it reads
  worse than a reader does. The tool returns one sentence; the chart travels
  beside the reply through `tools.collecting()`. Cost is flat in the size of
  the series.
- **The model picks the question, the data answers it.** `mark` names *what* to
  find — `peak`, `low`, `surge`, `drop`, `crossing`, `latest` — and the index is
  computed from the series in `bot/tools/charts.py`. A model supplying
  coordinates would be inventing where the marker goes, which is the same
  failure as inventing a number and worse for being drawn precisely.

`screener.concept` holds the invented data, mirroring `web/lib/data.ts` because
the two live in separate Docker build contexts and neither can import the
other's copy. `tests/test_charts.py` parses the TypeScript and fails if they
disagree, and pins `series()` against values from the original walk — so the
duplication is checked rather than trusted. Delete the package when ingest
lands; the tool then reads `score_snapshot` and nothing else about it changes.

Still not built, and still blocked on ingest:

- **Real data.** Everything above draws fiction. Every surface says so — the
  chart footer, the tool result, and the system prompt — and that wording is
  load-bearing until there are real snapshots behind it.
- **A tool over the snapshot tables**, replacing `screener.concept`, so the same
  analysis works from Discord where there is no screen to look at.
- **Structured multi-series context.** `web/lib/screen-context.tsx` publishes a
  prose summary, which is enough for "what am I looking at" and not enough to
  compare two tickers or reason across pillars.
- **The constraints do not move.** "Crossed 75 on the 3rd, driven by Momentum"
  is analysis; "looks like it is about to run" is not. Adding real data is
  exactly the change that would tempt the second.

## Done

**Database schema** — merged in #1. Nine migrations, ~20 tables across identity, a bitemporal
fact layer, a partitioned derived-daily layer, versioned scoring runs and alerting. 44 tests run
against a real Postgres 16 and demonstrate the load-bearing claims rather than asserting them:
that a bare-date cutoff silently drops the overnight fetch, that restatements insert rather than
overwrite, that only one live run may cover a date, that a partial unique index cannot prevent
overlapping validity periods, and that yearly and monthly partitions coexist on one parent.

Spec: `docs/specs/2026-09-04-database-schema-design.md`. Plan: `docs/plans/2026-09-04-database-schema.md`.

**Infrastructure roots** — secrets from Infisical, a fetch chain with Bright Data as an opt-in
strategy, an OpenRouter client, a notification channel protocol with a Discord webhook, a status
service behind a Cloudflare Tunnel, and CI/CD onto the VPS. Roots only: no source adapters, no
daily job, no alert content. `screener.boot` now applies migrations under an advisory lock and
pre-creates partitions, which gives `ensure_partitions` its first caller.

Ingest inherits `screener.fetch` and `screener.secrets` rather than inventing its own, and
`screener.provenance` supplies the `git_sha` and `config_hash` that `scoring_run` requires.

Spec: `docs/specs/2026-09-04-infrastructure-foundation.md`. Plan: `docs/plans/2026-09-04-infrastructure-foundation.md`.

## Done — sector distribution reconnaissance

Measured against the S&P 1500 with real yfinance labels, 1,501 of 1,506 symbols resolved.

| universe | level | groups | median members | tickers with >=20 peers |
|---|---|---|---|---|
| 500 | sector | 11 | 33 | 100% |
| 500 | industry | 111 | 3 | 5% |
| 1500 | sector | 11 | 112 | 100% |
| 1500 | industry | 140 | 8 | 41% |

**Industry-level scoring is not viable at this scale.** At 500 tickers a 20-peer floor pushes
95% of names to sector; at 1,500 it is still 59%. yfinance offers only two levels — 11 sectors
and ~140 industries at GICS sub-industry granularity — so there is no intermediate rung to fall
back through. v1 scores at sector level, which clears 20 members everywhere. Reasoning in
`DESIGN.md`.

Scripts were throwaway and were not kept.

## Then, in dependency order

Each needs its own brainstorm → spec → plan cycle; they are too big for one.

1. **Universe and identity** — pick the ticker list and populate `security`,
   `security_symbol`, `security_sector`, `sector_node`, `peer_group`. Everything else needs
   securities to exist. The taxonomy is settled (yfinance, two levels, sector groups only for
   v1); what remains is choosing the list and handling the symbols that do not resolve.
2. **Ingest** — prices and fundamentals into the bitemporal fact layer, payload writing,
   content-hash dedup. First real use of `cutoff_offset`. Settles where payloads land, which
   `DESIGN.md` now records as open. Inherits `screener.secrets`, and `screener.fetch` for every
   source but Yahoo: the crumb is only valid alongside the cookie issued with it, so the Yahoo
   path holds one session for the run — `screener.universe.sources.yahoo` already does this and
   the ingest client should extend that shape rather than start again.

   **Not yfinance.** The library emits parsed DataFrames, so a stored payload would be its
   reshaping rather than the response — which breaks the content-hash restatement detector and
   leaves "did the API lie or did our parser?" unanswerable. Direct also returns profile,
   statements, key statistics, earnings trend and recommendation trend in one `quoteSummary`
   call. Reasoning in `DESIGN.md`.
3. **Scoring** — metric computation, percentile within sector peer group, pillar aggregation
   with the coverage gate. No fallback walk in v1: every sector clears the peer floor, so
   `fallback_level` is recorded as sector throughout and the ladder stays unexercised until an
   industry rung is added.
4. **Diff and alerting** — crossing detection against the last comparable snapshot, cooldown,
   the Discord POST.

## Carried forward

- **The `emits_alerts = false` skip has no regression test.** Postgres cannot express it as a
  cross-table check, so it is an invariant the alerting code owns. The `alert_event` unique
  constraint is a backstop, not the mechanism. This must appear as an explicit requirement in the
  alerting spec, or a backfill run will eventually fire alerts into the channel.
- **Share-class symbols are lossy.** Wikipedia's `CWEN.A` becomes `CWEN-A`, which 404s at Yahoo.
  A handful of names will not resolve; handle it in the identity work rather than discovering it
  mid-ingest.
- **Rate limiting is a safeguard, not a known requirement.** The earlier figure here — eight
  concurrent yfinance workers losing 43% of requests — was withdrawn in `DESIGN.md`: it varied
  concurrency and request count at once and so was evidence for neither. What replaced it: 1,506
  sequential direct `quoteSummary` requests completed in 182s, 0.121s each, 1,505 succeeding,
  with per-block latency flat from first to last. Sequential with backoff is still the shape, and
  a throttle is still worth building — but as a safeguard against conditions the measurement did
  not cover, not against a limit anyone has hit.
- **Three things that measurement does not establish**, and the ingest spec should settle: it ran
  from a development machine rather than the VPS whose IP the nightly job will use; a night is
  roughly double it, since prices need fetching too and 3,000 requests is untested; and at three
  minutes the crumb never expired, so the refresh path never ran. Worth measuring from the box
  before ingest is designed around the numbers above.

## Open parameters

- **Minimum peer count — settled at 20**, but as a safety check rather than a routine mechanism,
  since v1 groups at sector level where every sector clears it.
- **Universe size — start nearer 1,000 than 500.** At 500 the thinnest sector (Communication
  Services) holds exactly 20 names, sitting on the floor rather than above it.
- Coverage floor for alert suppression, per rule in `alert_rule.min_coverage` — still open, needs
  real metric coverage to settle.

## Status

Schema, infrastructure, CI and contributor docs merged and green on `main`. Sector reconnaissance done. No
ingest, scoring or alerting code exists yet — `python -m screener.boot selftest` is the only thing that currently exercises the
infrastructure end to end, and it is worth running after a deploy for exactly that reason.
