"""The screen endpoints' query strings (ui-swap spec D9, D10).

Every wrong value is a refusal that names its parameter. A quiet default would
serve a screen sorted or filtered differently from what was asked, with nothing
on the page saying so.
"""

import pytest

from screener.screen import BadParameter, ScreenParams, SecurityParams, screen_params, security_params


def test_no_parameters_is_the_first_page_of_the_latest_screen_by_score():
    assert screen_params({}) == ScreenParams(
        run=None, sector=None, agree=None, partial=None, sort="score", offset=0, limit=50
    )


def test_every_parameter_is_read():
    got = screen_params({
        "run": ["7"], "sector": ["consumer-cyclical"], "agree": ["2"], "partial": ["hide"],
        "sort": ["delta"], "offset": ["50"], "limit": ["100"],
    })

    assert got == ScreenParams(7, "consumer-cyclical", 2, "hide", "delta", 50, 100)


def test_unclassified_is_a_sector():
    assert screen_params({"sector": ["unclassified"]}).sector == "unclassified"


@pytest.mark.parametrize(
    "name,value",
    [
        ("sort", "price"),
        ("sort", "v"),
        ("agree", "0"),
        ("agree", "4"),
        ("partial", "yes"),
        ("offset", "-1"),
        ("offset", "1e3"),
        ("offset", "1000001"),
        ("limit", "0"),
        ("limit", "101"),
        ("run", "0"),
        ("run", "seven"),
        ("run", "+7"),
        ("run", "٧"),
        ("sector", "Technology"),
        ("sector", "tech; drop table security"),
    ],
)
def test_a_wrong_value_is_refused_naming_its_parameter(name, value):
    with pytest.raises(BadParameter) as caught:
        screen_params({name: [value]})

    assert caught.value.name == name
    assert name in str(caught.value)


def test_the_sort_refusal_lists_what_is_allowed():
    with pytest.raises(BadParameter, match="sort must be one of score, delta, V, Q, M"):
        screen_params({"sort": ["price"]})


def test_an_unknown_parameter_is_refused_by_name():
    with pytest.raises(BadParameter) as caught:
        screen_params({"colour": ["red"]})

    assert caught.value.name == "colour"


def test_a_parameter_given_twice_is_refused():
    with pytest.raises(BadParameter) as caught:
        screen_params({"sort": ["score", "V"]})

    assert caught.value.name == "sort"


def test_a_symbol_is_trimmed_of_whitespace_and_a_dollar_sign():
    assert security_params({"symbol": [" $jpm "], "run": ["7"]}) == SecurityParams("jpm", 7)


@pytest.mark.parametrize("query", [{}, {"symbol": ["$"]}, {"symbol": ["   "]}, {"symbol": ["A" * 17]}])
def test_a_missing_or_impossible_symbol_is_refused(query):
    with pytest.raises(BadParameter) as caught:
        security_params(query)

    assert caught.value.name == "symbol"


def test_the_security_endpoint_refuses_the_screen_s_filters():
    with pytest.raises(BadParameter) as caught:
        security_params({"symbol": ["JPM"], "sort": ["V"]})

    assert caught.value.name == "sort"
