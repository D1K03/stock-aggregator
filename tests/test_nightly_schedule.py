"""When the next night is due, and what switches the scheduler off.

The hour is the whole design: scoring's `--as-of` refuses a past date, so a run
after midnight scores a day whose market has not opened rather than the one
that just closed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from screener.nightly import (
    BACKOFF_SECONDS,
    DEFAULT_ATTEMPTS,
    DEFAULT_TRIGGER_HOUR,
    NightlyConfig,
    is_due,
    next_trigger,
)


def _at(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def test_the_default_hour_is_after_the_us_close():
    # 23:00 UTC is 19:00 ET, three hours after a 16:00 ET close.
    assert DEFAULT_TRIGGER_HOUR == 23


def test_a_moment_before_the_trigger_waits_for_today():
    assert next_trigger(_at(22, 59), 23) == _at(23)


def test_the_trigger_moment_itself_waits_for_tomorrow():
    # Strictly after, so a run that finishes at 23:00:00 does not immediately
    # re-trigger.
    assert next_trigger(_at(23, 0), 23) == _at(23, day=16)


def test_a_moment_after_the_trigger_waits_for_tomorrow():
    assert next_trigger(_at(23, 1), 23) == _at(23, day=16)


def test_after_midnight_waits_for_tonight_not_a_week():
    assert next_trigger(_at(0, 1, day=16), 23) == _at(23, day=16)


def test_the_trigger_does_not_drift():
    # The failure this exists to prevent: a fixed 24-hour interval slips by the
    # length of each pass, about ten minutes a day and five hours a month, and
    # the hour is what the whole design turns on.
    first = next_trigger(_at(23, 1), 23)
    # A pass that took eleven minutes, then the next wait.
    second = next_trigger(first + timedelta(minutes=11), 23)

    assert second - first == timedelta(days=1)
    assert second.hour == 23 and second.minute == 0


def test_a_night_is_due_once_the_hour_has_passed():
    assert is_due(_at(23, 0), 23) is True
    assert is_due(_at(23, 59), 23) is True


def test_a_night_is_not_due_before_the_hour():
    # The half that stops a container booting at 10:00 from running the night
    # thirteen hours early, against a market still open.
    assert is_due(_at(10, 0), 23) is False
    assert is_due(_at(22, 59), 23) is False


def test_the_config_defaults_need_no_environment(monkeypatch):
    for name in ("NIGHTLY_TRIGGER_HOUR", "NIGHTLY_ATTEMPTS", "NIGHTLY_ENABLED"):
        monkeypatch.delenv(name, raising=False)

    config = NightlyConfig.from_env()

    assert config.trigger_hour == DEFAULT_TRIGGER_HOUR
    assert config.attempts == DEFAULT_ATTEMPTS
    assert config.enabled is True


def test_the_switch_is_explicit(monkeypatch):
    monkeypatch.setenv("NIGHTLY_ENABLED", "false")
    assert NightlyConfig.from_env().enabled is False

    monkeypatch.setenv("NIGHTLY_ENABLED", "true")
    assert NightlyConfig.from_env().enabled is True


def test_an_unset_trigger_hour_means_the_default_not_silence(monkeypatch):
    # Reddit switches off by holding an empty subreddit list, which works
    # because its work is described by data. A night has no such list.
    monkeypatch.delenv("NIGHTLY_TRIGGER_HOUR", raising=False)
    monkeypatch.delenv("NIGHTLY_ENABLED", raising=False)

    config = NightlyConfig.from_env()

    assert config.enabled is True
    assert config.trigger_hour == DEFAULT_TRIGGER_HOUR


def test_an_impossible_hour_is_refused(monkeypatch):
    monkeypatch.setenv("NIGHTLY_TRIGGER_HOUR", "25")
    with pytest.raises(ValueError):
        NightlyConfig.from_env()


def test_the_backoffs_lengthen():
    assert BACKOFF_SECONDS == (300, 900)
