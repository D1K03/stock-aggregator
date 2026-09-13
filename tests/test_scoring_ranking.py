"""Percentiles within a peer group that has enough, else against the market.

The floor is per metric (ratios spec D12), and falling back means every security
that produced the metric -- not only the others that fell (plan amendment A3).
"""

from decimal import Decimal

from screener.scoring import MIN_PEERS, GroupStat, deciles, rank

MARKET, TECH, REALTY = 1, 11, 12
DIRECTION = {"ret_12m": True, "debt_to_equity": False}


def _place(placed, security_id, code):
    return next(p for p in placed if p.security_id == security_id and p.code == code)


def _tech(count=MIN_PEERS, code="ret_12m"):
    values = {sid: {code: Decimal(sid)} for sid in range(1, count + 1)}
    groups = {sid: (TECH, 1) for sid in values}
    return values, groups


def test_a_bucket_at_the_floor_ranks_within_its_own_group():
    values, groups = _tech()

    placed, stats = rank(values, groups, market_id=MARKET, higher_is_better=DIRECTION, min_peers=MIN_PEERS)

    top = _place(placed, 20, "ret_12m")
    assert (top.peer_group_id, top.level, top.peer_count, top.percentile) == (TECH, 1, 20, Decimal(100))
    assert stats == [GroupStat(TECH, "ret_12m", 20, deciles([Decimal(i) for i in range(1, 21)]))]


def test_a_thin_bucket_ranks_against_every_producer_not_against_itself():
    values, groups = _tech()
    for sid, value in ((101, 100), (102, 200), (103, 300)):
        values[sid] = {"ret_12m": Decimal(value)}
        groups[sid] = (REALTY, 1)

    placed, stats = rank(values, groups, market_id=MARKET, higher_is_better=DIRECTION, min_peers=MIN_PEERS)

    middle = _place(placed, 102, "ret_12m")
    # Among its own three it would be 50. Against all 23, 21 values sit below it.
    assert (middle.peer_group_id, middle.level, middle.peer_count) == (MARKET, 0, 23)
    assert middle.percentile == Decimal(21) / 22 * 100
    # The full sector is untouched by its neighbour falling back.
    assert _place(placed, 1, "ret_12m").peer_group_id == TECH
    assert {(s.peer_group_id, s.member_count) for s in stats} == {(TECH, 20), (MARKET, 23)}


def test_an_unclassified_security_ranks_against_the_market_however_many_there_are():
    values = {sid: {"ret_12m": Decimal(sid)} for sid in range(1, 26)}
    groups = {sid: (MARKET, 0) for sid in values}

    placed, _ = rank(values, groups, market_id=MARKET, higher_is_better=DIRECTION, min_peers=MIN_PEERS)

    assert {(p.peer_group_id, p.level, p.peer_count) for p in placed} == {(MARKET, 0, 25)}


def test_the_floor_is_counted_per_metric_not_per_group():
    values, groups = _tech()
    for sid in (1, 2, 3):
        values[sid]["debt_to_equity"] = Decimal(sid)

    placed, _ = rank(values, groups, market_id=MARKET, higher_is_better=DIRECTION, min_peers=MIN_PEERS)

    assert _place(placed, 1, "ret_12m").peer_group_id == TECH
    leverage = _place(placed, 1, "debt_to_equity")
    assert (leverage.peer_group_id, leverage.level, leverage.peer_count) == (MARKET, 0, 3)


def test_lower_is_better_is_honoured():
    values, groups = _tech(code="debt_to_equity")

    placed, _ = rank(values, groups, market_id=MARKET, higher_is_better=DIRECTION, min_peers=MIN_PEERS)

    assert _place(placed, 1, "debt_to_equity").percentile == Decimal(100)
    assert _place(placed, 20, "debt_to_equity").percentile == Decimal(0)
