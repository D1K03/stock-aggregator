"""Which securities a security is scored against.

Every `security_sector` row points at a level-2 industry node; the peer groups
v1 scores are the eleven level-1 sectors, so reaching one means following
`sector_node.parent_id`. `metric_daily.fallback_level` therefore mirrors
`peer_group.level`: normally 1, dropping to 0 only if a sector fell below the
floor.

The ladder is unexercised in practice -- the thinnest real sector holds 49 --
and implemented anyway, because a floor that has never been tested is a floor
nobody can rely on.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import psycopg

# A safety check rather than a routine mechanism: v1 groups at sector level,
# where every sector clears it comfortably.
MIN_PEERS = 20


@dataclass(frozen=True)
class Peer:
    peer_group_id: int
    level: int
    member_count: int


def _market_group(cur: psycopg.Cursor) -> tuple[int, int]:
    cur.execute("select id from peer_group where level = 0 order by id limit 1")
    row = cur.fetchone()
    if row is None:
        # Created by the universe load, so its absence means the universe was
        # never loaded -- worth saying plainly rather than failing later on a
        # null foreign key.
        raise RuntimeError(
            "no level-0 peer group; run `python -m screener.universe load` first"
        )
    return row[0], 0


def resolve(
    conn: psycopg.Connection, security_ids: Sequence[int], *, as_of: date
) -> dict[int, Peer]:
    """`security_id -> Peer`. Every id asked about gets an answer."""
    if not security_ids:
        return {}

    ids = list(security_ids)
    with conn.cursor() as cur:
        market_id, market_level = _market_group(cur)
        cur.execute(
            """select s.id, pg.id, pg.level
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
        assigned = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    counts = Counter(
        group for group, _ in assigned.values() if group is not None
    )
    # The floor is measured against the securities being scored, not against
    # everything the sector ever held: a percentile is only as meaningful as
    # the number of peers that actually produced a value today.
    market_count = sum(
        1
        for group, _ in assigned.values()
        if group is None or counts[group] < MIN_PEERS
    )

    out: dict[int, Peer] = {}
    for security_id in ids:
        group, level = assigned.get(security_id, (None, None))
        if group is None or level is None or counts[group] < MIN_PEERS:
            out[security_id] = Peer(market_id, market_level, market_count)
        else:
            out[security_id] = Peer(group, level, counts[group])
    return out
