import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from screener.edgar import EdgarConfig, SourceError, Throttled
from screener.edgar import ingest as ing
from screener.edgar import source as edgar
from screener.edgar.store import (
    content_hash,
    save,
    source_id,
    universe_ciks,
    walked,
)

HOST = "https://sec.test/Archives/edgar"
DAY = date(2026, 9, 11)
UA = "stock-aggregator/0.1 (a@example.com)"

# The real preamble, wrapped header and rule included, because parsing has to
# survive them and a tidied-up fixture would prove nothing. Copied from
# form.20260911.idx.
PREAMBLE = """\
Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Sep 11, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/




Form Type   Company Name                                                  CIK
      Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
"""


def row(form, name, cik, accession, filed="20260911"):
    """One index line at the real column offsets (0/17/79/91/103)."""
    return (
        f"{form:<17}{name:<62}{cik:<12}{filed:<12}"
        f"edgar/data/{cik}/{accession}.txt"
    )


def index(*rows):
    return PREAMBLE + "\n".join(rows) + "\n"


def owner_xml(
    cik="0000001111",
    name="Doe Jane",
    director="0",
    officer="1",
    ten="0",
    other="0",
    title="CFO",
):
    return f"""
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>{cik}</rptOwnerCik>
      <rptOwnerName>{name}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{director}</isDirector>
      <isOfficer>{officer}</isOfficer>
      <isTenPercentOwner>{ten}</isTenPercentOwner>
      <isOther>{other}</isOther>
      <officerTitle>{title}</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>"""


def txn_xml(when="2026-09-09", code="S", shares="100", price="10.25", ad="D"):
    return f"""
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{when}</value></transactionDate>
      <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>900</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>"""


DERIVATIVE = """
    <derivativeTransaction>
      <securityTitle><value>Stock Option</value></securityTitle>
      <conversionOrExercisePrice><value>5.5</value></conversionOrExercisePrice>
      <transactionDate><value>2026-09-09</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>50</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <expirationDate><value>2030-01-01</value></expirationDate>
      <underlyingSecurity>
        <underlyingSecurityTitle><value>Common Stock</value></underlyingSecurityTitle>
        <underlyingSecurityShares><value>50</value></underlyingSecurityShares>
      </underlyingSecurity>
    </derivativeTransaction>"""

HOLDING = """
    <nonDerivativeHolding>
      <securityTitle><value>Common Stock</value></securityTitle>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>4242</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeHolding>"""


def submission(
    *,
    cik="0000000123",
    symbol="AAA",
    name="Alpha Inc.",
    period="2026-09-09",
    owners=None,
    transactions=None,
    derivatives="",
    holdings="",
):
    """A complete submission text file, with the XML where EDGAR puts it.

    The `<SEC-DOCUMENT>` wrapper is real: the XML is a block inside the
    submission, not the whole body, which is what `_xml_block` has to find.
    """
    owner_blocks = owners if owners is not None else owner_xml()
    txns = transactions if transactions is not None else txn_xml()
    return f"""<SEC-DOCUMENT>0000000123-26-000001.txt : 20260911
<SEC-HEADER>ACCESSION NUMBER: 0000000123-26-000001</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<XML>
<ownershipDocument>
  <periodOfReport>{period}</periodOfReport>
  <issuer>
    <issuerCik>{cik}</issuerCik>
    <issuerName>{name}</issuerName>
    <issuerTradingSymbol>{symbol}</issuerTradingSymbol>
  </issuer>{owner_blocks}
  <nonDerivativeTable>{txns}{holdings}
  </nonDerivativeTable>
  <derivativeTable>{derivatives}
  </derivativeTable>
</ownershipDocument>
</XML>
</DOCUMENT>
</SEC-DOCUMENT>
"""


