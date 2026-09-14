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
