-- Reference data for the scoring cycle, seeded by migration rather than by a
-- command, because `metric.code` has to agree with the code computing it. They
-- change together, so they ship together (scoring spec D11).
--
-- Weights are the deliberate exception the schema allows to be tuned in the
-- database without a redeploy. This only establishes the first row.
--
-- `on conflict do nothing` throughout: a migration that fails partway is left
-- unrecorded and the fixed file is re-run, so every statement here has to
-- survive meeting its own output.

-- Five, not six. Event risk is a flag layer rather than a scored pillar --
-- `event_flag_daily` is its table -- so it gets no row here and no weight.
insert into pillar (code, name) values
    ('valuation', 'Valuation'),
    ('quality',   'Quality'),
    ('momentum',  'Momentum'),
    ('sentiment', 'Sentiment'),
    ('insider',   'Insider/Institutional')
on conflict (code) do nothing;

-- All four are higher-is-better, so nothing here sets `higher_is_better` false.
-- The inversion it drives is implemented and tested anyway (spec D6): P/E
-- arrives with fundamentals, and an untested inversion discovered then is
-- expensive in a way that a tested unused one is not.
--
-- `off_52w_high` is <= 0 by construction, 0 meaning *at* the high, which is
-- still higher-is-better.
insert into metric (code, name, pillar_id, unit, higher_is_better, cadence)
select v.code, v.name, p.id, 'ratio', true, 'daily'
  from (values
        ('ret_3m',       '3-month total return'),
        ('ret_6m',       '6-month total return'),
        ('ret_12m',      '12-month total return'),
        ('off_52w_high', 'Distance below the 52-week high')
       ) as v(code, name)
  cross join pillar p
 where p.code = 'momentum'
on conflict (code) do nothing;

-- Selected on by exact description in `screener.scoring.run`, because
-- `scoring_logic_version` has no code column. The string is shared between this
-- file and that one for the same reason `metric.code` is.
insert into scoring_logic_version (description)
select 'v1 momentum: four price metrics, sector percentiles'
 where not exists (
     select 1 from scoring_logic_version
      where description = 'v1 momentum: four price metrics, sector percentiles'
 );

-- {Momentum: 1.0} by necessity, not by judgement: it is the only pillar prices
-- can produce. The first real weighting decision arrives with the second pillar.
insert into weight_version (code, note)
values ('v1', 'Momentum only -- the one pillar daily bars can produce')
on conflict (code) do nothing;

insert into pillar_weight (weight_version_id, pillar_id, weight)
select v.id, p.id, 1.0
  from weight_version v cross join pillar p
 where v.code = 'v1' and p.code = 'momentum'
on conflict (weight_version_id, pillar_id) do nothing;
