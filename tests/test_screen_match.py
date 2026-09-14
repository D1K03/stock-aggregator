"""Which security a symbol names when more than one holds it (ui-swap spec D10, §13).

`universe load` leaves a departed security's symbol row open, so a reused ticker
matches the security that left as well as the one trading under it now. The page
and Steven have nothing but the symbol to go on, so refusing that as ambiguous
would make the active security unreachable from both.
"""

import pytest

from screener.screen import AmbiguousSymbol, SymbolRow, UnknownSymbol, choose_match


def listing(security_id: int, mic: str = "XNAS", *, active: bool = True) -> SymbolRow:
    return SymbolRow(security_id, "ABC", "ABC Inc", mic, active)


def test_no_match_is_an_unknown_symbol():
    with pytest.raises(UnknownSymbol):
        choose_match("ABC", [])


def test_a_single_match_is_chosen_whether_or_not_it_is_active():
    assert choose_match("ABC", [listing(1, active=False)]).security_id == 1


def test_the_only_active_match_is_preferred_over_a_departed_one():
    assert choose_match("ABC", [listing(1, active=False), listing(2, "XNYS")]).security_id == 2


def test_two_active_matches_are_ambiguous_and_name_the_active_exchanges():
    with pytest.raises(AmbiguousSymbol) as caught:
        choose_match(
            "ABC", [listing(1, "XNYS"), listing(2, "XNAS"), listing(3, "BATS", active=False)]
        )

    assert caught.value.exchanges == ("XNAS", "XNYS")


def test_several_departed_matches_are_ambiguous():
    with pytest.raises(AmbiguousSymbol) as caught:
        choose_match("ABC", [listing(1, "XNYS", active=False), listing(2, active=False)])

    assert caught.value.exchanges == ("XNAS", "XNYS")
