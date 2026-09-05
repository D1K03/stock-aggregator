"""Chart JSON to bars and corporate actions. No I/O, no database, no clock.

Kept pure so the awkward cases — nulls in the quote arrays, an error response,
an events block that is simply absent — are tested without a socket.

`Decimal` throughout rather than float. Prices are money, and a float close of
99.99999999999999 stored in a `numeric` column is a value nobody typed.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Bar:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Action:
    effective_date: date
    action_type: str
    ratio: Decimal | None
    amount: Decimal | None


def _day(epoch: int) -> date:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date()


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    # str() first: Decimal(float) preserves the binary error rather than the
    # number the provider meant.
    number = Decimal(str(value))
    if not number.is_finite():
        # Python's json decoder accepts the bare `NaN` and `Infinity` tokens,
        # `Decimal("NaN")` is a perfectly good Decimal, and Postgres `numeric`
        # stores 'NaN' happily — so without this a NaN close lands in a
        # not-null column and poisons every momentum metric derived from it.
        # None, so the caller's null check drops the bar like any other
        # unusable one.
        return None
    return number


def parse(payload: bytes) -> tuple[list[Bar], list[Action]]:
    """Bars and actions, or two empty lists.

    Never raises. A malformed or error response is one security failing, which
    the caller counts and the next night's wider window repairs.
    """
    try:
        result = json.loads(payload)["chart"]["result"]
    except (KeyError, TypeError, ValueError):
        return [], []
    if not result:
        return [], []
    block = result[0]

    stamps = block.get("timestamp") or []
    quotes = (block.get("indicators") or {}).get("quote") or [{}]
    quote = quotes[0] if quotes else {}

    bars: list[Bar] = []
    for index, stamp in enumerate(stamps):
        try:
            open_val = _decimal(_at(quote, "open", index))
            high_val = _decimal(_at(quote, "high", index))
            low_val = _decimal(_at(quote, "low", index))
            close_val = _decimal(_at(quote, "close", index))
            volume_val = _at(quote, "volume", index)
            # A null anywhere drops the bar. The columns are `not null`, and a
            # zero-filled bar reads downstream as a real -100% move.
            if (
                open_val is None
                or high_val is None
                or low_val is None
                or close_val is None
                or volume_val is None
            ):
                continue
            bars.append(
                Bar(
                    trade_date=_day(stamp),
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    close=close_val,
                    volume=int(volume_val),  # type: ignore[arg-type]
                )
            )
        except (InvalidOperation, TypeError, ValueError, OverflowError, OSError):
            # A malformed bar (non-numeric value, out-of-range timestamp, etc.)
            # is dropped; the rest of the payload is processed normally.
            continue

    bars.sort(key=lambda bar: bar.trade_date)

    events = block.get("events") or {}
    actions: list[Action] = []
    for raw in (events.get("splits") or {}).values():
        try:
            numerator = _decimal(raw.get("numerator"))
            # Decimal("0") is falsy, so this guards a malformed zero denominator
            # as well as an absent one.
            denominator = _decimal(raw.get("denominator")) or Decimal(1)
            actions.append(
                Action(
                    effective_date=_day(raw["date"]),
                    action_type="split",
                    ratio=(numerator / denominator) if numerator else None,
                    amount=None,
                )
            )
        except (
            InvalidOperation,
            TypeError,
            ValueError,
            OverflowError,
            OSError,
            AttributeError,
            KeyError,
        ):
            # A malformed split entry (missing date, not a dict, non-numeric
            # numerator, etc.) is dropped; processing continues.
            continue

    for raw in (events.get("dividends") or {}).values():
        try:
            actions.append(
                Action(
                    effective_date=_day(raw["date"]),
                    action_type="dividend",
                    ratio=None,
                    amount=_decimal(raw.get("amount")),
                )
            )
        except (
            InvalidOperation,
            TypeError,
            ValueError,
            OverflowError,
            OSError,
            AttributeError,
            KeyError,
        ):
            # A malformed dividend entry (missing date, not a dict, etc.) is
            # dropped; processing continues.
            continue

    actions.sort(key=lambda action: (action.effective_date, action.action_type))
    return bars, actions


def _at(quote: dict[str, list[object]], name: str, index: int) -> object:
    values = quote.get(name) or []
    return values[index] if index < len(values) else None
