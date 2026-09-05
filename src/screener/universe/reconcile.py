"""Work out what changed, without touching anything.

Pure by design: no database, no clock, no filesystem. Every transition rule
lives here, so all of them can be tested exhaustively and cheaply, and `load`
is left with only the job of applying a decision someone else made.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from screener.universe.rows import UniverseRow, slugify

DEPARTURE_CEILING: float = 0.10


class AmbiguousIdentity(RuntimeError):
    """Two CSV rows resolved to one security. Never merged silently."""


@dataclass(frozen=True)
class ExistingSecurity:
    id: int
    cik: str
    symbol: str
    industry_code: str
    is_active: bool


@dataclass(frozen=True)
class Plan:
    new: tuple[UniverseRow, ...] = ()
    departed: tuple[ExistingSecurity, ...] = ()
    reclassified: tuple[tuple[ExistingSecurity, UniverseRow], ...] = ()
    renamed: tuple[tuple[ExistingSecurity, UniverseRow], ...] = ()
    reentered: tuple[tuple[ExistingSecurity, UniverseRow], ...] = ()
    unchanged: int = 0
    active_before: int = 0

    @property
    def departure_share(self) -> float:
        if not self.active_before:
            return 0.0
        return len(self.departed) / self.active_before

    def is_empty(self) -> bool:
        return not (self.new or self.departed or self.reclassified or self.renamed or self.reentered)

    def summary(self) -> str:
        return (
            f"{len(self.new)} new, {len(self.departed)} retired, "
            f"{len(self.reclassified)} reclassified, {len(self.renamed)} renamed, "
            f"{len(self.reentered)} re-entered, {self.unchanged} unchanged"
        )


def plan(rows: Sequence[UniverseRow], existing: Sequence[ExistingSecurity]) -> Plan:
    """Compare a CSV against current state and describe the difference.

    Resolution order is CIK, then current symbol, then treat as new. CIK first
    because a symbol is a mutable attribute: matching on it turns a rename into
    a departure plus an arrival and orphans the company's history.
    """
    by_cik = {e.cik: e for e in existing if e.cik}
    by_symbol = {e.symbol: e for e in existing}

    new: list[UniverseRow] = []
    reclassified: list[tuple[ExistingSecurity, UniverseRow]] = []
    renamed: list[tuple[ExistingSecurity, UniverseRow]] = []
    reentered: list[tuple[ExistingSecurity, UniverseRow]] = []
    unchanged = 0
    seen: dict[int, str] = {}

    for row in rows:
        match = by_cik.get(row.cik) if row.cik else None
        if match is None:
            match = by_symbol.get(row.symbol)
        if match is None:
            new.append(row)
            continue
        if match.id in seen:
            raise AmbiguousIdentity(
                f"rows {seen[match.id]!r} and {row.symbol!r} both resolve to "
                f"security {match.id} (cik {match.cik or 'none'})"
            )
        seen[match.id] = row.symbol

        acted = False
        if not match.is_active:
            reentered.append((match, row))
            acted = True
        if match.symbol != row.symbol:
            renamed.append((match, row))
            acted = True
        if match.industry_code != slugify(row.yf_industry or row.yf_sector):
            reclassified.append((match, row))
            acted = True
        if not acted:
            unchanged += 1

    departed = tuple(e for e in existing if e.is_active and e.id not in seen)
    return Plan(
        new=tuple(new),
        departed=departed,
        reclassified=tuple(reclassified),
        renamed=tuple(renamed),
        reentered=tuple(reentered),
        unchanged=unchanged,
        active_before=sum(1 for e in existing if e.is_active),
    )
