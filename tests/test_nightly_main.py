"""The loop's judgement: what is retried, what is reported, and what is silent."""

from datetime import date

import pytest

from screener.nightly import NightlyConfig

TODAY = date(2026, 9, 15)


class FakeEvent:
    """Records what the loop waited for, and never actually waits."""

    def __init__(self):
        self.waits = []
        self._set = False

    def wait(self, seconds=None):
        self.waits.append(seconds)
        return self._set

    def is_set(self):
        return self._set

    def set(self):
        self._set = True


@pytest.fixture
def loop(monkeypatch):
    """The module with its event and its notifier replaced."""
    from screener.nightly import __main__ as module

    event = FakeEvent()
    sent = []
    monkeypatch.setattr(module, "stopping", event)
    monkeypatch.setattr(module, "announce", lambda day, reason: sent.append((day, reason)))
    return module, event, sent


def _config(attempts=3):
    return NightlyConfig(trigger_hour=23, attempts=attempts, enabled=True)


def test_a_good_night_runs_once_and_says_nothing(loop, monkeypatch):
    module, event, sent = loop
    calls = []

    class Report:
        ok = True

    monkeypatch.setattr(module, "_attempt", lambda config, today: calls.append(today) or Report())

    assert module.run_tonight(_config(), TODAY) is True
    assert len(calls) == 1
    assert sent == []
    assert event.waits == []


def test_a_gated_night_is_retried_then_reported_once(loop, monkeypatch):
    module, event, sent = loop
    calls = []

    class Report:
        ok = False
        class prices:
            requested = 1504
            failed = 1504

    monkeypatch.setattr(module, "_attempt", lambda config, today: calls.append(today) or Report())

    assert module.run_tonight(_config(), TODAY) is False
    assert len(calls) == 3
    # One message for the night, not one per attempt.
    assert len(sent) == 1
    assert sent[0][0] == TODAY
    # Five minutes, then fifteen; no wait after the last attempt.
    assert event.waits == [300, 900]


def test_scoring_already_in_progress_steps_aside_without_reporting(loop, monkeypatch):
    # Another process holds the lock. Retrying would be arguing with a run that
    # is working.
    from screener.scoring import ScoringInProgress

    module, event, sent = loop
    calls = []

    def boom(config, today):
        calls.append(today)
        raise ScoringInProgress("another scoring run holds the lock")

    monkeypatch.setattr(module, "_attempt", boom)

    assert module.run_tonight(_config(), TODAY) is False
    assert len(calls) == 1
    assert sent == []


def test_no_bars_visible_is_reported_without_spending_three_attempts(loop, monkeypatch):
    from screener.scoring import NoBarsVisible

    module, event, sent = loop
    calls = []

    def boom(config, today):
        calls.append(today)
        raise NoBarsVisible("no bars visible")

    monkeypatch.setattr(module, "_attempt", boom)

    assert module.run_tonight(_config(), TODAY) is False
    assert len(calls) == 1
    assert len(sent) == 1


def test_an_unexpected_failure_is_retried(loop, monkeypatch):
    module, event, sent = loop
    calls = []

    def boom(config, today):
        calls.append(today)
        raise RuntimeError("the network went away")

    monkeypatch.setattr(module, "_attempt", boom)

    module.run_tonight(_config(), TODAY)

    assert len(calls) == 3
    assert len(sent) == 1
    assert "RuntimeError" in sent[0][1]


def test_a_stop_signal_ends_the_retries_early(loop, monkeypatch):
    module, event, sent = loop
    calls = []

    def boom(config, today):
        calls.append(today)
        event.set()
        raise RuntimeError("down")

    monkeypatch.setattr(module, "_attempt", boom)

    module.run_tonight(_config(), TODAY)

    # Stopped after the first attempt rather than sitting through two backoffs
    # while the container is being torn down, and silent: an interrupted night
    # is not a lost one, because the next boot's catch-up finishes it.
    assert len(calls) == 1
    assert sent == []


def test_the_message_says_the_night_cannot_be_recovered(monkeypatch):
    # The actual urgency: scoring is forward-only, so this is a permanent hole
    # in the forward log rather than something tomorrow repairs.
    from screener.nightly import __main__ as module

    sent = []

    class FakeChannel:
        name = "fake"

        def send(self, alert):
            sent.append(alert)

    monkeypatch.setattr(module, "_channel", lambda: FakeChannel())

    module.announce(TODAY, "prices wholly failed")

    assert len(sent) == 1
    assert str(TODAY) in sent[0].title
    assert "backfill" in sent[0].body.lower()
    assert sent[0].severity == "warning"


def test_an_unconfigured_webhook_does_not_raise(monkeypatch, caplog):
    # A missing webhook must not turn a failed night into a crash loop.
    from screener.nightly import __main__ as module
    from screener.notify import ChannelError

    def no_channel():
        raise ChannelError("DISCORD_WEBHOOK_URL is not set")

    monkeypatch.setattr(module, "_channel", no_channel)

    module.announce(TODAY, "prices wholly failed")

    assert "webhook" in caplog.text.lower()


def test_the_wait_is_interruptible_rather_than_a_sleep():
    # The property `screener.reddit` documents and this inherits: a signal
    # handler cannot interrupt `time.sleep`, so SIGTERM would be answered
    # whenever the sleep happened to end and every deploy would sit through the
    # full SIGKILL timeout. An Event can be set from the handler.
    import threading

    from screener.nightly import __main__ as module

    assert isinstance(module.stopping, threading.Event)


def test_the_switch_stops_the_scheduler_before_it_connects(monkeypatch):
    from screener.nightly import __main__ as module

    monkeypatch.setattr(module, "load_into_environ", lambda: None)
    monkeypatch.setenv("NIGHTLY_ENABLED", "false")

    assert module.main() == 0