def served(routes, *, listing=("20260911",)):
    """A transport answering by URL path, recording every request.

    `routes` maps an accession number to its submission body. Anything not
    routed 404s, so a test that asks for a URL nobody planned for fails loudly
    rather than getting a plausible empty answer.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("index.json"):
            return httpx.Response(
                200,
                json={
                    "directory": {
                        "item": [{"name": f"form.{d}.idx"} for d in listing]
                    }
                },
            )
        if path.endswith(".idx"):
            return httpx.Response(200, text=routes["__index__"])
        for accession, body in routes.items():
            if accession != "__index__" and path.endswith(f"{accession}.txt"):
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="no such thing")

    return httpx.MockTransport(handler), seen


def walk(routes, *, ciks=("0000000123",), listing=("20260911",), slept=None):
    transport, seen = served(routes, listing=listing)
    got = list(
        edgar.transactions(
            DAY,
            ciks=ciks,
            host=HOST,
            user_agent=UA,
            delay=0.0,
            sleep=(slept.append if slept is not None else lambda _s: None),
            transport=transport,
        )
    )
    return got, seen


# -- config -----------------------------------------------------------------


def test_an_unset_contact_address_is_how_the_ingest_is_switched_off(monkeypatch):
    monkeypatch.delenv("EDGAR_CONTACT_EMAIL", raising=False)
    assert EdgarConfig.from_env().enabled is False
    assert EdgarConfig(contact_email="a@example.com").enabled is True


def test_a_github_address_counts_as_unusable_because_sec_refuses_it(monkeypatch):
    # Measured in `screener.universe.sources.sec`: SEC answers 403 to an address
    # at that domain as firmly as to a User-Agent naming nobody, and the 403
    # reads as a network fault. Refusing it here is cheaper than debugging it.
    monkeypatch.setenv("EDGAR_CONTACT_EMAIL", "someone@github.com")
    assert EdgarConfig.from_env().enabled is False


def test_the_universe_refresh_address_does_not_switch_this_on(monkeypatch):
    # Two variables on purpose: setting a contact address for a command run four
    # times a year should not start a daily crawler against the same service.
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "a@example.com")
    monkeypatch.delenv("EDGAR_CONTACT_EMAIL", raising=False)
    assert EdgarConfig.from_env().enabled is False


def test_the_user_agent_carries_the_address_sec_requires(monkeypatch):
    monkeypatch.setenv("EDGAR_CONTACT_EMAIL", "a@example.com")
    assert EdgarConfig.from_env().user_agent == "stock-aggregator/0.1 (a@example.com)"


# -- the index --------------------------------------------------------------


def test_the_preamble_and_the_wrapped_header_are_not_read_as_filings():
    rows = list(edgar._index_rows(index(row("4", "Alpha Inc.", "123", "0000000123-26-000001"))))
    assert [r[0] for r in rows] == ["4"]


def test_a_form_type_with_a_space_and_a_name_with_commas_still_parse():
    # The two shapes that break both fixed-width slicing and whitespace
    # splitting, and both are real: `SCHEDULE 13D` puts a space in the form
    # type, and this company name puts commas and spaces in the name and runs
    # past the column the header implies.
    long_name = "FIRST REAL ESTATE INVESTMENT TRUST OF NEW JERSEY, INC."
    rows = list(
        edgar._index_rows(
            index(
                row("SCHEDULE 13D", "American Homes 4 Rent", "1562401", "0001193125-26-389400"),
                row("10-Q", long_name, "36840", "0001174947-26-000858"),
            )
        )
    )
    assert [r[0] for r in rows] == ["SCHEDULE 13D", "10-Q"]
    assert rows[1][1] == long_name
    assert rows[1][2] == "36840"


def test_a_filing_is_fetched_once_however_many_filers_it_lists():
    # EDGAR writes one index line per *filer*, and a Form 4 names at least two.
    # On 2026-09-11 one accession was listed eleven times. Fetching per row
    # would fetch that filing eleven times.
    accession = "0000000123-26-000001"
    rows = [row("4", "Alpha Inc.", "123", accession)] + [
        row("4", f"Holder {n}", f"90{n}", accession) for n in range(10)
    ]
    got, seen = walk({"__index__": index(*rows), accession: submission()})
    submissions = [r for r in seen if r.url.path.endswith(".txt")]
    assert len(submissions) == 1
    assert len(got) == 1


def test_an_issuer_outside_the_universe_is_never_fetched():
    # The architectural assertion: the CIK set filters the *index*, so a filing
    # we do not hold costs nothing. 435 filings a day, 174 of them ours.
    ours = "0000000123-26-000001"
    theirs = "0000000999-26-000002"
    routes = {
        "__index__": index(
            row("4", "Alpha Inc.", "123", ours),
            row("4", "Not Ours Plc", "999", theirs),
        ),
        ours: submission(),
        theirs: submission(cik="0000000999", symbol="ZZZ"),
    }
    got, seen = walk(routes, ciks=("0000000123",))
    fetched = [r.url.path for r in seen if r.url.path.endswith(".txt")]
    assert len(fetched) == 1
    assert ours in fetched[0]
    assert all(t.issuer_cik == "0000000123" for t in got)


def test_the_walk_asks_for_the_submission_the_index_named_rather_than_guessing():
    # **The correction this module exists around.** `.../{accession}/form4.xml`
    # is what one filer agent in seven happens to call its XML -- measured, it
    # 404s for accession 0001104659-26-107065 while the index's own `.txt`
    # serves. Without this test the next reader re-derives `form4.xml` from a
    # single probe and loses six filings in seven, silently.
    accession = "0000000123-26-000001"
    _got, seen = walk(
        {"__index__": index(row("4", "Alpha Inc.", "123", accession)), accession: submission()}
    )
    paths = [r.url.path for r in seen]
    assert not any(p.endswith("form4.xml") for p in paths)
    assert any(p.endswith(f"{accession}.txt") for p in paths)


def test_a_form_type_that_is_not_a_form_4_is_ignored():
    accession = "0000000123-26-000001"
    got, seen = walk(
        {"__index__": index(row("8-K", "Alpha Inc.", "123", accession)), accession: submission()}
    )
    assert got == []
    assert not [r for r in seen if r.url.path.endswith(".txt")]


def test_an_amendment_is_walked_and_keeps_its_document_type():
    accession = "0000000123-26-000009"
    got, _ = walk(
        {"__index__": index(row("4/A", "Alpha Inc.", "123", accession)), accession: submission()}
    )
    assert [t.document_type for t in got] == ["4/A"]


# -- which days exist -------------------------------------------------------


def test_the_published_days_come_from_the_quarter_listing():
    # A missing daily index is a 403 from S3 and a blocked User-Agent is a 403
    # from SEC, and `screener.fetch` reduces both to the same string. The
    # listing is what makes them distinguishable: a day it does not name is
    # never requested, so a 403 always means SEC refused us.
    transport, _seen = served({"__index__": index()}, listing=("20260911", "20260914"))
    got = edgar.days(2026, 3, host=HOST, user_agent=UA, transport=transport)
    assert got == (date(2026, 9, 11), date(2026, 9, 14))


def test_a_quarter_listing_that_is_not_json_is_a_source_error():
    def handler(_request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(SourceError):
        edgar.days(
            2026, 3, host=HOST, user_agent=UA, transport=httpx.MockTransport(handler)
        )


def test_quarters_covers_every_quarter_a_window_touches():
    assert edgar.quarters(date(2026, 2, 1), date(2026, 9, 11)) == (
        (2026, 1), (2026, 2), (2026, 3),
    )
    assert edgar.quarters(date(2025, 12, 30), date(2026, 1, 2)) == ((2025, 4), (2026, 1))
    assert edgar.quarters(date(2026, 9, 11), date(2026, 9, 1)) == ()


# -- the filing -------------------------------------------------------------


def test_a_relationship_flag_reads_the_same_whether_sec_wrote_1_or_true():
    # Both spellings are real and both were in one day's filings: AFLAC's
    # carries <isDirector>0</isDirector> and Apple's <isOfficer>true</isOfficer>.
    # Reading one spelling would make every officer at half the filers a
    # non-officer, which is the kind of wrong that looks like a finding.
    accession = "0000000123-26-000001"
    for spelling, expected in (("1", True), ("true", True), ("0", False), ("false", False)):
        got, _ = walk(
            {
                "__index__": index(row("4", "Alpha Inc.", "123", accession)),
                accession: submission(owners=owner_xml(officer=spelling)),
            }
        )
        assert got[0].owners[0].is_officer is expected


def test_a_joint_filing_keeps_every_owner_on_one_transaction():
    # **The bug this table shape exists to avoid.** On 2026-09-11 accession
    # 0000902664-26-003792 listed ten reporting owners against Amalgamated
    # Financial and carried exactly ONE transaction between them. A row per
    # owner would turn one 79,649-share trade into ten, and anything summing
    # shares would read ten times the volume with every figure still plausible.
    accession = "0000000123-26-000001"
    owners = "".join(
        owner_xml(cik=f"000000{n:04d}", name=f"Holder {n}", officer="0", ten="1", title="")
        for n in range(10)
    )
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: submission(owners=owners),
        }
    )
    assert len(got) == 1
    assert len(got[0].owners) == 10
    assert got[0].shares == Decimal("100")


def test_a_derivative_transaction_carries_its_underlying_and_the_other_does_not():
    accession = "0000000123-26-000001"
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: submission(derivatives=DERIVATIVE),
        }
    )
    kinds = {t.table_kind: t for t in got}
    assert set(kinds) == {"non_derivative", "derivative"}
    assert kinds["derivative"].underlying_shares == Decimal("50")
    assert kinds["derivative"].expiration_date == date(2030, 1, 1)
    assert kinds["derivative"].conversion_or_exercise_price == Decimal("5.5")
    assert kinds["non_derivative"].underlying_shares is None
    assert kinds["non_derivative"].expiration_date is None


def test_a_holding_is_not_a_transaction():
    # A holding reports a balance, not an event: no date, no code, nobody did
    # anything. Storing one would put a non-event in a window.
    accession = "0000000123-26-000001"
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: submission(holdings=HOLDING),
        }
    )
    assert len(got) == 1
    assert got[0].shares == Decimal("100")


def test_a_transaction_with_no_date_or_no_code_is_dropped():
    accession = "0000000123-26-000001"
    for broken in (txn_xml(when=""), txn_xml(code="")):
        got, _ = walk(
            {
                "__index__": index(row("4", "Alpha Inc.", "123", accession)),
                accession: submission(transactions=broken),
            }
        )
        assert got == []


def test_two_identical_transactions_in_one_document_are_numbered_apart():
    # The parser half of the key. Two grants on the same day at the same price
    # under different plans are two real transactions and the document lists
    # them twice; if this numbered them alike the upsert would merge them into
    # one row downstream and the loss would be invisible. Asserted here as well
    # as over the table, because the storage test builds its rows by hand and
    # would not notice the parser collapsing them.
    accession = "0000000123-26-000001"
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: submission(transactions=txn_xml() + txn_xml()),
        }
    )
    assert [t.seq for t in got] == [1, 2]
    assert got[0].shares == got[1].shares


def test_the_two_tables_are_numbered_independently():
    # `table_kind` is part of the key, so a derivative row and a non-derivative
    # row may both be seq 1 without colliding.
    accession = "0000000123-26-000001"
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: submission(derivatives=DERIVATIVE),
        }
    )
    assert sorted((t.table_kind, t.seq) for t in got) == [
        ("derivative", 1), ("non_derivative", 1),
    ]


def test_a_dropped_transaction_does_not_leave_a_hole_in_the_numbering():
    # The sequence counts what is kept, so it stays dense and a re-walk of the
    # same immutable document reproduces it exactly.
    accession = "0000000123-26-000001"
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: submission(
                transactions=txn_xml(shares="1") + txn_xml(when="") + txn_xml(shares="3")
            ),
        }
    )
    assert [(t.seq, t.shares) for t in got] == [(1, Decimal("1")), (2, Decimal("3"))]


def test_a_price_keeps_every_digit_it_was_filed_with():
    # 47.9122 was a real filed price. Through a float it is not the same string
    # on the way back out, and `content_hash` re-reads these as decimal text.
    accession = "0000000123-26-000001"
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: submission(transactions=txn_xml(price="47.9122")),
        }
    )
    assert got[0].price_per_share == Decimal("47.9122")
    assert str(got[0].price_per_share) == "47.9122"


def test_a_submission_with_no_xml_block_is_skipped_rather_than_ending_the_day():
    good = "0000000123-26-000001"
    bad = "0000000123-26-000002"
    got, _ = walk(
        {
            "__index__": index(
                row("4", "Alpha Inc.", "123", bad),
                row("4", "Alpha Inc.", "123", good),
            ),
            bad: "<SEC-DOCUMENT>a paper filing from 1996</SEC-DOCUMENT>",
            good: submission(),
        }
    )
    assert len(got) == 1


def test_a_submission_holding_unparseable_xml_is_skipped():
    accession = "0000000123-26-000001"
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: "<XML><ownershipDocument><unclosed></XML>",
        }
    )
    assert got == []


def test_an_empty_officer_title_is_absent_rather_than_blank():
    # SEC writes <officerTitle/> for a filer who is not an officer. Storing ''
    # as though it were a job title would put it in a group-by.
    accession = "0000000123-26-000001"
    got, _ = walk(
        {
            "__index__": index(row("4", "Alpha Inc.", "123", accession)),
            accession: submission(owners=owner_xml(title="")),
        }
    )
    assert got[0].owners[0].officer_title is None


def test_a_cik_is_ten_digits_whichever_way_it_arrived():
    # The index publishes it unpadded and the XML padded; the join against
    # `security.cik` is a string comparison, so they have to agree.
    assert edgar._pad("4977") == "0000004977"
    assert edgar._pad("0000004977") == "0000004977"


# -- refusals ---------------------------------------------------------------


def test_a_refusal_from_sec_ends_the_walk_rather_than_asking_again():
    # **The opposite remedy to Arctic Shift's 422.** There a refusal means the
    # query was too big and halving it is the fix. Here it means SEC has blocked
    # this address for about ten minutes, and every further request extends
    # that -- so the only useful response is to stop.
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith(".idx"):
            return httpx.Response(
                200, text=index(row("4", "Alpha Inc.", "123", "0000000123-26-000001"))
            )
        return httpx.Response(403, text="Undeclared Automated Tool")

    with pytest.raises(Throttled):
        list(
            edgar.transactions(
                DAY, ciks=("0000000123",), host=HOST, user_agent=UA,
                delay=0.0, sleep=lambda _s: None,
                transport=httpx.MockTransport(handler),
            )
        )
    # One index, one refused submission, and then nothing.
    assert len(calls) == 2


def test_the_walk_pauses_between_filings_so_ten_a_second_is_never_reached():
    slept: list[float] = []
    rows = [
        row("4", "Alpha Inc.", "123", f"0000000123-26-00000{n}") for n in range(1, 4)
    ]
    routes = {"__index__": index(*rows)}
    for n in range(1, 4):
        routes[f"0000000123-26-00000{n}"] = submission()
    walk(routes, slept=slept)
    assert len(slept) == 3


def test_an_empty_universe_fetches_nothing_and_says_so(caplog):
    # A fresh database before `universe load` is a real state, and filtering
    # against an empty set would look exactly like a healthy pass.
    got, seen = walk({"__index__": index()}, ciks=())
    assert got == []
    assert seen == []


# -- storage ----------------------------------------------------------------


def a_security(conn, cik="0000000123", symbol="AAA", name="Alpha Inc."):
    return int(
        conn.execute(
            """
            insert into security (name, mic, currency, country, cik, primary_symbol,
                                  first_seen)
            values (%s, 'XNAS', 'USD', 'US', %s, %s, date '2020-01-01')
            returning id
            """,
            [name, cik, symbol],
        ).fetchone()[0]
    )


def a_transaction(seq=1, *, code="S", shares="100", price="10.25", accession="0000000123-26-000001"):
    return edgar.Transaction(
        accession_number=accession,
        document_type="4",
        table_kind="non_derivative",
        seq=seq,
        issuer_cik="0000000123",
        issuer_name="Alpha Inc.",
        issuer_symbol="AAA",
        period_of_report=date(2026, 9, 9),
        filed_date=DAY,
        owners=(
            edgar.Owner(
                cik="0000001111", name="Doe Jane", is_director=False, is_officer=True,
                is_ten_percent_owner=False, is_other=False, officer_title="CFO",
            ),
        ),
        security_title="Common Stock",
        transaction_date=date(2026, 9, 9),
        transaction_code=code,
        acquired_disposed="D",
        shares=Decimal(shares),
        price_per_share=Decimal(price),
        shares_owned_after=Decimal("900"),
        direct_or_indirect="D",
    )


def test_the_migration_seeded_the_source(fresh_db):
    assert source_id(fresh_db) > 0


def test_the_universe_cik_map_pads_and_prefers_the_active_security(fresh_db):
    a_security(fresh_db, cik="123", symbol="AAA")
    got = universe_ciks(fresh_db)
    assert "0000000123" in got


def test_a_retired_security_is_still_in_the_universe_map(fresh_db):
    # `is_active` is about index membership. A 2023 filing by a company that
    # left the index in 2025 is still a fact about a security we hold.
    security_id = a_security(fresh_db, cik="777", symbol="OLD")
    fresh_db.execute("update security set is_active = false where id = %s", [security_id])
    assert universe_ciks(fresh_db).get("0000000777") == security_id


def test_a_transaction_round_trips_and_re_walking_the_day_writes_nothing(fresh_db):
    # The archive is immutable, so a second walk of the same day is the
    # cheapest possible no-op. That is a stronger guarantee than reddit has --
    # there a comment can genuinely be edited.
    security_id = a_security(fresh_db)
    source = source_id(fresh_db)
    securities = {"0000000123": security_id}
    assert save(fresh_db, source, securities, [a_transaction()]) == (1, 0)
    assert save(fresh_db, source, securities, [a_transaction()]) == (0, 0)
    stamp = fresh_db.execute("select max(fetched_at) from insider_transaction").fetchone()[0]
    save(fresh_db, source, securities, [a_transaction()])
    assert (
        fresh_db.execute("select max(fetched_at) from insider_transaction").fetchone()[0]
        == stamp
    )


def test_two_identical_transactions_in_one_filing_are_two_rows(fresh_db):
    # **The single assertion this table's key exists for.** Two grants on the
    # same day at the same price under different plans are two real
    # transactions. A key over the contents -- date, code, shares, price --
    # looks equivalent and silently merges them into one row that looks right.
    security_id = a_security(fresh_db)
    source = source_id(fresh_db)
    securities = {"0000000123": security_id}
    both = [a_transaction(seq=1), a_transaction(seq=2)]
    assert save(fresh_db, source, securities, both) == (2, 0)
    assert (
        fresh_db.execute("select count(*) from insider_transaction").fetchone()[0] == 2
    )


def test_the_hash_ignores_which_security_row_the_cik_resolved_to(fresh_db):
    # `security_id` is our resolution of a CIK, not something SEC restated. A
    # `universe load` that re-pointed a CIK must not read as a rewritten trade.
    first = a_security(fresh_db)
    second = a_security(fresh_db, cik="0000000456", symbol="BBB", name="Beta Inc.")
    source = source_id(fresh_db)
    save(fresh_db, source, {"0000000123": first}, [a_transaction()])
    # Same filing, resolved to a different security row: the hash is unchanged,
    # so the upsert's `where` excludes it and nothing is written.
    assert save(fresh_db, source, {"0000000123": second}, [a_transaction()]) == (0, 0)
    assert (
        fresh_db.execute("select security_id from insider_transaction").fetchone()[0]
        == first
    )


def test_the_hash_moves_when_a_filed_figure_does():
    assert content_hash(a_transaction()) != content_hash(a_transaction(shares="101"))
    assert content_hash(a_transaction()) != content_hash(a_transaction(code="P"))
    assert content_hash(a_transaction()) == content_hash(a_transaction())


def test_the_owners_are_stored_as_arrays_on_the_transaction(fresh_db):
    security_id = a_security(fresh_db)
    source = source_id(fresh_db)
    save(fresh_db, source, {"0000000123": security_id}, [a_transaction()])
    names, titles, officer = fresh_db.execute(
        "select owner_names, officer_titles, is_officer from insider_transaction"
    ).fetchone()
    assert names == ["Doe Jane"]
    assert titles == ["CFO"]
    assert officer is True


def test_a_filing_for_an_issuer_outside_the_universe_is_dropped_not_crashed(fresh_db):
    # Unreachable by construction -- the index filter decides what is fetched --
    # so this guards the invariant rather than an expected path. The column is
    # `not null`; dropping the row beats failing the day.
    source = source_id(fresh_db)
    assert save(fresh_db, source, {}, [a_transaction()]) == (0, 0)


# -- the frontier -----------------------------------------------------------


def a_run(conn, source, day, status):
    conn.execute(
        """
        insert into ingest_run (source_id, endpoint, started_at, status)
        values (%s, %s, now(), %s)
        """,
        [source, f"form4/{day.isoformat()}", status],
    )


def test_a_day_that_finished_ok_is_never_walked_again(fresh_db):
    source = source_id(fresh_db)
    a_run(fresh_db, source, DAY, "ok")
    assert DAY in walked(fresh_db, source)


def test_a_day_with_no_filings_from_our_universe_still_counts_as_walked(fresh_db):
    # **Why `ingest_run` is the frontier and `max(filed_date)` cannot be.** A
    # day nobody we hold filed on produces zero rows, so "walked it, found
    # nothing" and "never walked it" would be the same observation and the day
    # would be re-walked on every pass for ever.
    source = source_id(fresh_db)
    a_run(fresh_db, source, DAY, "ok")
    assert fresh_db.execute("select count(*) from insider_transaction").fetchone()[0] == 0
    assert DAY in walked(fresh_db, source)


def test_a_day_that_failed_is_offered_again_until_it_has_been_tried_enough(fresh_db):
    source = source_id(fresh_db)
    for n in range(3):
        a_run(fresh_db, source, DAY, "failed")
        assert (DAY in walked(fresh_db, source)) is False, f"gave up after {n + 1} tries"
    a_run(fresh_db, source, DAY, "failed")
    # Four attempts is enough: a day EDGAR will not serve must stop consuming
    # the whole per-pass budget on every pass.
    assert DAY in walked(fresh_db, source)


def test_a_running_day_is_not_treated_as_done(fresh_db):
    source = source_id(fresh_db)
    a_run(fresh_db, source, DAY, "running")
    assert DAY not in walked(fresh_db, source)


# -- a pass -----------------------------------------------------------------


@pytest.fixture
def wired(fresh_db, db_url, monkeypatch):
    """A migrated database with one security, and `ingest` pointed at it."""
    a_security(fresh_db)
    monkeypatch.setattr(ing, "settings", lambda: SimpleNamespace(database_url=db_url))
    monkeypatch.setattr(
        "screener.audit.writer.settings", lambda: SimpleNamespace(database_url=db_url)
    )
    return fresh_db


def a_pass(routes, *, listing=("20260911",), days_per_pass=5, now=None):
    transport, seen = served(routes, listing=listing)
    config = EdgarConfig(
        host=HOST, contact_email="a@example.com", backfill_days=30,
        days_per_pass=days_per_pass, delay=0.0,
    )
    reports = ing.once(
        config,
        transport=transport,
        sleep=lambda _s: None,
        now=now or datetime(2026, 9, 15, tzinfo=UTC),
    )
    return reports, seen


def test_a_pass_walks_a_published_day_and_stores_what_it_finds(wired):
    accession = "0000000123-26-000001"
    reports, _ = a_pass(
        {"__index__": index(row("4", "Alpha Inc.", "123", accession)), accession: submission()}
    )
    assert [r.day for r in reports] == [DAY]
    assert reports[0].stored == 1
    assert reports[0].filings == 1
    assert wired.execute("select count(*) from insider_transaction").fetchone()[0] == 1


def test_a_second_pass_skips_the_day_it_already_walked(wired):
    accession = "0000000123-26-000001"
    routes = {
        "__index__": index(row("4", "Alpha Inc.", "123", accession)),
        accession: submission(),
    }
    a_pass(routes)
    reports, seen = a_pass(routes)
    assert reports == []
    # The quarter listing is still read -- that is how it learns there is
    # nothing new -- but no index and no submission is fetched again.
    assert not [r for r in seen if r.url.path.endswith((".idx", ".txt"))]


def test_a_pass_is_bounded_by_days_per_pass(wired):
    accession = "0000000123-26-000001"
    reports, _ = a_pass(
        {"__index__": index(row("4", "Alpha Inc.", "123", accession)), accession: submission()},
        listing=("20260908", "20260909", "20260910", "20260911"),
        days_per_pass=2,
    )
    # Newest first, so a fresh deployment has yesterday before it has last month.
    assert [r.day for r in reports] == [date(2026, 9, 11), date(2026, 9, 10)]


def test_the_pass_writes_one_audit_row_however_many_days_it_walked(wired):
    accession = "0000000123-26-000001"
    a_pass(
        {"__index__": index(row("4", "Alpha Inc.", "123", accession)), accession: submission()},
        listing=("20260910", "20260911"),
    )
    rows = wired.execute(
        "select outcome, detail from audit.event where operation = 'edgar.ingest'"
    ).fetchall()
    assert len(rows) == 1
    outcome, detail = rows[0]
    assert outcome == "ok"
    assert detail["days"] == ["2026-09-11", "2026-09-10"]


def test_a_pass_sec_refused_is_recorded_as_refused_rather_than_an_error(wired):
    # `refused` has been in the audit vocabulary since 026 and nothing used it.
    # SEC turning us away is literally a refusal, and calling it an error would
    # put a healthy pipeline in the error column.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200, json={"directory": {"item": [{"name": "form.20260911.idx"}]}}
            )
        if request.url.path.endswith(".idx"):
            return httpx.Response(
                200, text=index(row("4", "Alpha Inc.", "123", "0000000123-26-000001"))
            )
        return httpx.Response(403, text="Undeclared Automated Tool")

    config = EdgarConfig(host=HOST, contact_email="a@example.com", delay=0.0)
    ing.once(
        config,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    outcome = wired.execute(
        "select outcome from audit.event where operation = 'edgar.ingest'"
    ).fetchone()[0]
    assert outcome == "refused"


def test_a_refusal_ends_the_pass_rather_than_burning_every_day_on_it(wired):
    days_asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("index.json"):
            return httpx.Response(
                200,
                json={
                    "directory": {
                        "item": [{"name": f"form.2026091{n}.idx"} for n in (0, 1)]
                    }
                },
            )
        if path.endswith(".idx"):
            days_asked.append(path)
            return httpx.Response(
                200, text=index(row("4", "Alpha Inc.", "123", "0000000123-26-000001"))
            )
        return httpx.Response(403, text="Undeclared Automated Tool")

    config = EdgarConfig(host=HOST, contact_email="a@example.com", delay=0.0)
    ing.once(
        config,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    # One day attempted, not two: every further request extends SEC's block.
    assert len(days_asked) == 1


def test_a_pass_with_no_securities_does_nothing_and_says_so(fresh_db, db_url, monkeypatch):
    monkeypatch.setattr(ing, "settings", lambda: SimpleNamespace(database_url=db_url))
    config = EdgarConfig(host=HOST, contact_email="a@example.com", delay=0.0)
    assert ing.once(config, now=datetime(2026, 9, 15, tzinfo=UTC)) == []


def test_a_pass_without_a_contact_address_opens_no_socket():
    def never(_request):
        raise AssertionError("a disabled pass must not open a socket")

    assert ing.once(EdgarConfig(), transport=httpx.MockTransport(never)) == []
