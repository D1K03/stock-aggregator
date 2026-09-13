"""Security to peer group and industry, up through sector_node.parent_id (spec D7).

Every `security_sector` row points at a level-2 industry node while the peer
groups v1 scores are level 1, so reaching a security's peers means following
the parent. That is not obvious from the schema and is exactly the kind of
thing otherwise discovered mid-implementation.
"""

from datetime import date

import pytest

from screener.scoring import MIN_PEERS, market_group, resolve

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


def test_a_classified_security_gets_its_sector_group_and_industry(fresh_db, taxonomy):
    ids = _securities(fresh_db, 3, taxonomy["industry"])

    got = resolve(fresh_db, ids, as_of=AS_OF)

    assert {(p.peer_group_id, p.level, p.industry) for p in got.values()} == {
        (taxonomy["sector_group"], 1, "software")
    }


def test_resolve_assigns_the_sector_however_few_it_holds(fresh_db, taxonomy):
    # The floor belongs to ranking now, per metric (ratios spec D12): a sector of
    # 112 can hold only 8 that produced a ratio, which only ranking can see.
    ids = _securities(fresh_db, MIN_PEERS - 1, taxonomy["industry"])

    got = resolve(fresh_db, ids, as_of=AS_OF)

    assert {p.peer_group_id for p in got.values()} == {taxonomy["sector_group"]}
    assert {p.level for p in got.values()} == {1}


def test_a_security_with_no_sector_gets_the_market_group(fresh_db, taxonomy):
    ids = _securities(fresh_db, 1, None)

    got = resolve(fresh_db, ids, as_of=AS_OF)

    assert (got[ids[0]].peer_group_id, got[ids[0]].level, got[ids[0]].industry) == (
        taxonomy["market"], 0, None
    )


def test_a_classification_that_had_not_started_by_as_of_is_not_used(fresh_db, taxonomy):
    ids = _securities(fresh_db, 3, taxonomy["industry"])
    fresh_db.execute(
        "update security_sector set valid_from = '2026-06-01' where security_id = %s",
        (ids[0],),
    )

    got = resolve(fresh_db, ids, as_of=AS_OF)

    assert (got[ids[0]].level, got[ids[0]].industry) == (0, None)
    assert {got[i].level for i in ids[1:]} == {1}


def test_every_id_asked_about_gets_an_answer(fresh_db, taxonomy):
    ids = _securities(fresh_db, 3, taxonomy["industry"])

    assert set(resolve(fresh_db, ids, as_of=AS_OF)) == set(ids)


def test_asking_about_nothing_returns_nothing(fresh_db, taxonomy):
    assert resolve(fresh_db, [], as_of=AS_OF) == {}


def test_the_market_group_is_found_and_its_absence_is_explained(fresh_db, taxonomy):
    assert market_group(fresh_db) == taxonomy["market"]

    fresh_db.execute("delete from peer_group where level = 0")
    with pytest.raises(RuntimeError, match="universe load"):
        market_group(fresh_db)
