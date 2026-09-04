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

## Current task — schema implementation plan

The schema is designed and agreed:
`docs/specs/2026-09-04-database-schema-design.md`. ~20 tables across identity, a bitemporal
fact layer, a partitioned derived-daily layer, versioned scoring runs, and alerting. Roughly
36M rows and 3–4 GB a year at the 3,000-ticker ceiling.

Next step is an implementation plan: migration ordering, the partition pre-creation job, and
which slice to build first.

Settled during design, previously open here:

- Metric detail is narrow relational, not JSONB — the deferred backtest and web UI are both
  cross-sectional metric queries.
- Weights version in `weight_version`/`pillar_weight`; the blended score is a materialised
  derivation stamped with its weight version.
- Raw payloads go to Blob, not a JSONB column. Postgres holds parsed values only.
- Alert state is `alert_state` (cooldown, direction) plus immutable `alert_event` rows carrying
  the frozen payload.

Still parameters rather than design, to settle against real data:

- Minimum peer count for the fallback walk — start at 20 and tune.
- Coverage floor for alert suppression, per rule.
- Universe composition. 3,000 tickers must be spread so sectors have workable peer counts, and
  the first-fortnight ~100-ticker run will not exercise thin-sector fallback — it needs a
  deliberate test.

## Status

No code on disk yet. The schema is specified but no migrations are written.
