-- Valuation and Quality: ten ratios, the v2 logic and weights, and the column
-- that says which period a ratio describes.
--
-- Spec: docs/specs/2026-09-13-ratios.md.
--
-- `metric.code` agrees with `screener.scoring.ratios.RATIO_CODES` by hand, for the
-- reason 019 gives for momentum: the seed ships with the code that computes it.
-- Nothing here is an input -- `is_input` stays false -- so fundamentals ingest's
-- `metric_ids`, which filters on it, can never write a ratio as a fact.
--
-- Valuation is `daily` because its denominator moves with the price. Quality is
-- `quarterly` because only a new fact moves it, even though it is recomputed
-- every night.

insert into metric (code, name, pillar_id, unit, higher_is_better, cadence)
select v.code, v.name, p.id, 'ratio', v.higher_is_better, v.cadence
  from (values
        ('earnings_yield', 'Earnings yield',              'valuation', true,  'daily'),
        ('ebitda_ev',      'EBITDA to enterprise value',  'valuation', true,  'daily'),
        ('fcf_yield',      'Free cash flow yield',        'valuation', true,  'daily'),
        ('book_yield',     'Book value to market cap',    'valuation', true,  'daily'),
        ('ffo_yield',      'Funds from operations yield', 'valuation', true,  'daily'),
        ('roic',           'Return on invested capital',  'quality',   true,  'quarterly'),
        ('roe',            'Return on equity',            'quality',   true,  'quarterly'),
        ('gross_margin',   'Gross margin',                'quality',   true,  'quarterly'),
        -- The only metric where less is better: leverage is a risk, not a return.
        ('debt_to_equity', 'Debt to equity',              'quality',   false, 'quarterly'),
        ('interest_cover', 'Interest cover',              'quality',   true,  'quarterly')
       ) as v(code, name, pillar, higher_is_better, cadence)
  join pillar p on p.code = v.pillar
on conflict (code) do nothing;

-- Selected on by exact description in `screener.scoring.run`, as v1 is. v1 stays:
-- every run already written references it, and `logic_version_id` is what keeps
-- those nights from being compared with these.
insert into scoring_logic_version (description)
select 'v2 momentum, valuation, quality: sector percentiles, industry-applicable ratios'
 where not exists (
     select 1 from scoring_logic_version
      where description = 'v2 momentum, valuation, quality: sector percentiles, industry-applicable ratios'
 );

-- Equal, because any other split claims to know which pillar predicts better and
-- nothing does yet; scores are logged daily so a backtest can set this later.
-- Stored as 1, 1, 1 rather than 0.3333: `blend` normalises over the weights
-- present, so these blend to exactly a third each without a rounded constant.
insert into weight_version (code, note)
values ('v2', 'Equal thirds across Momentum, Valuation and Quality -- no backtest yet to justify another split')
on conflict (code) do nothing;

insert into pillar_weight (weight_version_id, pillar_id, weight)
select v.id, p.id, 1.0
  from weight_version v cross join pillar p
 where v.code = 'v2' and p.code in ('momentum', 'valuation', 'quality')
on conflict (weight_version_id, pillar_id) do nothing;

-- Which period a ratio was computed on: four summed quarters or one annual
-- figure. Null for momentum, which has no period, and for a ratio built only from
-- balance items, which has no basis to name (plan amendment A1) -- `period_end`
-- still records its date. One foreign key cannot hold the up-to-sixteen facts a
-- ratio rests on, so traceability is by reproduction and this column is what
-- lets a reader see the period without re-deriving it (spec D14).
alter table metric_daily
    add column period_basis text check (period_basis in ('TTM', 'A'));
