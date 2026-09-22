"""A setting changed under a running worker is the one its next pass uses.

`screener.secrets.watch()` keeps `os.environ` in step with Infisical. These are
the other half of that promise: each long-running worker reads its
configuration at the top of every pass rather than once at boot, and one that
has been switched off stops before a pass rather than after it.
"""

from types import ModuleType

import pytest


# Passes a test allows before stopping the loop itself. A worker that has gone
# back to reading its configuration once would otherwise loop for ever on the
# value it booted with, and hang the suite rather than fail it.
PASSES = 5


class FakeEvent:
    """A stop event that never waits, so a loop runs its passes back to back."""

    def __init__(self) -> None:
        self._set = False

    def wait(self, seconds: float | None = None) -> bool:
        return self._set

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True


@pytest.fixture
def worker(monkeypatch):
    """Ready a worker's `main()` to run passes against a `once` the test supplies."""

    def prepare(module: ModuleType) -> ModuleType:
        monkeypatch.setattr(module, "load_into_environ", lambda: None)
        monkeypatch.setattr(module, "watch", lambda *args, **kwargs: None)
        monkeypatch.setattr(module, "stopping", FakeEvent())
        monkeypatch.setattr(module.signal, "signal", lambda *args: None)
        if hasattr(module, "sys"):
            monkeypatch.setattr(module.sys, "argv", ["python -m screener.worker"])
        return module

    return prepare


def test_rupert_spends_against_the_budget_infisical_holds_now(worker, monkeypatch):
    # The one that costs money. A budget lowered in Infisical is the one the
    # next pass spends against, and a zero stops it before that pass.
    from screener.rupert import __main__ as rupert

    worker(rupert)
    monkeypatch.setenv("RUPERT_DAILY_MAX_CALLS", "40")
    budgets = []

    def once(config):
        budgets.append(config.daily_max_calls)
        monkeypatch.setenv("RUPERT_DAILY_MAX_CALLS", "25" if len(budgets) == 1 else "0")
        if len(budgets) >= PASSES:
            rupert.stopping.set()

    monkeypatch.setattr(rupert, "once", once)

    assert rupert.main() == 0
    assert budgets == [40, 25]


def test_edgar_gives_sec_the_address_infisical_holds_now(worker, monkeypatch):
    from screener.edgar import __main__ as edgar

    worker(edgar)
    monkeypatch.setenv("EDGAR_CONTACT_EMAIL", "first@example.com")
    addresses = []

    def once(config):
        addresses.append(config.contact_email)
        if len(addresses) == 1:
            monkeypatch.setenv("EDGAR_CONTACT_EMAIL", "second@example.com")
        else:
            monkeypatch.delenv("EDGAR_CONTACT_EMAIL", raising=False)
        if len(addresses) >= PASSES:
            edgar.stopping.set()

    monkeypatch.setattr(edgar, "once", once)

    assert edgar.main() == 0
    assert addresses == ["first@example.com", "second@example.com"]


def test_edgar_stops_rather_than_send_sec_an_address_it_refuses(worker, monkeypatch):
    # A typo made under a running process is refused the way one made before
    # boot is: SEC answers a github.com address with a 403 that reads as a
    # network fault, so it is never sent.
    from screener.edgar import __main__ as edgar

    worker(edgar)
    monkeypatch.setenv("EDGAR_CONTACT_EMAIL", "first@example.com")
    addresses = []

    def once(config):
        addresses.append(config.contact_email)
        monkeypatch.setenv("EDGAR_CONTACT_EMAIL", "someone@github.com")
        if len(addresses) >= PASSES:
            edgar.stopping.set()

    monkeypatch.setattr(edgar, "once", once)

    assert edgar.main() == 1
    assert addresses == ["first@example.com"]


def test_reddit_walks_the_subreddits_infisical_holds_now(worker, monkeypatch):
    from screener.reddit import __main__ as reddit

    worker(reddit)
    monkeypatch.setenv("REDDIT_SUBREDDITS", "stocks")
    walked = []

    def once(config):
        walked.append(config.subreddits)
        if len(walked) == 1:
            monkeypatch.setenv("REDDIT_SUBREDDITS", "stocks, wallstreetbets")
        else:
            reddit.stopping.set()

    monkeypatch.setattr(reddit, "once", once)

    assert reddit.main() == 0
    assert walked == [("stocks",), ("stocks", "wallstreetbets")]
