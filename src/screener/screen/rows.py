"""Typed rows for the screen, parsed from either place a row comes from.

The status service reads on the application's own connection, where psycopg
hands back `Decimal`, `date` and `datetime`. Steven reads through
`playground.select`, whose `Result` cells were made JSON-safe on the way out --
decimals as strings, dates and times as ISO strings (ui-swap spec F12). Shaping
and adjustment must not care which, so both are parsed here into one record and
nothing past this module sees a raw cell.

Pure: no connection, no socket.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import NoneType, UnionType
from typing import Any, TypeVar, cast, get_args, get_type_hints

R = TypeVar("R")


@dataclass(frozen=True)
class RunRow:
    id: int
    as_of: date
    started_at: datetime
    finished_at: datetime | None
    git_sha: str
    config_hash: str
    weight_version: str
    weight_version_id: int
    cutoff_offset_seconds: int
    logic: str
    emits_alerts: bool


@dataclass(frozen=True)
class PreviousRunRow:
    id: int
    as_of: date


@dataclass(frozen=True)
class ScreenRow:
    security_id: int
    symbol: str
    name: str
    sector_code: str
    sector_name: str
    score: Decimal
    previous_score: Decimal | None
    v_score: Decimal | None
    v_coverage: Decimal | None
    q_score: Decimal | None
    q_coverage: Decimal | None
    m_score: Decimal | None
    m_coverage: Decimal | None
    pillar_agreement: int
    min_coverage: Decimal


@dataclass(frozen=True)
class TilesRow:
    scored: int
    active_now: int
    partial: int
    agreement_3: int
    market_ranked_values: int


@dataclass(frozen=True)
class SectorRow:
    code: str
    name: str


@dataclass(frozen=True)
class BarRow:
    security_id: int
    trade_date: date
    close: Decimal
    observed_at: datetime


@dataclass(frozen=True)
class ActionRow:
    security_id: int
    effective_date: date
    action_type: str
    ratio: Decimal | None
    amount: Decimal | None


@dataclass(frozen=True)
class SymbolRow:
    security_id: int
    symbol: str
    name: str
    mic: str
    is_active: bool


@dataclass(frozen=True)
class ClassificationRow:
    sector_code: str
    sector_name: str
    industry_code: str | None
    industry_name: str | None


@dataclass(frozen=True)
class SnapshotRow:
    score: Decimal
    pillar_agreement: int
    min_coverage: Decimal


@dataclass(frozen=True)
class PillarRow:
    pillar_code: str
    score: Decimal
    metric_count: int
    coverage: Decimal


@dataclass(frozen=True)
class MetricRow:
    code: str
    raw_value: Decimal
    percentile: Decimal
    peer_group: str
    peer_count: int
    fallback_level: int
    period_basis: str | None
    period_end: date | None


@dataclass(frozen=True)
class MetricInfoRow:
    code: str
    name: str
    higher_is_better: bool
    pillar_code: str


def _concrete(hint: Any) -> Any:
    """`Decimal | None` -> `Decimal`; any other annotation unchanged."""
    if isinstance(hint, UnionType):
        (inner,) = [arg for arg in get_args(hint) if arg is not NoneType]
        return inner
    return hint


def _cell(kind: Any, value: Any) -> Any:
    if value is None:
        return None
    if kind is Decimal and not isinstance(value, Decimal):
        return Decimal(value)
    if kind is datetime and not isinstance(value, datetime):
        return datetime.fromisoformat(cast(str, value))
    if kind is date and not isinstance(value, date):
        return date.fromisoformat(cast(str, value))
    return value


def parse(record: type[R], cells: Sequence[Any]) -> R:
    """One row into `record`, its cells taken in the record's field order.

    A row of the wrong width is refused rather than zipped short: a query that
    gained or lost a column would otherwise shift values into the wrong fields
    and still construct a record.
    """
    hints = get_type_hints(record)
    if len(cells) != len(hints):
        raise ValueError(f"{record.__name__} takes {len(hints)} cells, got {len(cells)}")
    return record(
        **{
            name: _cell(_concrete(hint), cell)
            for (name, hint), cell in zip(hints.items(), cells)
        }
    )


def parse_all(record: type[R], rows: Sequence[Sequence[Any]]) -> list[R]:
    return [parse(record, row) for row in rows]
