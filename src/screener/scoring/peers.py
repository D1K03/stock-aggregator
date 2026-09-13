"""Which peer group, and which industry, a security is scored under.

Every `security_sector` row points at a level-2 industry node; the peer groups
scored are the eleven level-1 sectors, so reaching one means following
`sector_node.parent_id`. The industry itself is returned too, because which
ratios apply to a security is decided by its industry (ratios spec D4) while its
percentiles stay at sector level.

**No floor is applied here.** A sector's membership says nothing about how many
of its members produced a given ratio, so `MIN_PEERS` is checked per metric in
`ranking.py` (ratios spec D12). This module only says where a security belongs.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import psycopg

# Checked per (metric, peer group) bucket in `ranking.py`. At sector level on
# momentum every sector clears it; ratios are what make it bite.
MIN_PEERS = 20


@dataclass(frozen=True)
class Peer:
    peer_group_id: int
    level: int
    # The level-2 industry code, for ratio applicability. None for an
    # unclassified security, or one classified straight at a sector.
    industry: str | None


def market_group(conn: psycopg.Connection) -> int:
    """The level-0 group every fallback ranks under."""
    with conn.cursor() as cur:
        cur.execute("select id from peer_group where level = 0 order by id limit 1")
        row = cur.fetchone()
    if row is None:
        # Created by the universe load, so its absence means the universe was
        # never loaded -- worth saying plainly rather than failing later on a
        # null foreign key.
        raise RuntimeError(
            "no level-0 peer group; run `python -m screener.universe load` first"
        )
    return row[0]


def resolve(
    conn: psycopg.Connection, security_ids: Sequence[int], *, as_of: date
) -> dict[int, Peer]:
    """`security_id -> Peer`. Every id asked about gets an answer."""
    if not security_ids:
        return {}

    ids = list(security_ids)
    market_id = market_group(conn)
    with conn.cursor() as cur:
        cur.execute(
            """select s.id, pg.id, pg.level,
                      case when industry.level = 2 then industry.code end
                 from security s
                 left join security_sector ss
                   on ss.security_id = s.id
                  and ss.valid_from <= %(as_of)s
                  and (ss.valid_to is null or ss.valid_to > %(as_of)s)
                 left join sector_node industry on industry.id = ss.sector_node_id
                 -- `coalesce`, because a security classified straight at a
                 -- level-1 node has no parent to walk up to and is already
                 -- where it belongs.
                 left join sector_node sector
                   on sector.id = coalesce(industry.parent_id, industry.id)
                 left join peer_group pg
                   on pg.sector_node_id = sector.id and pg.level = 1
                where s.id = any(%(ids)s)""",
            {"as_of": as_of, "ids": ids},
        )
        assigned = {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}

    out: dict[int, Peer] = {}
    for security_id in ids:
        group, level, industry = assigned.get(security_id, (None, None, None))
        if group is None or level is None:
            out[security_id] = Peer(market_id, 0, industry)
        else:
            out[security_id] = Peer(group, level, industry)
    return out
