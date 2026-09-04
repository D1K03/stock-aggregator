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

## Next — sector distribution reconnaissance

Before building ingest, pull nothing but the sector and industry label for the candidate universe
and count members per sector. An hour of work, no schema writes, no scoring.

This is the cheapest way to test the assumption the whole scoring model rests on. Percentiles are
taken *within sector*; if several sectors hold four names, those scores are noise and nothing
downstream will say so. Settling it now also converts the two open parameters below from guesses
into measurements. Finding it out after ingest and scoring are built means rework.

## Then, in dependency order

Each needs its own brainstorm → spec → plan cycle; they are too big for one.

1. **Universe and identity** — populate `security`, `security_symbol`, `security_sector`,
   `sector_node`, `peer_group`. Everything else needs securities to exist, and the sector
   taxonomy and fallback walk get decided here.
2. **Ingest** — yfinance prices and fundamentals into the bitemporal fact layer, Blob payload
   writing, content-hash dedup. First real use of `cutoff_offset`.
3. **Scoring** — metric computation, percentile within peer group, pillar aggregation with the
   coverage gate. The peer-group fallback walk lives here.
4. **Diff and alerting** — crossing detection against the last comparable snapshot, cooldown,
   the Discord POST.

## Carried forward

- **The `emits_alerts = false` skip has no regression test.** Postgres cannot express it as a
  cross-table check, so it is an invariant the alerting code owns. The `alert_event` unique
  constraint is a backstop, not the mechanism. This must appear as an explicit requirement in the
  alerting spec, or a backfill run will eventually fire alerts into the channel.
- **Thin-sector fallback is untested against real data.** The first ~100-ticker run will not
  exercise it; it needs a deliberate test rather than waiting for production to reveal it.

## Open parameters

Settled against real data, not by design:

- Minimum peer count for the fallback walk — start at 20 and tune.
- Coverage floor for alert suppression, per rule in `alert_rule.min_coverage`.
- Universe composition — 3,000 tickers must be spread so sectors have workable peer counts.

## Status

Schema merged and green on `main`. No ingest, scoring or alerting code exists yet.
