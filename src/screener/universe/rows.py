"""The CSV contract: one row per security, sorted, stable field order.

The file is committed, so a diff is the review surface for both membership
changes and reclassifications. That only works if the ordering is deterministic
and the field order never drifts, which is why both are pinned here rather than
left to whatever a dict happens to iterate.
"""

import csv
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

FIELDNAMES: tuple[str, ...] = (
    "symbol",
    "name",
    "index_name",
    "mic",
    "currency",
    "cik",
    "yf_sector",
    "yf_industry",
    "gics_sector",
)


@dataclass(frozen=True)
class UniverseRow:
    symbol: str
    name: str
    index_name: str
    mic: str
    currency: str
    cik: str
    yf_sector: str
    yf_industry: str
    gics_sector: str


def normalise_symbol(text: str) -> str:
    """Wikipedia writes share classes as `BRK.B`; Yahoo wants `BRK-B`."""
    return text.strip().upper().replace(".", "-")


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def write_rows(path: Path, rows: Iterable[UniverseRow]) -> int:
    ordered = sorted(rows, key=lambda r: r.symbol)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIELDNAMES))
        writer.writeheader()
        for row in ordered:
            writer.writerow(asdict(row))
    return len(ordered)


def read_rows(path: Path) -> list[UniverseRow]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [UniverseRow(**r) for r in csv.DictReader(handle)]
