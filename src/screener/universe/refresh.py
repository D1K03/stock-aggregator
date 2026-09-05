"""Build the committed CSV from Wikipedia, SEC and Yahoo.

Never opens a database connection. Everything fragile or slow lives here, in a
command run four times a year while someone is watching, rather than on the
nightly path.
"""

import csv
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from screener.fetch import LanePool
from screener.universe.rows import UniverseRow, write_rows
from screener.universe.sources.sec import cik_by_symbol
from screener.universe.sources.wikipedia import constituents
from screener.universe.sources.yahoo import Profile, YahooClient

logger = logging.getLogger(__name__)

UNRESOLVED_FIELDNAMES: tuple[str, ...] = ("symbol", "name", "index_name", "reason")


class ProfileSource(Protocol):
    """What `refresh` needs from Yahoo, so a test can supply it without a socket."""

    def profile(self, symbol: str) -> Profile | None: ...


@dataclass(frozen=True)
class RefreshReport:
    written: int
    unresolved: int


def refresh(
    out_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    delay: float = 0.0,
    client: ProfileSource | None = None,
    sleep: Callable[[float], None] | None = None,
    lanes: LanePool | None = None,
) -> RefreshReport:
    """Write `universe.csv` and `universe-unresolved.csv` into `out_dir`.

    A symbol Yahoo will not classify goes to the unresolved file rather than
    into the main one half-populated: the loader then needs no "is this row
    usable" branch, and the failures stay visible instead of polluting a diff.
    """
    members = constituents(transport=transport)
    ciks = cik_by_symbol(transport=transport)
    # Only a client we opened is ours to close; a caller's is the caller's.
    owned: YahooClient | None = None
    if client is None:
        owned = YahooClient(transport=transport, lanes=lanes)
        yahoo: ProfileSource = owned
    else:
        yahoo = client
    pause = sleep or time.sleep

    rows: list[UniverseRow] = []
    unresolved: list[dict[str, str]] = []

    try:
        for member in members:
            profile = yahoo.profile(member.symbol)
            if delay:
                pause(delay)
            if profile is None:
                unresolved.append(
                    {
                        "symbol": member.symbol,
                        "name": member.name,
                        "index_name": member.index_name,
                        "reason": "yahoo returned no usable profile",
                    }
                )
                continue
            rows.append(
                UniverseRow(
                    symbol=member.symbol,
                    name=member.name,
                    index_name=member.index_name,
                    mic=profile.mic,
                    currency=profile.currency,
                    cik=ciks.get(member.symbol, ""),
                    yf_sector=profile.sector,
                    yf_industry=profile.industry,
                    gics_sector=member.gics_sector,
                )
            )
    finally:
        if owned is not None:
            owned.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    written = write_rows(out_dir / "universe.csv", rows)

    with (out_dir / "universe-unresolved.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(UNRESOLVED_FIELDNAMES))
        writer.writeheader()
        for row in sorted(unresolved, key=lambda r: r["symbol"]):
            writer.writerow(row)

    logger.info("universe: %d written, %d unresolved", written, len(unresolved))
    return RefreshReport(written=written, unresolved=len(unresolved))
