-- Queries for the /playground console. Every one verified as the
-- read-only `playground` role against a real night: 1,504 securities,
-- 346,489 fundamental facts, 1,499 scored snapshots. All run well
-- inside the console's 200-row / 10s bounds.

-- 1. The screen.
select ss.symbol, s.name, round(sd.blended_score,1) as score,
       sd.pillar_agreement as agree, round(sd.min_coverage,2) as coverage
  from snapshot_daily sd
  join security s on s.id = sd.security_id
  join security_symbol ss on ss.security_id = sd.security_id and ss.valid_to is null
 order by sd.blended_score desc
 limit 15;

-- 2. Traceability: one score decomposed into its raw inputs.
select m.code as metric,
       round(md.raw_value * 100, 1) as raw_pct,
       round(md.percentile, 1) as percentile,
       md.peer_count, pg.code as peer_group, md.fallback_level
  from metric_daily md
  join metric m         on m.id  = md.metric_id
  join peer_group pg    on pg.id = md.peer_group_id
  join security_symbol ss on ss.security_id = md.security_id and ss.valid_to is null
 where ss.symbol = 'DELL'
 order by m.code;

-- 3. Why percentiles are sector-relative.
select pg.code as sector, count(*) as members,
       round(avg(md.raw_value) * 100, 1) as avg_12m_return_pct,
       round(min(md.raw_value) * 100, 1) as worst,
       round(max(md.raw_value) * 100, 1) as best
  from metric_daily md
  join metric m      on m.id  = md.metric_id and m.code = 'ret_12m'
  join peer_group pg on pg.id = md.peer_group_id
 group by pg.code
 order by avg_12m_return_pct desc;

-- 4. Point-in-time fundamentals read (period_type is load-bearing).
select distinct on (m.code, ff.period_end, ff.period_type)
       m.code as line_item, ff.period_end, ff.period_type,
       round(ff.value/1e9, 2) as billions,
       ff.observed_at::date as learned_on
  from fundamental_fact ff
  join metric m           on m.id = ff.metric_id
  join security_symbol ss on ss.security_id = ff.security_id and ss.valid_to is null
 where ss.symbol = 'AAPL'
   and m.code in ('revenue','net_income','gross_profit')
   and ff.period_type = 'A'
 order by m.code, ff.period_end desc, ff.period_type, ff.observed_at desc
 limit 15;

-- 5. A derived ratio -- a preview of the Quality pillar.
with latest as (
  select distinct on (ff.security_id, m.code)
         ff.security_id, m.code, ff.value
    from fundamental_fact ff
    join metric m on m.id = ff.metric_id
   where m.code in ('revenue','gross_profit') and ff.period_type = 'A'
   order by ff.security_id, m.code, ff.period_end desc, ff.observed_at desc
)
select ss.symbol,
       round(r.value/1e9, 1)  as revenue_bn,
       round(100 * g.value / nullif(r.value,0), 1) as gross_margin_pct
  from latest r
  join latest g on g.security_id = r.security_id and g.code = 'gross_profit'
  join security_symbol ss on ss.security_id = r.security_id and ss.valid_to is null
 where r.code = 'revenue' and r.value > 5e9
 order by gross_margin_pct desc nulls last
 limit 12;

-- 6. The D6 collision: annual and quarterly share a period_end.
select ss.symbol, m.code as line_item, ff.period_end,
       max(ff.value) filter (where ff.period_type='A') / 1e9 as annual_bn,
       max(ff.value) filter (where ff.period_type='Q') / 1e9 as quarter_bn
  from fundamental_fact ff
  join metric m           on m.id = ff.metric_id
  join security_symbol ss on ss.security_id = ff.security_id and ss.valid_to is null
 where ss.symbol = 'AAPL' and m.code = 'revenue'
 group by ss.symbol, m.code, ff.period_end
having count(distinct ff.period_type) > 1
 order by ff.period_end desc
 limit 8;

-- 7. Coverage per line item.
select m.code as line_item,
       count(distinct ff.security_id) as securities,
       round(100.0 * count(distinct ff.security_id) /
             (select count(*) from security_symbol where valid_to is null), 0) as pct_of_universe
  from fundamental_fact ff
  join metric m on m.id = ff.metric_id
 group by m.code
 order by securities;

-- 8. The same return, scored against different peers.
-- The same ~40% 12-month return, scored against different peers.
select pg.code as sector, ss.symbol,
       round(md.raw_value * 100, 1) as return_pct,
       round(md.percentile, 1) as percentile_in_sector
  from metric_daily md
  join metric m           on m.id = md.metric_id and m.code = 'ret_12m'
  join peer_group pg      on pg.id = md.peer_group_id
  join security_symbol ss on ss.security_id = md.security_id and ss.valid_to is null
 where md.raw_value between 0.38 and 0.42
 order by percentile_in_sector desc
 limit 14;
