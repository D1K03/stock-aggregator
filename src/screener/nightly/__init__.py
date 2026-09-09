"""The nightly scheduler: one pass of the pipeline, once a night.

A fourth long-running service beside `bot`, `reddit` and `skybird`, and shaped
like `screener.reddit` -- PID 1, a `threading.Event` for the wait, its own
compose service. It changes no command: `night` calls `run_prices`,
`run_fundamentals` and `run_scoring` exactly as their own CLIs do, and decides
only when they run.

Why it exists at all: scoring is forward-only and `--as-of` refuses a past
date, so a night not run is a night that can never be recovered -- and
`DESIGN.md` says the forward log of daily snapshots is the only credible basis
for backtesting weights. Every day without a scheduler is a permanent hole.
"""

from screener.nightly.config import (
    BACKOFF_SECONDS,
    DEFAULT_ATTEMPTS,
    DEFAULT_TRIGGER_HOUR,
    NightlyConfig,
)
from screener.nightly.night import NightReport, already_scored, run_night
from screener.nightly.schedule import is_due, next_trigger

__all__ = [
    "BACKOFF_SECONDS",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_TRIGGER_HOUR",
    "NightReport",
    "NightlyConfig",
    "already_scored",
    "is_due",
    "next_trigger",
    "run_night",
]
