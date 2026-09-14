"""The screen endpoints' query strings, refused by name when wrong (ui-swap D9, D10).

Pure. A parameter that is present but wrong is a 400 naming it, never a quiet
default: a page that asked for `sort=value` and silently got `score` would show a
screen in the wrong order with nothing on it saying so.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
# Far past any real screen (~1,500 rows), and short of anything that would reach
# Postgres as an out-of-range integer and come back as a 503.
MAX_OFFSET = 1_000_000
# Long enough for any listed symbol with a class suffix.
MAX_SYMBOL = 16
SORTS: tuple[str, ...] = ("score", "delta", "V", "Q", "M")
PARTIALS: tuple[str, ...] = ("only", "hide")

_MAX_ID = 2**63 - 1
# The universe loader's sector codes are lowercase words joined by hyphens. Only
# the shape is checked: whether a code exists is a question about the run's rows,
# and an unknown one returns an empty page (plan amendment P2).
_SECTOR = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SCREEN = frozenset({"run", "sector", "agree", "partial", "sort", "offset", "limit"})
_SECURITY = frozenset({"symbol", "run"})


class BadParameter(ValueError):
    """A query-string parameter the endpoint cannot honour."""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name


@dataclass(frozen=True)
class ScreenParams:
    run: int | None = None
    sector: str | None = None
    agree: int | None = None
    partial: str | None = None
    sort: str = "score"
    offset: int = 0
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True)
class SecurityParams:
    symbol: str
    run: int | None = None


def _known(query: Mapping[str, Sequence[str]], allowed: frozenset[str]) -> None:
    for name in sorted(query):
        if name not in allowed:
            raise BadParameter(name, f"unknown parameter {name!r}")


def _one(query: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = query.get(name) or []
    if len(values) > 1:
        raise BadParameter(name, f"{name} given more than once")
    return values[0] if values else None


def _integer(raw: str | None, name: str, *, low: int, high: int) -> int | None:
    if raw is None:
        return None
    # ASCII digits only: `int` also accepts " 7", "+7", "7_000" and other
    # scripts' digits, none of which a page sends.
    if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:
        raise BadParameter(name, f"{name} must be a whole number from {low} to {high}")
    return int(raw)


def _choice(raw: str | None, name: str, choices: Sequence[str]) -> str | None:
    if raw is not None and raw not in choices:
        raise BadParameter(name, f"{name} must be one of {', '.join(choices)}")
    return raw


def screen_params(query: Mapping[str, Sequence[str]]) -> ScreenParams:
    """`/api/screen`'s parameters, from `urllib.parse.parse_qs` output."""
    _known(query, _SCREEN)
    sector = _one(query, "sector")
    if sector is not None and not _SECTOR.fullmatch(sector):
        raise BadParameter("sector", "sector must be a sector code or 'unclassified'")
    offset = _integer(_one(query, "offset"), "offset", low=0, high=MAX_OFFSET)
    limit = _integer(_one(query, "limit"), "limit", low=1, high=MAX_LIMIT)
    return ScreenParams(
        run=_integer(_one(query, "run"), "run", low=1, high=_MAX_ID),
        sector=sector,
        agree=_integer(_one(query, "agree"), "agree", low=1, high=3),
        partial=_choice(_one(query, "partial"), "partial", PARTIALS),
        sort=_choice(_one(query, "sort"), "sort", SORTS) or "score",
        offset=0 if offset is None else offset,
        limit=DEFAULT_LIMIT if limit is None else limit,
    )


def security_params(query: Mapping[str, Sequence[str]]) -> SecurityParams:
    """`/api/screen/security`'s parameters. A leading `$` is dropped, as people type one."""
    _known(query, _SECURITY)
    symbol = (_one(query, "symbol") or "").strip().removeprefix("$")
    if not symbol:
        raise BadParameter("symbol", "symbol is required")
    if len(symbol) > MAX_SYMBOL:
        raise BadParameter("symbol", f"symbol is longer than {MAX_SYMBOL} characters")
    return SecurityParams(
        symbol=symbol, run=_integer(_one(query, "run"), "run", low=1, high=_MAX_ID)
    )
