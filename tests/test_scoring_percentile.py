"""Mid-rank percentiles within a peer group (spec D6, plan amendment A3).

Ties are handled symmetrically rather than by arbitrary ordering, and the
endpoints of a distinct group are 0 and 100 -- both properties the spec asks
for by name, which is what fixes the divisor at n - 1.
"""

from decimal import Decimal

import pytest

from screener.scoring import deciles, percentiles


def _d(*values: str) -> list[Decimal]:
    return [Decimal(v) for v in values]


def test_a_group_where_every_value_is_equal_gives_every_member_fifty():
    assert percentiles(_d("7", "7", "7", "7")) == [Decimal(50)] * 4


def test_the_minimum_and_maximum_of_a_distinct_group_are_zero_and_one_hundred():
    got = percentiles(_d("1", "2", "3"))

    assert got[0] == Decimal(0)
    assert got[2] == Decimal(100)
    assert got[1] == Decimal(50)


def test_input_order_is_preserved():
    assert percentiles(_d("3", "1", "2")) == [Decimal(100), Decimal(0), Decimal(50)]


def test_a_tied_pair_shares_the_midpoint_of_the_ranks_they_span():
    # 1, 2, 2, 3: the two 2s take the average of the second and third ranks.
    got = percentiles(_d("1", "2", "2", "3"))

    assert got == [Decimal(0), Decimal(50), Decimal(50), Decimal(100)]


def test_higher_is_better_false_inverts_the_percentile():
    # Nothing in this cycle sets the flag false. P/E arrives with fundamentals,
    # and an untested inversion discovered then is the expensive kind.
    assert percentiles(_d("1", "2", "3"), higher_is_better=False) == [
        Decimal(100),
        Decimal(50),
        Decimal(0),
    ]


def test_a_single_member_group_scores_fifty_rather_than_dividing_by_zero():
    assert percentiles(_d("42")) == [Decimal(50)]


def test_an_empty_group_gives_an_empty_list():
    assert percentiles([]) == []


def test_deciles_has_eleven_entries_bounded_by_the_minimum_and_maximum():
    got = deciles(_d("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"))

    assert len(got) == 11
    assert got[0] == Decimal(0)
    assert got[10] == Decimal(10)
    assert got[5] == Decimal(5)


def test_deciles_interpolate_between_order_statistics():
    got = deciles(_d("0", "10"))

    assert got[0] == Decimal(0)
    assert got[5] == Decimal(5)
    assert got[10] == Decimal(10)


def test_deciles_of_a_single_value_repeat_it():
    assert deciles(_d("3")) == [Decimal(3)] * 11


def test_deciles_of_nothing_is_refused():
    # `peer_group_stat.deciles` is not null with a length check; there is no
    # honest eleven-element answer for an empty group.
    with pytest.raises(ValueError):
        deciles([])
