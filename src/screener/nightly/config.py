"""Scheduler settings: when a night runs, how hard it tries, and the off switch."""

from dataclasses import dataclass

from screener.config import env

# 23:00 UTC, and the hour is the design rather than a preference. US markets
# close at 20:00 UTC in summer and 21:00 in winter, so this leaves two to three
# hours for Yahoo to settle -- and `screener.scoring.cli` refuses an `--as-of`
# in the past, so a run after midnight would score a day whose market has not
# opened rather than the one that just closed.
DEFAULT_TRIGGER_HOUR = 23

# Three, then the night is given up until tomorrow. Deliberately unlike
# `screener.reddit`, which retries every five minutes for ever: reddit's pass is
# cheap and idempotent, while a night is around 3,000 Yahoo requests, and
# hammering a source having a bad evening is how a rate limit nobody has hit
# becomes one that has been.
DEFAULT_ATTEMPTS = 3

# Between attempts. Five minutes, then fifteen.
BACKOFF_SECONDS: tuple[int, ...] = (300, 900)


@dataclass(frozen=True)
class NightlyConfig:
    trigger_hour: int = DEFAULT_TRIGGER_HOUR
    attempts: int = DEFAULT_ATTEMPTS
    enabled: bool = True

    def __post_init__(self) -> None:
        # A typo here would not fail until the container had waited most of a
        # day for an hour that never arrives.
        if not 0 <= self.trigger_hour <= 23:
            raise ValueError(
                f"NIGHTLY_TRIGGER_HOUR must be 0-23, got {self.trigger_hour}"
            )
        if self.attempts < 1:
            raise ValueError(f"NIGHTLY_ATTEMPTS must be at least 1, got {self.attempts}")

    @classmethod
    def from_env(cls) -> "NightlyConfig":
        return cls(
            trigger_hour=env.integer("NIGHTLY_TRIGGER_HOUR", DEFAULT_TRIGGER_HOUR),
            attempts=env.integer("NIGHTLY_ATTEMPTS", DEFAULT_ATTEMPTS),
            # An explicit boolean rather than something inferred. Reddit
            # switches off by holding an empty subreddit list, which works
            # because its work is described by data; a night has no such list,
            # so an unset trigger hour must mean the default hour and never
            # silence. The switch exists so a night can be stopped during an
            # incident without editing compose.
            enabled=env.text("NIGHTLY_ENABLED", "true").strip().lower()
            not in ("false", "0", "no", "off"),
        )
