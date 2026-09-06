"""Security to peer group, up through `sector_node.parent_id` (spec D7).

Every `security_sector` row points at a level-2 industry node while the peer
groups v1 scores are level 1, so reaching a security's peers means following
the parent. That is not obvious from the schema and is exactly the kind of
thing otherwise discovered mid-implementation.
"""

from datetime import date

import pytest

from screener.scoring import MIN_PEERS, resolve

AS_OF = date(2026, 3, 2)


@pytest.fixture
def taxonomy(fresh_db):
    """One scheme, one sector with one industry under it, and both peer groups."""
    with fresh_db.cursor() as cur:
        cur.execute(
            "insert into sector_scheme (code, name) values ('yfinance', 'yfinance')"
            " returning id"
        )
        scheme = cur.fetchone()[0]
        cur.execute(
            "insert into sector_node (scheme_id, level, code, name)"
            " values (%s, 1, 'technology', 'Technology') returning id",
            (scheme,),
        )
        sector = cur.fetchone()[0]
        cur.execute(
            "insert into sector_node (scheme_id, parent_id, level, code, name)"
            " values (%s, %s, 2, 'software', 'Software') returning id",
            (scheme, sector),
        )
        industry = cur.fetchone()[0]
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, null, 0, 'market') returning id",
            (scheme,),
        )
        market = cur.fetchone()[0]
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, %s, 1, 'technology') returning id",
            (scheme, sector),
        )
        sector_group = cur.fetchone()[0]
    return {"industry": industry, "market": market, "sector_group": sector_group}


def _securities(conn, count: int, industry_id: int | None) -> list[int]:
    ids = []
    with conn.cursor() as cur:
        for i in range(count):
            cur.execute(
                """insert into security
                   (name, mic, currency, country, primary_symbol, first_seen)
                   values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
                (f"Co {i}", f"S{i:03d}"),
            )
            security = cur.fetchone()[0]
            ids.append(security)
            if industry_id is not None:
                cur.execute(
                    """insert into security_sector
                       (security_id, sector_node_id, valid_from, source)
                       values (%s, %s, '2020-01-01', 'yfinance')""",
                    (security, industry_id),
                )
    return ids


def test_a_sector_above_the_floor_scores_against_its_sector_group(fresh_db, taxonomy):
    ids = _securities(fresh_db, MIN_PEERS, taxonomy["industry"])

    got = resolve(fresh_db, ids, as_of=AS_OF)

    assert {p.peer_group_id for p in got.values()} == {taxonomy["sector_group"]}
    assert {p.level for p in got.values()} == {1}
    assert {p.member_count for p in got.values()} == {MIN_PEERS}


def test_a_sector_below_the_floor_falls_back_to_the_market_group(fresh_db, taxonomy):
    # Unexercised in practice -- the thinnest real sector holds 49 -- and
    # implemented anyway, because the ladder is what `fallback_level` records.
    ids = _securities(fresh_db, MIN_PEERS - 1, taxonomy["industry"])

    got = resolve(fresh_db, ids, as_of=AS_OF)

    assert {p.peer_group_id for p in got.values()} == {taxonomy["market"]}
    assert {p.level for p in got.values()} == {0}


def test_a_security_with_no_sector_still_gets_an_answer(fresh_db, taxonomy):
    ids = _securities(fresh_db, 1, None)

    got = resolve(fresh_db, ids, as_of=AS_OF)

    assert got[ids[0]].peer_group_id == taxonomy["market"]
    assert got[ids[0]].level == 0


def test_a_classification_that_had_not_started_by_as_of_is_not_used(fresh_db, taxonomy):
    ids = _securities(fresh_db, MIN_PEERS, taxonomy["industry"])
    fresh_db.execute(
        "update security_sector set valid_from = '2026-06-01' where security_id = %s",
        (ids[0],),
    )

    got = resolve(fresh_db, ids, as_of=AS_OF)

    assert got[ids[0]].level == 0
    # And the sector it left is now one short of the floor, so nobody is in it.
    assert {p.level for p in got.values()} == {0}


def test_every_id_asked_about_gets_an_answer(fresh_db, taxonomy):
    ids = _securities(fresh_db, 3, taxonomy["industry"])

    assert set(resolve(fresh_db, ids, as_of=AS_OF)) == set(ids)


def test_asking_about_nothing_returns_nothing(fresh_db, taxonomy):
    assert resolve(fresh_db, [], as_of=AS_OF) == {}
