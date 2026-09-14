"""Every statement the screen runs, as literals (ui-swap spec §4).

Only bound parameters vary. Where two statements share a clause it is one
literal joined to another, which is still a `LiteralString` -- the type psycopg
requires so that SQL assembled from runtime values is a type error.

Parameters are named. Piece (c) reads some of these through `playground.select`
for Steven and widens that function to take a mapping (plan amendment P6).

Every select lists its columns in the field order of the `rows` record it is
parsed into; `rows.parse` refuses a row of the wrong width.
"""

from typing import LiteralString

# D7: live, finished well, and scored by the logic `screener.scoring.run` writes
# today, so a v1 night -- momentum only, split-distorted -- is never the screen.
_QUALIFYING_RUNS: LiteralString = """
select r.id, lower(r.as_of_range), r.started_at, r.finished_at, r.git_sha,
       encode(r.config_hash, 'hex'), w.code, r.weight_version_id,
       extract(epoch from r.cutoff_offset)::bigint, l.description, r.emits_alerts
  from scoring_run r
  join scoring_logic_version l on l.id = r.logic_version_id
  join weight_version w on w.id = r.weight_version_id
 where r.status = 'live'
   and r.outcome = 'ok'
   and l.description = %(logic)s
"""

LATEST_RUN: LiteralString = _QUALIFYING_RUNS + """
 order by lower(r.as_of_range) desc, r.id desc
 limit 1
"""

RUN_BY_ID: LiteralString = _QUALIFYING_RUNS + """
   and r.id = %(run)s::bigint
"""

# D7: a weight change starts Δ afresh, because a score under other weights is
# not the same measurement.
PREVIOUS_RUN: LiteralString = """
select r.id, lower(r.as_of_range)
  from scoring_run r
  join scoring_logic_version l on l.id = r.logic_version_id
 where r.status = 'live'
   and r.outcome = 'ok'
   and l.description = %(logic)s
   and lower(r.as_of_range) < %(as_of)s
   and r.weight_version_id = %(weight)s
 order by lower(r.as_of_range) desc, r.id desc
 limit 1
"""

# The level-1 sector a security held on the night, walked up from its industry
# node as `screener.scoring.peers.resolve` does, in the scheme of the level-0
# peer group. Deliberately not the metric's `peer_group_id`, which can be the
# market (D9). Joined onto a security aliased `sec`.
SECTOR_AT_AS_OF: LiteralString = """
  left join security_sector ss
    on ss.security_id = sec.id
   and ss.valid_from <= %(as_of)s
   and (ss.valid_to is null or ss.valid_to > %(as_of)s)
  left join sector_node industry
    on industry.id = ss.sector_node_id
   and industry.scheme_id = (select scheme_id from peer_group where level = 0 order by id limit 1)
  -- `coalesce`, because a security classified straight at a level-1 node has no
  -- parent to walk up to and is already where it belongs.
  left join sector_node sector
    on sector.id = coalesce(industry.parent_id, industry.id)
"""

_RUN_ROWS: LiteralString = """
with run_rows as (
    select snap.security_id,
           sec.primary_symbol as symbol,
           sec.name,
           coalesce(sector.code, 'unclassified') as sector_code,
           coalesce(sector.name, 'Unclassified') as sector_name,
           snap.blended_score as score,
           prev.blended_score as previous_score,
           v.score as v_score, v.coverage as v_coverage,
           q.score as q_score, q.coverage as q_coverage,
           m.score as m_score, m.coverage as m_coverage,
           snap.pillar_agreement,
           snap.min_coverage
      from snapshot_daily snap
      join security sec on sec.id = snap.security_id
""" + SECTOR_AT_AS_OF + """
      left join snapshot_daily prev
        on prev.scoring_run_id = %(previous_run)s::bigint
       and prev.as_of = %(previous_as_of)s::date
       and prev.security_id = snap.security_id
      left join pillar_score_daily v
        on v.scoring_run_id = snap.scoring_run_id and v.as_of = snap.as_of
       and v.security_id = snap.security_id
       and v.pillar_id = (select id from pillar where code = 'valuation')
      left join pillar_score_daily q
        on q.scoring_run_id = snap.scoring_run_id and q.as_of = snap.as_of
       and q.security_id = snap.security_id
       and q.pillar_id = (select id from pillar where code = 'quality')
      left join pillar_score_daily m
        on m.scoring_run_id = snap.scoring_run_id and m.as_of = snap.as_of
       and m.security_id = snap.security_id
       and m.pillar_id = (select id from pillar where code = 'momentum')
     where snap.scoring_run_id = %(run)s
       and snap.as_of = %(as_of)s
)
"""

