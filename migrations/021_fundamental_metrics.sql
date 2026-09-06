-- The line items fundamentals ingest stores, the flag that says they are not
-- scored, and the point-in-time index corrected.
--
-- Seeded by migration rather than by a command for the reason 019 gives: a
-- `metric.code` has to agree with the code parsing it, so they change together
-- and therefore ship together.

-- `is_input` rather than reusing `is_active`. The two would look
-- interchangeable today -- nothing scores an input, so `is_active = false`
-- would read correctly -- and stop being interchangeable the first time a
-- metric is genuinely retired, at which point "which of these false rows are
-- inputs and which are dead?" has no answer left in the data. The collision is
-- certain rather than hypothetical, and the column is cheap now.
alter table metric add column is_input boolean not null default false;

-- D6. `fundamental_fact`'s unique constraint includes `period_type`; its index
-- did not, and neither did the point-in-time read documented in the schema
-- spec. A fiscal Q4 ends when its fiscal year does -- AAPL reports both
-- annual and quarterly revenue at 2025-09-30, four-fold apart -- so
-- `distinct on (security_id, metric_id, period_end)` collapses the year into
-- its own last quarter and returns whichever was inserted later. Silently, for
-- every company, every year.
--
-- Column order matches the read's `order by` exactly and has to: with
-- period_type ahead of period_end, Postgres cannot satisfy the `distinct on`
-- from this index and would sort anyway, having paid for the index on insert.
create index fundamental_fact_pit_idx2 on fundamental_fact
    (security_id, metric_id, period_end, period_type, observed_at desc);

-- The old one is dropped rather than kept. The only query it serves better
-- than the index above is "the latest observation of one metric for one period
-- end, across both period types" -- which is precisely the query D6 exists to
-- say nobody should run. Keeping it would cost an index write per fact per
-- night in order to serve a mistake.
drop index fundamental_fact_pit_idx;

-- 28 line items. `is_input = true` throughout: stored as evidence, never
-- scored, and no pillar average will ever include one.
--
-- `pillar_id` is a fiction and has to be, because the column is `not null` and
-- an input does not belong to a pillar -- `shares_diluted_avg` feeds Valuation
-- and Quality both, and `revenue` feeds Valuation through P/S and Quality
-- through every margin. Nothing reads it for an input row, and any query that
-- groups inputs by pillar will get a plausible-looking wrong answer.
--
-- `cadence` and `higher_is_better` are equally unread: period granularity
-- lives in `period_type` per fact, and nothing ranks an input. They are
-- not-null columns being filled.
insert into metric (code, name, pillar_id, unit, higher_is_better, cadence, is_input)
select v.code, v.name, p.id, v.unit, true, 'quarterly', true
  from (values
        -- income statement
        ('revenue',                  'Total revenue',              'currency', 'valuation'),
        ('cost_of_revenue',          'Cost of revenue',            'currency', 'quality'),
        ('gross_profit',             'Gross profit',               'currency', 'quality'),
        ('research_and_development', 'Research and development',   'currency', 'quality'),
        ('selling_general_admin',    'Selling, general and admin', 'currency', 'quality'),
        ('operating_income',         'Operating income',           'currency', 'quality'),
        ('ebit',                     'EBIT',                       'currency', 'valuation'),
        ('interest_expense',         'Interest expense',           'currency', 'quality'),
        ('pretax_income',            'Pretax income',              'currency', 'quality'),
        ('tax_provision',            'Tax provision',              'currency', 'quality'),
        ('net_income',               'Net income',                 'currency', 'valuation'),
        -- cash flow
        ('depreciation_amortisation', 'Depreciation and amortisation', 'currency', 'valuation'),
        ('operating_cash_flow',      'Operating cash flow',        'currency', 'quality'),
        ('capital_expenditure',      'Capital expenditure',        'currency', 'quality'),
        -- balance sheet
        ('total_assets',             'Total assets',               'currency', 'quality'),
        ('current_assets',           'Current assets',             'currency', 'quality'),
        ('current_liabilities',      'Current liabilities',        'currency', 'quality'),
        ('total_liabilities',        'Total liabilities',          'currency', 'quality'),
        ('stockholders_equity',      'Stockholders equity',        'currency', 'valuation'),
        ('cash_and_equivalents',     'Cash and equivalents',       'currency', 'quality'),
        ('cash_and_short_term_investments',
                                     'Cash and short-term investments', 'currency', 'quality'),
        ('current_debt',             'Current debt',               'currency', 'quality'),
        ('long_term_debt',           'Long-term debt',             'currency', 'quality'),
        ('total_debt',               'Total debt',                 'currency', 'quality'),
        ('net_ppe',                  'Net property, plant and equipment', 'currency', 'quality'),
        -- share counts. The first two are averages across the period; the
        -- third is the count at the period end, and the names say which
        -- because they were measured a percent apart and a ratio reaching for
        -- the wrong one is wrong by exactly the amount nobody notices.
        ('shares_basic_avg',         'Basic average shares',       'shares',   'valuation'),
        ('shares_diluted_avg',       'Diluted average shares',     'shares',   'valuation'),
        ('shares_outstanding',       'Shares outstanding',         'shares',   'valuation')
       ) as v(code, name, unit, pillar_code)
  join pillar p on p.code = v.pillar_code
on conflict (code) do nothing;
