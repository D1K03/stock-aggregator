import pytest

from screener.universe.reconcile import (
    AmbiguousIdentity,
    ExistingSecurity,
    plan,
)
from screener.universe.rows import UniverseRow


def csv_row(symbol: str, *, cik: str = "", industry: str = "Consumer Electronics") -> UniverseRow:
    return UniverseRow(
        symbol=symbol, name=f"{symbol} Inc", index_name="sp500", mic="XNAS",
        currency="USD", cik=cik, yf_sector="Technology", yf_industry=industry,
        gics_sector="Information Technology",
    )


def existing(sec_id: int, symbol: str, *, cik: str = "", industry: str = "consumer-electronics",
             active: bool = True) -> ExistingSecurity:
    return ExistingSecurity(id=sec_id, cik=cik, symbol=symbol, industry_code=industry, is_active=active)


def test_a_symbol_not_in_the_database_is_new():
    result = plan([csv_row("AAPL")], [])
    assert [r.symbol for r in result.new] == ["AAPL"]


def test_an_active_security_absent_from_the_csv_has_departed():
    result = plan([], [existing(1, "AAPL")])
    assert [e.id for e in result.departed] == [1]


def test_an_unchanged_security_is_counted_not_acted_on():
    result = plan([csv_row("AAPL")], [existing(1, "AAPL")])
    assert result.unchanged == 1
    assert not (result.new or result.departed or result.reclassified or result.renamed)


def test_a_changed_industry_is_a_reclassification():
    result = plan([csv_row("AAPL", industry="Software - Infrastructure")], [existing(1, "AAPL")])
    assert [e.id for e, _ in result.reclassified] == [1]


def test_a_rename_is_matched_by_cik_not_symbol():
    """The decision the reconciler hangs on. Matching by symbol would report a
    departure plus an unrelated arrival and orphan the company's history."""
    result = plan(
        [csv_row("META", cik="0001326801")],
        [existing(1, "FB", cik="0001326801")],
    )
    assert [e.id for e, _ in result.renamed] == [1]
    assert not result.new
    assert not result.departed


def test_a_rename_without_a_cik_cannot_be_detected_and_reads_as_churn():
    """Recorded so the limitation is explicit: no CIK, no rename detection."""
    result = plan([csv_row("META")], [existing(1, "FB")])
    assert [r.symbol for r in result.new] == ["META"]
    assert [e.id for e in result.departed] == [1]


def test_an_inactive_security_present_again_has_re_entered():
    result = plan([csv_row("AAPL")], [existing(1, "AAPL", active=False)])
    assert [e.id for e, _ in result.reentered] == [1]
    assert not result.new


def test_an_inactive_security_still_absent_is_not_a_departure():
    result = plan([], [existing(1, "AAPL", active=False)])
    assert not result.departed


def test_two_csv_rows_resolving_to_one_security_is_an_error():
    with pytest.raises(AmbiguousIdentity, match="0000320193"):
        plan(
            [csv_row("AAPL", cik="0000320193"), csv_row("AAPL2", cik="0000320193")],
            [existing(1, "AAPL", cik="0000320193")],
        )


def test_departure_share_is_measured_against_the_active_universe():
    result = plan([csv_row("A")], [existing(1, "A"), existing(2, "B"), existing(3, "C"), existing(4, "D")])
    assert result.departure_share == pytest.approx(0.75)


def test_departure_share_is_zero_when_the_database_is_empty():
    assert plan([csv_row("A")], []).departure_share == 0.0