_FILTERED: LiteralString = """
, screen as (
    select *
      from run_rows
     where (%(sector)s::text is null or sector_code = %(sector)s::text)
       and (%(agree)s::int is null or pillar_agreement >= %(agree)s::int)
       and (%(partial)s::text is null
            or (%(partial)s::text = 'only' and min_coverage < 1)
            or (%(partial)s::text = 'hide' and min_coverage >= 1))
)
"""

SCREEN_COUNT: LiteralString = _RUN_ROWS + _FILTERED + """
select count(*) from screen
"""

# Every order ends with `security_id`, so pages neither overlap nor skip on ties (D8).
ORDER_BY: dict[str, LiteralString] = {
    "score": " order by score desc nulls last, security_id",
    "delta": " order by score - previous_score desc nulls last, security_id",
    "V": " order by v_score desc nulls last, security_id",
    "Q": " order by q_score desc nulls last, security_id",
    "M": " order by m_score desc nulls last, security_id",
}

_PAGE: LiteralString = """
select security_id, symbol, name, sector_code, sector_name, score, previous_score,
       v_score, v_coverage, q_score, q_coverage, m_score, m_coverage,
       pillar_agreement, min_coverage
  from screen
"""


def screen_page(sort: str) -> LiteralString:
    """One page, in one of `ORDER_BY`'s fixed orders. `sort` is validated by `params`."""
    return _RUN_ROWS + _FILTERED + _PAGE + ORDER_BY[sort] + " offset %(offset)s limit %(limit)s"


SECTORS: LiteralString = _RUN_ROWS + """
select sector_code, sector_name
  from run_rows
 group by sector_code, sector_name
 order by sector_code = 'unclassified', sector_name
"""

# Unfiltered, so the tiles describe the night rather than the current view. A
# market-ranked value counts only when the security had a sector that night: an
# unclassified security ranks against the market because that is where it
# belongs, not because its bucket was thin (D9).
TILES: LiteralString = """
select (select count(*) from snapshot_daily
         where scoring_run_id = %(run)s and as_of = %(as_of)s),
       (select count(*) from security where is_active),
       (select count(*) from snapshot_daily
         where scoring_run_id = %(run)s and as_of = %(as_of)s and min_coverage < 1),
       (select count(*) from snapshot_daily
         where scoring_run_id = %(run)s and as_of = %(as_of)s and pillar_agreement >= 3),
       (select count(*) from metric_daily md
         where md.scoring_run_id = %(run)s
           and md.as_of = %(as_of)s
           and md.fallback_level = 0
           and exists (select 1 from security_sector ss
                        where ss.security_id = md.security_id
                          and ss.valid_from <= %(as_of)s
                          and (ss.valid_to is null or ss.valid_to > %(as_of)s)))
"""

# Display closes carry no `observed_at` bound (D11). `since` only lets the read
# prune to one or two price partitions.
CLOSES: LiteralString = """
select security_id, trade_date, close, observed_at
  from (select security_id, trade_date, close, observed_at,
               row_number() over (partition by security_id order by trade_date desc) as newest
          from price_daily
         where security_id = any(%(ids)s)
           and trade_date > %(since)s
           and trade_date <= %(as_of)s) bars
 where newest <= %(count)s
 order by security_id, trade_date
"""

ACTIONS: LiteralString = """
select security_id, effective_date, action_type, ratio, amount
  from corporate_action
 where security_id = any(%(ids)s)
   and effective_date > %(since)s
   and effective_date <= %(as_of)s
 order by security_id, effective_date
"""

# D13: whether a security's inputs changed after its run started. Bars are
# checked only inside the momentum window, which is all a run reads of them.
REFRESHED: LiteralString = """
select exists (select 1 from price_daily
                where security_id = %(id)s
                  and trade_date > %(start)s
                  and trade_date <= %(as_of)s
                  and observed_at > %(started_at)s),
       exists (select 1 from fundamental_fact
                where security_id = %(id)s
                  and observed_at > %(started_at)s)
"""
