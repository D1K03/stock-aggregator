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

## Done

**Database schema** — merged in #1. Nine migrations, ~20 tables across identity, a bitemporal
fact layer, a partitioned derived-daily layer, versioned scoring runs and alerting. 44 tests run
against a real Postgres 16 and demonstrate the load-bearing claims rather than asserting them:
that a bare-date cutoff silently drops the overnight fetch, that restatements insert rather than
overwrite, that only one live run may cover a date, that a partial unique index cannot prevent
overlapping validity periods, and that yearly and monthly partitions coexist on one parent.

Spec: `docs/specs/2026-09-04-database-schema-design.md`. Plan: `docs/plans/2026-09-04-database-schema.md`.

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
2. **Ingest** — yfinance prices and fundamentals into the bitemporal fact layer, Blob payload
   writing, content-hash dedup. First real use of `cutoff_offset`.
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
- **Rate limiting must be designed in, not retrofitted.** Eight concurrent yfinance workers lost
  43% of requests in 45 seconds. Sequential with backoff is the shape; budget ~0.8s per symbol.

## Open parameters

- **Minimum peer count — settled at 20**, but as a safety check rather than a routine mechanism,
  since v1 groups at sector level where every sector clears it.
- **Universe size — start nearer 1,000 than 500.** At 500 the thinnest sector (Communication
  Services) holds exactly 20 names, sitting on the floor rather than above it.
- Coverage floor for alert suppression, per rule in `alert_rule.min_coverage` — still open, needs
  real metric coverage to settle.

## Status

Schema, CI and contributor docs merged and green on `main`. Sector reconnaissance done. No
ingest, scoring or alerting code exists yet.
