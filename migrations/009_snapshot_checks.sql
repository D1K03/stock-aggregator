-- snapshot_daily.pillar_agreement, min_coverage and worst_fallback_level carried
-- no check constraints even though the analogous columns on metric_daily and
-- pillar_score_daily do. 006 is already applied in any existing database and
-- the runner never re-applies a recorded migration, so this lands as a new
-- migration rather than an edit to 006.
alter table snapshot_daily
    add constraint snapshot_daily_pillar_agreement_check
        check (pillar_agreement >= 0);
alter table snapshot_daily
    add constraint snapshot_daily_min_coverage_check
        check (min_coverage between 0 and 1);
alter table snapshot_daily
    add constraint snapshot_daily_worst_fallback_level_check
        check (worst_fallback_level in (0, 1, 2));
