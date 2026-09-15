"""Form 4 transactions from EDGAR. Returns rows, never writes.

Three requests deep, and each layer exists because the one above it does not
answer the question:

    index.json  -- which days were published at all
    form.D.idx  -- which filings landed that day, and where each one lives
    <acc>.txt   -- the filing itself, XML and all

**The daily index is the frontier and the submission it names is the document.**
Not `.../{accession}/form4.xml`: the filer's agent names its own XML file, and
`form4.xml`, `ownership.xml`, `wk-form4_1789156901.xml` and
`tm2624595-6_4seq1.xml` were four distinct patterns in a sample of seven on
2026-09-11. Guessing `form4.xml` 404s for six filings in seven. The index
already carries the path to the complete submission, which holds the XML
verbatim inside an `<XML>` block, so nothing has to be guessed and it is one
request rather than a directory listing plus a file.

**One index line per filer, not per filing.** A Form 4 names at least two
parties -- the issuer and the reporting owner -- and EDGAR writes a line for
each. 921 Form 4 and 4/A rows on 2026-09-11 were 435 filings, one of them listed
eleven times. Fetching per row would fetch that filing eleven times.

The happy consequence is the whole architecture here: **the issuer's CIK is
always one of those lines**, so the universe filter applies to the index, before
any filing is opened. 435 filings that day, 174 of them ours.

Rate limiting lives in this module rather than in `screener.fetch`, on D6's
terms. Unlike Arctic Shift's, SEC's limit is published (10 requests a second)
and enforced by blocking the address, so the delay here is compliance.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from screener.fetch import fetch

logger = logging.getLogger(__name__)

# Both are Form 4s. An amendment carries its own accession and is stored as an
# ordinary row: SEC treats it as a new filing rather than a rewrite, and so does
# this.
FORM_TYPES = frozenset({"4", "4/A"})

# A day's index is ~740 KB and a submission is 5-36 KB, so this is generous.
TIMEOUT = 60.0

# Refuse a body this large before parsing it. `xml.etree` is the stdlib parser
# and `defusedxml` is not a dependency of this project and is not going to
# become one for this; measured, external entities raise `ParseError: undefined
# entity` so XXE is not reachable, but internal entities *do* expand, so an
# expansion bomb would work. What actually stands against that is that these
# bytes come from sec.gov over TLS -- and this cap, so an absurd body is refused
# rather than parsed. Worth writing down rather than implying it is safe.
MAX_SUBMISSION_BYTES = 8 * 1024 * 1024

# httpx's phrasing for the two refusals, which reach this module as text:
# `screener.fetch` collapses every strategy's exception into one `FetchError`
# message, so there is no status code left to read. Anchored on the quoted
# reason rather than a bare "403", which a byte count or a CIK would satisfy.
REFUSED = "'403 Forbidden'"
RATE_LIMITED = "'429 Too Many Requests'"

# One row of a daily index, matched rather than sliced.
#
# **Fixed-width slicing is a trap here and whitespace splitting is impossible.**
# The data columns start at 0/17/79/91/103, but the header wraps onto a second
# line so its own offsets disagree with them; `SCHEDULE 13D/A` and `1-A POS`
# put spaces in the form type; and `FIRST REAL ESTATE INVESTMENT TRUST OF NEW
# JERSEY, INC.` puts spaces and a comma in the name and runs past column 62.
# Two-or-more spaces is the only separator all three respect. Measured: this
# matches 3,784 of 3,784 data rows on 2026-09-11, with every CIK column
# agreeing with the CIK embedded in its own path.
ROW = re.compile(
    r"^(?P<form>\S.*?)\s{2,}(?P<name>\S.*?)\s{2,}"
    r"(?P<cik>\d{1,10})\s+(?P<filed>\d{8})\s+(?P<path>edgar/\S+\.txt)\s*$"
)

# The filing's XML, as EDGAR wraps it inside the complete submission.
XML_BLOCK = re.compile(r"<XML>\s*(.*?)\s*</XML>", re.DOTALL)


class SourceError(RuntimeError):
    """EDGAR answered, but not with anything usable."""


class Throttled(SourceError):
    """SEC turned us away. Stop asking; do not narrow and retry.

    **The opposite remedy to Arctic Shift's 422**, which is why it is a
    different type rather than a shared one. There, a refusal means the query
    was too big and halving the window is the fix. Here it means SEC's limiter
    has blocked this address for about ten minutes, and every further request
    extends that window -- so the only useful response is to end the pass and
    let the next one start clean.
    """


@dataclass(frozen=True, slots=True)
class Owner:
    """One reporting owner on a filing, and how they are connected to it.

    The relationship is the point. A ten percent holder trimming a position and
    a chief financial officer buying on the open market are not the same event,
    and without these flags they are the same row.
    """

    cik: str
    name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str | None


@dataclass(frozen=True, slots=True)
class _Filing:
    """The header every transaction in one document shares.

    Internal, and a dataclass rather than a `**kwargs` splat because pyright
    cannot type the latter: every field would widen to `object` at the call
    site and `Transaction` would stop being checked at the one place it matters.
    """

    accession_number: str
    document_type: str
    issuer_cik: str
    issuer_name: str
    issuer_symbol: str | None
    period_of_report: date
    filed_date: date
    owners: tuple["Owner", ...]


@dataclass(frozen=True, slots=True)
class Transaction:
    """One transaction from one Form 4, with its filing's context attached.

    Flat rather than nested inside a filing object: the transaction is the unit
    a pillar reads and the unit the table stores, and a filing that carried two
    of them is two rows that happen to share an accession number.
    """

    accession_number: str
    document_type: str
    table_kind: str
    seq: int

    issuer_cik: str
    issuer_name: str
    issuer_symbol: str | None

    period_of_report: date
    filed_date: date

    owners: tuple[Owner, ...]

    security_title: str | None
    transaction_date: date
    transaction_code: str
    acquired_disposed: str | None
    shares: Decimal | None
    price_per_share: Decimal | None
    shares_owned_after: Decimal | None
    direct_or_indirect: str | None

    # Derivative rows only. Null on every non-derivative one.
    conversion_or_exercise_price: Decimal | None = None
    expiration_date: date | None = None
    underlying_title: str | None = None
    underlying_shares: Decimal | None = None


def quarters(after: date, before: date) -> tuple[tuple[int, int], ...]:
    """Every `(year, quarter)` the inclusive range `[after, before]` touches.

    EDGAR files its daily indexes under a quarter directory, so a window
    spanning a boundary needs both listings.
    """
    if before < after:
        return ()
    out: list[tuple[int, int]] = []
    year, quarter = after.year, (after.month - 1) // 3 + 1
    last = (before.year, (before.month - 1) // 3 + 1)
    while (year, quarter) <= last:
        out.append((year, quarter))
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return tuple(out)


def days(
    year: int,
    quarter: int,
    *,
    host: str,
    user_agent: str,
    transport: httpx.BaseTransport | None = None,
) -> tuple[date, ...]:
    """The days in one quarter that actually have a published index.

    **This request exists so that a missing day never has to be guessed at.**
    EDGAR answers a daily index that does not exist with a 403 from S3, and SEC
    answers a request it has refused with a 403 of its own -- and by the time
    either reaches this module `screener.fetch` has reduced it to the same
    string. A weekend and a throttle would be indistinguishable.

    The quarter listing settles it: it names exactly which `form.*.idx` exist,
    so a day we never ask for cannot be a refusal we have to interpret, and a
    403 on a day this listing *did* name is unambiguously SEC refusing us.
    Verified 2026-09-15 -- Saturday the 12th, Sunday the 13th, Labor Day and
    today are all absent from QTR3, and Monday the 14th is present.
    """
    url = f"{host}/daily-index/{year}/QTR{quarter}/index.json"
    payload = _get_json(url, user_agent=user_agent, transport=transport)
    items = (payload.get("directory") or {}).get("item") or []
    if not isinstance(items, list):
        raise SourceError("edgar returned a quarter listing that is not a list")
    out: list[date] = []
    for item in items:
        name = (item or {}).get("name") if isinstance(item, dict) else None
        match = re.fullmatch(r"form\.(\d{8})\.idx", str(name or ""))
        if match:
            parsed = _date(match.group(1), "%Y%m%d")
            if parsed is not None:
                out.append(parsed)
    return tuple(sorted(out))


def transactions(
    day: date,
    *,
    ciks: Collection[str],
    host: str,
    user_agent: str,
    delay: float = 0.15,
    sleep: Callable[[float], None] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[Transaction]:
    """Every Form 4 transaction filed on `day` by an issuer in `ciks`.

    `ciks` is a collection of zero-padded ten-digit strings, and it being an
    *argument* is the whole reason this module can keep its half of the split:
    it is data, exactly as a subreddit name is data, so nothing here opens a
    database connection. `screener.edgar.ingest` reads the set once per pass.

    Without it a day is 435 filings to keep 174. With it the filter runs against
    the index, and the rest are never opened.

    The filter matches on *any* filer rather than on the issuer, because the
    index does not say which line is which. That is deliberately loose: a
    company we hold filing as a ten percent owner of one we do not is fetched
    and then dropped by `store.save`, which is the price of deciding from the
    index instead of from 435 filings.

    `sleep` is injected so a test asserts the pauses without waiting, as
    `reddit.source` and `universe.sources.yahoo` do.
    """
    pause = sleep or time.sleep
    wanted = {_pad(c) for c in ciks}
    if not wanted:
        # An empty universe is a real state -- a fresh database before
        # `universe load` has run -- and filtering against it would silently
        # fetch nothing while looking exactly like a working pass. Say so.
        logger.warning("no universe CIKs supplied; %s cannot be filtered", day)
        return

    index = _get_text(
        f"{host}/daily-index/{day.year}/QTR{(day.month - 1) // 3 + 1}/"
        f"form.{day:%Y%m%d}.idx",
        user_agent=user_agent,
        transport=transport,
    )

    # Dedupe to one entry per filing, keeping whichever path names it. Every
    # line of a filing points at the same submission, so any of them will do.
    filings: dict[str, tuple[str, str, set[str]]] = {}
    for form, _name, cik, _filed, path in _index_rows(index):
        if form not in FORM_TYPES:
            continue
        accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
        held = filings.setdefault(accession, (form, path, set()))
        held[2].add(_pad(cik))

    for accession, (form, path, parties) in sorted(filings.items()):
        # The issuer is one of the filers on every Form 4, so an intersection
        # here is the universe filter -- decided without opening the filing.
        if not parties & wanted:
            continue
        pause(delay)
        try:
            # The index writes paths relative to the archive root
            # (`edgar/data/…`), and `host` already ends in `/edgar`, so the
            # prefix comes off rather than the suffix -- joining them naively
            # gives `…/edgar/edgar/data/…`, which is a 403 that reads as a
            # throttle.
            raw = _get_text(
                f"{host}/{path.removeprefix('edgar/')}",
                user_agent=user_agent,
                transport=transport,
            )
        except Throttled:
            raise
        except Exception as exc:
            # One unreadable filing is not a reason to lose the other 173.
            # `screener.nightly` already settles this argument for the pipeline:
            # a partial ingest is a success, and a channel that cries wolf over
            # one bad row is one nobody reads.
            logger.warning("could not fetch %s: %s", accession, exc)
            continue
        yield from _parse(accession, form, raw, day)


def _index_rows(text: str) -> Iterator[tuple[str, str, str, str, str]]:
    """`(form, name, cik, filed, path)` for each data row of a daily index.

    The preamble, the wrapped header and the rule under it do not match `ROW`
    and are skipped by not matching rather than by being counted -- EDGAR has
    changed how many blank lines it writes before now, and a fixed skip would
    have eaten a real row when it did.
    """
    for line in text.splitlines():
        match = ROW.match(line)
        if match:
            yield (
                match["form"].strip(),
                match["name"].strip(),
                match["cik"],
                match["filed"],
                match["path"],
            )


def _parse(
    accession: str, document_type: str, raw: str, filed: date
) -> Iterator[Transaction]:
    """Every transaction in one complete submission."""
    block = XML_BLOCK.search(raw)
    if block is None:
        logger.warning("%s carries no XML block; skipping", accession)
        return
    try:
        root = ET.fromstring(block.group(1))
    except ET.ParseError as exc:
        # Skipped rather than raised, for the reason a failed fetch is: a filing
        # EDGAR wrote in 2004 that this parser cannot read should cost that one
        # filing, not the day.
        logger.warning("%s holds XML that will not parse: %s", accession, exc)
        return

    issuer_cik = _clean(root.findtext("issuer/issuerCik"))
    issuer_name = _clean(root.findtext("issuer/issuerName"))
    period = _date(_clean(root.findtext("periodOfReport")), "%Y-%m-%d")
    if not issuer_cik or not issuer_name or period is None:
        logger.warning("%s names no issuer or no period; skipping", accession)
        return

    owners = _owners(root)
    if not owners:
        logger.warning("%s names no reporting owner; skipping", accession)
        return

    filing = _Filing(
        accession_number=accession,
        document_type=document_type,
        issuer_cik=_pad(issuer_cik),
        issuer_name=issuer_name,
        issuer_symbol=_clean(root.findtext("issuer/issuerTradingSymbol")),
        period_of_report=period,
        filed_date=filed,
        owners=owners,
    )
    yield from _from_table(root, "non_derivative", filing)
    yield from _from_table(root, "derivative", filing)


def _owners(root: ET.Element) -> tuple[Owner, ...]:
    """Every reporting owner on the filing, in document order.

    All of them, and that is the point. A joint filing is N owners against one
    shared transaction table -- the XML does not say which of them made a given
    trade, because they made it together -- so the alternative of one row per
    owner would multiply a single trade by N. Ten owners and one 15,000-share
    transaction was a real filing on 2026-09-11.
    """
    out: list[Owner] = []
    for block in root.findall("reportingOwner"):
        name = _clean(block.findtext("reportingOwnerId/rptOwnerName"))
        if not name:
            continue
        out.append(
            Owner(
                cik=_pad(_clean(block.findtext("reportingOwnerId/rptOwnerCik")) or ""),
                name=name,
                is_director=_flag(block.findtext("reportingOwnerRelationship/isDirector")),
                is_officer=_flag(block.findtext("reportingOwnerRelationship/isOfficer")),
                is_ten_percent_owner=_flag(
                    block.findtext("reportingOwnerRelationship/isTenPercentOwner")
                ),
                is_other=_flag(block.findtext("reportingOwnerRelationship/isOther")),
                officer_title=_clean(
                    block.findtext("reportingOwnerRelationship/officerTitle")
                ),
            )
        )
    return tuple(out)


def _from_table(
    root: ET.Element, table_kind: str, filing: _Filing
) -> Iterator[Transaction]:
    """One of the two transaction sections, numbered from 1 in document order.

    **Holdings are skipped.** `nonDerivativeHolding` and `derivativeHolding`
    report a balance rather than an event: no date, no code, nothing anybody
    did. Storing one as a transaction with a null date would put a non-event in
    a window.
    """
    prefix = "nonDerivative" if table_kind == "non_derivative" else "derivative"
    derivative = table_kind == "derivative"
    amounts = "transactionAmounts"
    seq = 0
    for node in root.findall(f"{prefix}Table/{prefix}Transaction"):
        when = _date(_clean(node.findtext("transactionDate/value")), "%Y-%m-%d")
        code = _clean(node.findtext("transactionCoding/transactionCode"))
        if when is None or not code:
            # Dropped rather than stored incomplete: a transaction with no date
            # cannot be put in a window and one with no code cannot be told from
            # a gift, so neither is evidence of anything.
            logger.warning(
                "%s: a %s transaction has no date or no code; skipping it",
                filing.accession_number, table_kind,
            )
            continue
        # Numbered only over what is kept, so the sequence is dense and a
        # re-walk of the same immutable document reproduces it exactly.
        seq += 1
        yield Transaction(
            accession_number=filing.accession_number,
            document_type=filing.document_type,
            table_kind=table_kind,
            seq=seq,
            issuer_cik=filing.issuer_cik,
            issuer_name=filing.issuer_name,
            issuer_symbol=filing.issuer_symbol,
            period_of_report=filing.period_of_report,
            filed_date=filing.filed_date,
            owners=filing.owners,
            security_title=_clean(node.findtext("securityTitle/value")),
            transaction_date=when,
            transaction_code=code,
            acquired_disposed=_clean(
                node.findtext(f"{amounts}/transactionAcquiredDisposedCode/value")
            ),
            shares=_decimal(node.findtext(f"{amounts}/transactionShares/value")),
            price_per_share=_decimal(
                node.findtext(f"{amounts}/transactionPricePerShare/value")
            ),
            shares_owned_after=_decimal(
                node.findtext(
                    "postTransactionAmounts/sharesOwnedFollowingTransaction/value"
                )
            ),
            direct_or_indirect=_clean(
                node.findtext("ownershipNature/directOrIndirectOwnership/value")
            ),
            conversion_or_exercise_price=(
                _decimal(node.findtext("conversionOrExercisePrice/value"))
                if derivative
                else None
            ),
            expiration_date=(
                _date(_clean(node.findtext("expirationDate/value")), "%Y-%m-%d")
                if derivative
                else None
            ),
            underlying_title=(
                _clean(
                    node.findtext("underlyingSecurity/underlyingSecurityTitle/value")
                )
                if derivative
                else None
            ),
            underlying_shares=(
                _decimal(
                    node.findtext("underlyingSecurity/underlyingSecurityShares/value")
                )
                if derivative
                else None
            ),
        )


def _get_text(
    url: str, *, user_agent: str, transport: httpx.BaseTransport | None
) -> str:
    """One GET, with SEC's refusals given their own type.

    No retry and no backoff. `screener.fetch` does not retry inside a strategy,
    and here that is the right policy rather than an inherited one: SEC's
    limiter answers a repeat by extending the block.
    """
    try:
        result = fetch(
            url,
            headers={"User-Agent": user_agent},
            timeout=TIMEOUT,
            transport=transport,
        )
    except Exception as exc:
        text = str(exc)
        if REFUSED in text or RATE_LIMITED in text:
            raise Throttled(f"sec refused {url}") from exc
        raise SourceError(f"edgar refused: {type(exc).__name__}") from exc
    if len(result.text) > MAX_SUBMISSION_BYTES:
        raise SourceError(f"{url} returned more than {MAX_SUBMISSION_BYTES} bytes")
    return result.text


def _get_json(
    url: str, *, user_agent: str, transport: httpx.BaseTransport | None
) -> dict:
    text = _get_text(url, user_agent=user_agent, transport=transport)
    try:
        import json

        payload = json.loads(text)
    except ValueError as exc:
        raise SourceError("edgar answered with something that is not JSON") from exc
    if not isinstance(payload, dict):
        raise SourceError("edgar returned something that is not a listing")
    return payload


def _flag(value: str | None) -> bool:
    """A Form 4 boolean, in either spelling SEC actually uses.

    Measured on 2026-09-11: AFLAC's filing carries `<isDirector>0</isDirector>`
    and Apple's carries `<isOfficer>true</isOfficer>`. Reading one spelling
    would silently make every officer at half the filers a non-officer, which is
    the kind of wrong that looks like a finding.
    """
    return (value or "").strip().lower() in ("1", "true", "yes", "y")


def _clean(value: str | None) -> str | None:
    """Trimmed text, or None for absent and for the empty elements SEC writes.

    `<officerTitle/>` for a filer who is not an officer and
    `<natureOfOwnership><value /></natureOfOwnership>` both arrive as an empty
    string rather than being omitted. Storing '' as though it were a job title
    would put it in a group-by.
    """
    text = (value or "").strip()
    return text or None


def _pad(cik: str) -> str:
    """A CIK as ten digits, whichever way it arrived.

    The daily index publishes it unpadded (`4977`) and the XML publishes it
    padded (`0000004977`). Normalising here is what lets the join against
    `security.cik` be a string comparison rather than an arithmetic one.
    """
    digits = "".join(ch for ch in cik if ch.isdigit())
    return digits.zfill(10) if digits else ""


def _decimal(value: str | None) -> Decimal | None:
    """A reported figure, kept as decimal text.

    Never a float. A price that changed in its seventh digit because it went
    through binary floating point is exactly the quiet wrongness this project
    spends its comments avoiding, and `store.content_hash` re-reads these as
    `str(Decimal(...))` so a re-walk of the same bytes hashes the same way.
    """
    text = _clean(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _date(value: str | None, fmt: str) -> date | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        return None
