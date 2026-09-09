# Nightly Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A container that runs prices, fundamentals and scoring once a night at 23:00 UTC, recovers a night interrupted by a deploy, and says so in Discord when a night is lost for good.

**Architecture:** `screener.nightly` is a fourth long-running service alongside `bot`, `reddit` and `skybird`, following `screener.reddit` exactly: PID 1, a `threading.Event` so SIGTERM is answered promptly, `restart: unless-stopped`. Four modules — pure clock arithmetic, pure-ish configuration, one night's orchestration, and the loop that owns the signals and the retries. **No existing command changes**: `night.py` calls `run_prices`, `run_fundamentals` and `run_scoring` exactly as `screener.ingest.cli` and `screener.scoring.cli` already do.

**Tech Stack:** Python 3.11+, `psycopg` 3, Postgres 16, `pytest`, `pyright`. No new dependencies.

**Spec:** `docs/specs/2026-09-07-nightly-scheduling.md`

## Global Constraints

- **No new runtime dependencies.** `httpx`, `psycopg` and `discord.py` are the whole runtime list.
- **No command behaviour changes.** Nothing in `screener.ingest` or `screener.scoring` is modified. This cycle decides *when* they run and nothing else.
- **All SQL identifiers lowercase; `timestamptz` never `timestamp`.**
- **psycopg types query parameters as `LiteralString`** — every query is a single literal string with placeholders, never concatenation or an f-string, in `src/` or in `tests/`.
- **Each package has a small public surface through `__init__.py`; nothing outside imports a submodule directly.** A package's own `__main__` importing its siblings is the established exception.
- **`__all__` ordering follows ruff's RUF022** — SCREAMING_CASE, then CamelCase, then snake_case, alphabetical within each group — *not* plain `sorted()`. `src/screener/ingest/__init__.py` is the precedent.
- **Comments explain *why*, not *what*.**
- **All times are UTC.** `visibility_cutoff` builds from UTC midnight, so a local-time trigger would move twice a year relative to arithmetic defined in UTC.
- **Every `scoring_run` this schedules still has `emits_alerts = false`.** No crossing is computed and no `alert_rule` is read.
- `pyright` must report zero errors, and it checks `tests/` as well as `src/`.

## Environment

```bash
export DATABASE_URL_TEST="postgresql://postgres:screener@localhost:5432/screener_test"
```

The value committed in `.env` is a unix-socket URL that does **not** reach the Docker container — ignore it. Use `.venv/bin/python -m pytest` and **always pass an explicit worker count**: `-n0` for a single file, `-n 4` for the whole suite. Bare `pytest` uses `-n auto`, which spawns 24+ workers and kills the container's Postgres with OutOfMemory — an environment limit, not a defect in your work.

Baseline before Task 1: **875 passed, 1 skipped.**

---

### Task 1: `config.py` and `schedule.py` — the settings and the clock

Both are pure and small, and the clock arithmetic is the thing D3 turns on, so they are tested together and land together.

**Files:**
- Create: `src/screener/nightly/__init__.py`
- Create: `src/screener/nightly/config.py`
- Create: `src/screener/nightly/schedule.py`
- Test: `tests/test_nightly_schedule.py`

**Interfaces:**
- Consumes: `screener.config.env` (`env.text`, `env.integer`).
- Produces:
  - `DEFAULT_TRIGGER_HOUR: int` = 23, `DEFAULT_ATTEMPTS: int` = 3, `BACKOFF_SECONDS: tuple[int, ...]` = `(300, 900)`.
  - `NightlyConfig` — frozen dataclass: `trigger_hour: int`, `attempts: int`, `enabled: bool`; classmethod `from_env() -> NightlyConfig`.
  - `next_trigger(now: datetime, hour: int) -> datetime`
  - `is_due(now: datetime, hour: int) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_nightly_schedule.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightly_schedule.py -n0 -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.nightly'`.

- [ ] **Step 3: Write `config.py`**

Create `src/screener/nightly/config.py`:

```python
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
```

- [ ] **Step 4: Write `schedule.py`**

Create `src/screener/nightly/schedule.py`:

```python
"""When the next night is due. No I/O, no clock of its own.

Every function here takes the moment as an argument, so the loop owns the one
call to `datetime.now` and everything below it is testable without waiting.
"""

from datetime import datetime, time, timedelta, timezone


def next_trigger(now: datetime, hour: int) -> datetime:
    """The next `hour`:00 UTC strictly after `now`.

    Computed from the wall clock rather than by adding a day to the last run: a
    fixed interval drifts by the length of each pass -- about ten minutes a day
    and five hours a month -- and the hour is what the whole design turns on.

    Strictly after, so a night that finishes exactly on the hour waits for
    tomorrow rather than triggering itself again.
    """
    tonight = datetime.combine(now.date(), time(hour=hour), tzinfo=timezone.utc)
    return tonight if tonight > now else tonight + timedelta(days=1)


def is_due(now: datetime, hour: int) -> bool:
    """Whether tonight's run is owed on the clock alone.

    Half of the catch-up condition; the caller pairs it with "has today already
    been scored". Both halves matter: without the other, a restart would score
    a night twice, and without this one a container starting at 10:00 would run
    the night thirteen hours early, against a market still open.

    `now` must be UTC-aware -- everything here is UTC, because
    `screener.scoring.visibility_cutoff` builds from UTC midnight and a
    local-time trigger would move twice a year against arithmetic defined in
    UTC.
    """
    return now.hour >= hour
```

- [ ] **Step 5: Write the package surface**

Create `src/screener/nightly/__init__.py`:

```python
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
from screener.nightly.schedule import is_due, next_trigger

__all__ = [
    "BACKOFF_SECONDS",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_TRIGGER_HOUR",
    "NightlyConfig",
    "is_due",
    "next_trigger",
]
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_nightly_schedule.py -n0 -v`
Expected: PASS, 12 passed.

- [ ] **Step 7: Typecheck and commit**

```bash
.venv/bin/pyright
git add src/screener/nightly tests/test_nightly_schedule.py
git commit -m "Work out when the next night is due, without drifting"
```

Expected: `pyright` reports zero errors.

---

### Task 2: `night.py` — one night, and the gate

**Files:**
- Create: `src/screener/nightly/night.py`
- Modify: `src/screener/nightly/__init__.py`
- Test: `tests/test_nightly_night.py`

**Interfaces:**
- Consumes: `NightlyConfig` (Task 1, not used here but the package surface grows alongside it).
- Produces:
  - `NightReport` — frozen dataclass: `prices: IngestReport`, `fundamentals: FundamentalsReport`, `scoring: ScoringReport | None`; property `ok: bool` (true when `scoring is not None`).
  - `already_scored(conn: psycopg.Connection, day: date) -> bool`
  - `run_night(conn, *, today: date, blobs, chart, timeseries) -> NightReport`

- [ ] **Step 1: Write the failing test**

Create `tests/test_nightly_night.py`:

```python
"""One night: the order, the gate, and what counts as done.

The gate is the point. `cutoff_offset` filters on `observed_at`, so yesterday's
bars stay visible -- scoring after a dead ingest would write a
complete-looking snapshot from stale data, and tomorrow's crossing diff would
read it as "nothing moved".
"""

import json
from datetime import date, timedelta

import pytest

from screener.nightly import NightReport, already_scored, run_night

TODAY = date(2026, 9, 15)


class FakeChart:
    """Bars for every symbol, or none at all."""

    def __init__(self, *, answer: bool = True):
        self.answer = answer

    def fetch(self, symbol, start, end):
        if not self.answer:
            return None
        return json.dumps({
            "chart": {"result": [{
                "timestamp": [1757894400],
                "indicators": {"quote": [{
                    "open": [1.0], "high": [1.0], "low": [1.0],
                    "close": [1.0], "volume": [1],
                }]},
            }]}
        }).encode()


class FakeTimeseries:
    def __init__(self, *, answer: bool = True):
        self.answer = answer

    def fetch(self, symbol):
        if not self.answer:
            return None
        return json.dumps({"timeseries": {"result": []}}).encode()


class FakeBlobs:
    def __init__(self):
        self.written = {}

    def put(self, path, data):
        self.written[path] = data

    def get(self, path):
        return self.written[path]


@pytest.fixture
def two(fresh_db):
    for name, symbol in (("Alpha", "AAA"), ("Beta", "BBB")):
        fresh_db.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01')""",
            (name, symbol),
        )
    return fresh_db


def _scored(conn, day, outcome="ok", status="live"):
    logic = conn.execute(
        "select id from scoring_logic_version order by id limit 1"
    ).fetchone()[0]
    weight = conn.execute(
        "select id from weight_version where code = 'v1'"
    ).fetchone()[0]
    conn.execute(
        """insert into scoring_run
           (as_of_range, cutoff_offset, logic_version_id, weight_version_id,
            status, emits_alerts, git_sha, config_hash, started_at, outcome)
           values (daterange(%s, %s, '[)'), '1 day 6 hours', %s, %s,
                   %s, false, 'abc', '\\x00'::bytea, now(), %s)""",
        (day, day + timedelta(days=1), logic, weight, status, outcome),
    )


def test_a_night_with_nothing_scored_is_not_already_scored(fresh_db):
    assert already_scored(fresh_db, TODAY) is False


def test_a_successful_live_run_today_counts_as_scored(fresh_db):
    _scored(fresh_db, TODAY)
    assert already_scored(fresh_db, TODAY) is True


def test_a_failed_run_today_does_not_count_as_scored(fresh_db):
    # Otherwise the catch-up check would treat a lost night as a finished one.
    _scored(fresh_db, TODAY, outcome="failed")
    assert already_scored(fresh_db, TODAY) is False


def test_a_run_still_going_does_not_count_as_scored(fresh_db):
    _scored(fresh_db, TODAY, outcome="running")
    assert already_scored(fresh_db, TODAY) is False


def test_yesterdays_run_does_not_count_as_tonight(fresh_db):
    _scored(fresh_db, TODAY - timedelta(days=1))
    assert already_scored(fresh_db, TODAY) is False


def test_prices_wholly_failing_stops_scoring(two, monkeypatch):
    called = []
    monkeypatch.setattr(
        "screener.nightly.night.run_scoring",
        lambda *a, **k: called.append(k) or (_ for _ in ()).throw(AssertionError),
    )

    report = run_night(
        two, today=TODAY, blobs=FakeBlobs(),
        chart=FakeChart(answer=False), timeseries=FakeTimeseries(),
    )

    assert called == []
    assert report.scoring is None
    assert report.ok is False
    assert report.prices.status == "failed"


def test_a_partial_ingest_still_scores(two, monkeypatch):
    # CWEN-A fails every single night -- the only failure in 3,012 requests
    # across a measured night. A gate that stopped on any per-security failure
    # would stop every night.
    class OneBadSymbol(FakeChart):
        def fetch(self, symbol, start, end):
            return None if symbol == "BBB" else super().fetch(symbol, start, end)

    called = []
    monkeypatch.setattr(
        "screener.nightly.night.run_scoring",
        lambda *a, **k: called.append(k) or "scored",
    )

    report = run_night(
        two, today=TODAY, blobs=FakeBlobs(),
        chart=OneBadSymbol(), timeseries=FakeTimeseries(),
    )

    assert len(called) == 1
    assert report.prices.status == "partial"
    assert report.ok is True


def test_a_fundamentals_failure_does_not_stop_scoring(two, monkeypatch):
    # Scoring reads bars and nothing else this cycle: no ratio consumes a
    # fundamental fact yet, so a fundamentals failure must not block a run that
    # does not depend on it. This widens when the ratios cycle lands.
    called = []
    monkeypatch.setattr(
        "screener.nightly.night.run_scoring",
        lambda *a, **k: called.append(k) or "scored",
    )

    report = run_night(
        two, today=TODAY, blobs=FakeBlobs(),
        chart=FakeChart(), timeseries=FakeTimeseries(answer=False),
    )

    assert len(called) == 1
    assert report.fundamentals.status == "failed"
    assert report.ok is True


def test_scoring_is_asked_for_todays_date(two, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "screener.nightly.night.run_scoring",
        lambda conn, **k: seen.update(k) or "scored",
    )

    run_night(
        two, today=TODAY, blobs=FakeBlobs(),
        chart=FakeChart(), timeseries=FakeTimeseries(),
    )

    assert seen["as_of"] == TODAY
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightly_night.py -n0 -v`
Expected: FAIL with `ImportError: cannot import name 'NightReport' from 'screener.nightly'`.

- [ ] **Step 3: Write the implementation**

Create `src/screener/nightly/night.py`:

```python
"""One night: prices, then fundamentals, then scoring.

Ordering is by construction rather than by clock arithmetic. Three separately
triggered jobs would encode "scoring runs after ingest" as an assumption about
gaps between trigger times, which breaks the first evening ingest runs long.

Nothing here changes what a command does. These are the same three functions
`screener.ingest.cli` and `screener.scoring.cli` call.
"""

import logging
from dataclasses import dataclass
from datetime import date

import psycopg

from screener.blobs import BlobStore
from screener.ingest import (
    FundamentalsReport,
    IngestReport,
    active_securities,
    run_fundamentals,
    run_prices,
)
from screener.scoring import ScoringReport, run_scoring

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NightReport:
    prices: IngestReport
    fundamentals: FundamentalsReport
    # None when the gate stopped it. A night that did not score is a night that
    # did not happen, because the snapshot is the point.
    scoring: ScoringReport | None

    @property
    def ok(self) -> bool:
        return self.scoring is not None


def already_scored(conn: psycopg.Connection, day: date) -> bool:
    """Whether a live run has already scored `day` successfully.

    Half of the catch-up condition. `outcome = 'ok'` rather than merely the row
    existing: a run left `running` by a killed process, or marked `failed` on
    the way out, is a night still owed rather than one finished.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from scoring_run "
            "where status = 'live' and outcome = 'ok' "
            "  and as_of_range @> %s::date "
            "limit 1",
            (day,),
        )
        return cur.fetchone() is not None


def run_night(
    conn: psycopg.Connection,
    *,
    today: date,
    blobs: BlobStore,
    chart,
    timeseries,
) -> NightReport:
    """Fetch, then score, on one autocommit connection.

    Autocommit because both ingest halves commit per security and `run_scoring`
    commits its run row before the writes it wraps -- all three already depend
    on it.
    """
    securities = active_securities(conn)
    prices = run_prices(
        conn, client=chart, blobs=blobs, today=today, securities=securities
    )
    fundamentals = run_fundamentals(
        conn, client=timeseries, blobs=blobs, today=today, securities=securities
    )

    if prices.status == "failed":
        # `cutoff_offset` filters on `observed_at`, so yesterday's bars are
        # still visible: scoring now would write a complete-looking snapshot
        # from stale data, and tomorrow's crossing diff would read it as
        # "nothing moved". A silent-wrong shape, so the night stops here.
        logger.error(
            "prices wholly failed for %s (%d requested); not scoring",
            today, prices.requested,
        )
        return NightReport(prices, fundamentals, None)

    # Gated on prices alone, deliberately. Scoring reads bars and nothing else
    # this cycle -- no ratio consumes a fundamental fact yet -- so a
    # fundamentals failure must not block a run that does not depend on it.
    # **When the ratios cycle lands, this gate widens to fundamentals.**
    scoring = run_scoring(conn, as_of=today)
    return NightReport(prices, fundamentals, scoring)
```

Add to `src/screener/nightly/__init__.py`:

```python
from screener.nightly.night import NightReport, already_scored, run_night
```

and extend `__all__` to `["BACKOFF_SECONDS", "DEFAULT_ATTEMPTS", "DEFAULT_TRIGGER_HOUR", "NightReport", "NightlyConfig", "already_scored", "is_due", "next_trigger", "run_night"]` — RUF022 order: SCREAMING_CASE, then CamelCase, then snake_case.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_nightly_night.py -n0 -v`
Expected: PASS, 9 passed. If `FakeChart`'s payload does not parse, read `tests/conftest.py`'s `_chart_bytes` helper and match its shape — the price parser's expectations are settled and this test must meet them rather than the reverse.

- [ ] **Step 5: Typecheck and commit**

```bash
.venv/bin/pyright
git add src/screener/nightly tests/test_nightly_night.py
git commit -m "Run one night in order, and stop before scoring stale data"
```

---

### Task 3: `__main__.py` — the loop, the retries and the message

**Files:**
- Create: `src/screener/nightly/__main__.py`
- Test: `tests/test_nightly_main.py`

**Interfaces:**
- Consumes: everything from Tasks 1 and 2.
- Produces:
  - `stopping: threading.Event` — module-level, so a test can replace it.
  - `announce(day: date, reason: str) -> None`
  - `run_tonight(config: NightlyConfig, today: date) -> bool` — true when the night was scored.
  - `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_nightly_main.py`:

```python
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
    # while the container is being torn down.
    assert len(calls) == 1


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightly_main.py -n0 -v`
Expected: FAIL with `ModuleNotFoundError` or `AttributeError` on `run_tonight`.

- [ ] **Step 3: Write the implementation**

Create `src/screener/nightly/__main__.py`:

```python
"""`python -m screener.nightly` -- the scheduler container's entry point.

Runs the pipeline once a night at `NIGHTLY_TRIGGER_HOUR` UTC, catching up a
night a restart interrupted and reporting one it had to give up on.

The wait is a `threading.Event`, not `time.sleep`, for the reason
`screener.reddit` gives: a signal handler cannot interrupt a long sleep, so
SIGTERM would be answered whenever the sleep happened to end and every deploy
would sit through the full SIGKILL timeout. This is PID 1 in the container.
"""

import logging
import signal
import threading
from datetime import date, datetime, timezone
from typing import Any

import psycopg

from screener.blobs import store
from screener.config import settings
from screener.ingest import ChartClient, TimeseriesClient
from screener.nightly.config import BACKOFF_SECONDS, NightlyConfig
from screener.nightly.night import NightReport, already_scored, run_night
from screener.nightly.schedule import is_due, next_trigger
from screener.notify import Alert, ChannelError, DiscordWebhook, NotificationChannel
from screener.scoring import NoBarsVisible, ScoringInProgress
from screener.secrets import SecretsError, load_into_environ

logger = logging.getLogger(__name__)

stopping = threading.Event()


def _channel() -> NotificationChannel:
    """The delivery channel, built late so an unset webhook is not fatal at import."""
    return DiscordWebhook()


def announce(day: date, reason: str) -> None:
    """Say that a night was lost. Once, and only when one was.

    This is not an alert. It reads no `alert_rule`, computes no crossing and
    touches no `emits_alerts` flag, so it neither pre-empts the alerting cycle
    nor breaks the scoring cycle's claim that its runs stay silent.
    """
    try:
        channel = _channel()
    except ChannelError:
        # A missing webhook must not turn a failed night into a crash loop.
        logger.warning(
            "no Discord webhook configured; the lost night for %s is only in this log",
            day,
        )
        return
    try:
        channel.send(
            Alert(
                title=f"Nightly run failed for {day}",
                body=(
                    f"{reason}\n\n"
                    "This snapshot cannot be backfilled. Scoring is forward-only "
                    "and `--as-of` refuses a past date, so this date is a "
                    "permanent hole in the forward log rather than something "
                    "tomorrow repairs."
                ),
                severity="warning",
            )
        )
    except ChannelError as exc:
        logger.error("could not report the lost night for %s: %s", day, exc)


def _attempt(config: NightlyConfig, today: date) -> NightReport:
    """One attempt at tonight, with its own connection and its own clients."""
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        with ChartClient() as chart, TimeseriesClient() as timeseries:
            return run_night(
                conn, today=today, blobs=store(), chart=chart, timeseries=timeseries
            )


def run_tonight(config: NightlyConfig, today: date) -> bool:
    """Try tonight, up to `config.attempts` times. True when it scored."""
    reason = "the night did not run"
    for attempt in range(1, config.attempts + 1):
        try:
            report = _attempt(config, today)
            if report.ok:
                logger.info("night %s complete", today)
                return True
            reason = (
                f"prices wholly failed: 0 of {report.prices.requested} securities "
                "answered, so scoring was skipped rather than score stale bars"
            )
        except ScoringInProgress as exc:
            # Another process holds the lock, which means a run is working.
            # Retrying would be arguing with it.
            logger.info("%s", exc)
            return False
        except NoBarsVisible as exc:
            # Not transient. Three attempts would delay nothing but this line.
            logger.error("%s", exc)
            announce(today, str(exc))
            return False
        except Exception as exc:
            logger.exception("night %s, attempt %d of %d failed", today, attempt, config.attempts)
            reason = f"{type(exc).__name__}: {exc}"

        if attempt < config.attempts and not stopping.is_set():
            # Five minutes, then fifteen. A night is ~3,000 Yahoo requests, so
            # a tight retry loop against a source having a bad evening is how a
            # rate limit nobody has hit becomes one that has been.
            stopping.wait(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
        if stopping.is_set():
            # Being torn down. Do not sit through the remaining backoffs, and
            # do not report a night that was interrupted rather than lost --
            # the next boot's catch-up check will finish it.
            return False

    announce(today, reason)
    return False


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        load_into_environ()
    except SecretsError as exc:
        logger.error("%s", exc)
        return 1

    config = NightlyConfig.from_env()
    if not config.enabled:
        # Not an error, and exiting zero lets `restart: unless-stopped` leave
        # it alone rather than restarting it for ever.
        logger.info("NIGHTLY_ENABLED is false; not scheduling")
        return 0

    def stop(*_: Any) -> None:
        logger.info("stopping after the current step")
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info("scheduling a night at %02d:00 UTC", config.trigger_hour)

    while not stopping.is_set():
        now = datetime.now(timezone.utc)
        # Both halves of the catch-up condition. Without the second a restart
        # would score a night twice; without the first a container starting at
        # 10:00 would run the night thirteen hours early, against a market
        # still open.
        if is_due(now, config.trigger_hour):
            with psycopg.connect(settings().database_url, autocommit=True) as conn:
                owed = not already_scored(conn, now.date())
            if owed:
                run_tonight(config, now.date())

        if stopping.is_set():
            break
        wait = (
            next_trigger(datetime.now(timezone.utc), config.trigger_hour)
            - datetime.now(timezone.utc)
        ).total_seconds()
        logger.info("next night in %.1f hours", wait / 3600)
        stopping.wait(wait)

    logger.info("nightly scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_nightly_main.py -n0 -v`
Expected: PASS, 10 passed.

The interruptible-wait test is deliberately modest: it pins the mechanism rather than driving a real signal through a real wait, because a test that waited would be a test that waits. The full loop's timing is verified by reading, as `screener.reddit`'s is.

- [ ] **Step 5: Whole suite, typecheck, commit**

```bash
.venv/bin/python -m pytest -n 4
.venv/bin/pyright
git add src/screener/nightly/__main__.py tests/test_nightly_main.py
git commit -m "Wait for the hour, catch up after a restart, report a lost night"
```

---

### Task 4: The compose service, and the documentation

**Files:**
- Modify: `deploy/compose.prod.yaml`
- Modify: `tests/test_compose.py` (the `NEEDS_DATABASE` list)
- Modify: `.env.example`, `deploy/local.env.example`
- Modify: `CLAUDE.md`, `PLAN.md`, `docs/infrastructure.md`
- Modify: `docs/specs/2026-09-04-database-schema-design.md` (the erratum §11 owes)
- Modify: `docs/specs/2026-09-07-nightly-scheduling.md` (the status line)

**Interfaces:**
- Consumes: `python -m screener.nightly` (Task 3).
- Produces: nothing code depends on.

- [ ] **Step 1: Add the service**

In `deploy/compose.prod.yaml`, after the `reddit` service, add:

```yaml
  # The nightly pipeline: prices, fundamentals, scoring, once a night.
  #
  # Its own service rather than a cron on the box, because a schedule outside
  # this repository is one more thing that fails invisibly -- `CLAUDE.md`
  # already names two of those, and a third is not worth the simplicity.
  #
  # No healthcheck, and no ports. It spends almost all of its life asleep on a
  # `threading.Event`, so there is nothing to probe that would distinguish
  # "waiting for 23:00" from "wedged", and a check that could not tell them
  # apart would restart a container that was about to do its job.
  nightly:
    image: ghcr.io/d1k03/stock-aggregator:${SCREENER_IMAGE_TAG:-latest}
    restart: unless-stopped
    command: ["python", "-m", "screener.nightly"]

    depends_on:
      # On `api`, not on `postgres`, for the reason `reddit` gives: a healthy
      # database is not a migrated one, and this needs `security`, `metric` and
      # the seeded reference rows to exist before its first night.
      api:
        condition: service_healthy

    env_file:
      - path: .env
        required: false

    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER:-screener}:${POSTGRES_PASSWORD:-screener}@postgres:5432/${POSTGRES_DB:-screener}
      NIGHTLY_TRIGGER_HOUR: ${NIGHTLY_TRIGGER_HOUR:-23}
      NIGHTLY_ENABLED: ${NIGHTLY_ENABLED:-true}
      DISCORD_WEBHOOK_URL: ${DISCORD_WEBHOOK_URL:-}
      INFISICAL_CLIENT_ID: ${INFISICAL_CLIENT_ID:-}
      INFISICAL_CLIENT_SECRET: ${INFISICAL_CLIENT_SECRET:-}
      INFISICAL_PROJECT_ID: ${INFISICAL_PROJECT_ID:-}
      INFISICAL_ENV: ${INFISICAL_ENV:-prod}
```

- [ ] **Step 2: Add it to the compose test's inventory**

`tests/test_compose.py`'s `test_every_service_is_either_given_a_database_or_deliberately_not` asserts the exact service set, so a new service fails it until it is declared. Add `"nightly"` to the `NEEDS_DATABASE` mapping with a one-line reason in that file's existing style — it reads and writes every table the pipeline touches.

Run: `.venv/bin/python -m pytest tests/test_compose.py -n0 -v`
Expected: PASS.

- [ ] **Step 3: Document the settings**

In `.env.example` and `deploy/local.env.example`, add `NIGHTLY_TRIGGER_HOUR` (default 23, UTC, and why the hour matters) and `NIGHTLY_ENABLED` (default true, the incident switch). Follow the commenting style already used for `REDDIT_REFRESH_HOURS`.

- [ ] **Step 4: Write the erratum the spec owes**

In `docs/specs/2026-09-04-database-schema-design.md`, beneath the passage describing `cutoff_offset` as sized for "a live run scoring D at 02:00 the next morning", add an erratum in the style the scoring spec uses: that run is one `screener.scoring.cli` refuses, because at 02:00 on D+1 the date D is in the past. The offset's value and its reasoning are unaffected — what is wrong is the worked example of when the run happens. The scheduler runs at 23:00 on D, scoring D.

- [ ] **Step 5: CLAUDE.md**

Add to the "Infrastructure layout" list, after `screener.ingest`:

```markdown
- `screener.nightly` — the scheduler, in a container of its own. One pass of the pipeline a
  night at 23:00 UTC: prices, fundamentals, then scoring, in that order, because ordering by
  construction beats ordering by clock arithmetic. **23:00 rather than the schema spec's 02:00**
  — `--as-of` refuses a past date, so a run after midnight scores a day whose market has not
  opened rather than the one that just closed. Clock-aligned rather than a bare interval, which
  would drift five hours a month past the hour the design turns on, and it asks on boot whether
  tonight is already scored, so a deploy at 23:03 finishes the night instead of losing that date
  for good. Scoring is skipped when prices wholly failed, because `cutoff_offset` filters on
  `observed_at` and yesterday's bars would otherwise produce a complete-looking snapshot of stale
  data. A partial ingest is a success: `CWEN-A` fails every night, and a channel that cries wolf
  nightly is one nobody reads.
```

Under "Commands", after the scoring entry:

```markdown
- Run the scheduler: `python -m screener.nightly` (the container's command) — waits for 23:00
  UTC, runs prices, fundamentals and scoring in order, and posts to Discord only when a night is
  given up. `NIGHTLY_ENABLED=false` stops it without a redeploy.
```

Update the "Status" paragraph: the pipeline now runs nightly on its own.

- [ ] **Step 6: PLAN.md and docs/infrastructure.md**

In `PLAN.md`, add a "Done" entry for the scheduler, and record the open parameter §10 names: nothing is sent on success, so a container that never starts is indistinguishable from one that succeeds — `/status` reporting the age of the last successful night is the cheap answer, and belongs with whatever cycle next touches that endpoint.

In `docs/infrastructure.md`, add the scheduler beside the other long-running services, and say plainly what it costs when it is not running: scoring is forward-only, so a day the container was down is a permanent hole in the forward log.

- [ ] **Step 7: Update the spec's status line**

Change `Status: agreed, not implemented. Written 2026-09-07.` to record that it is implemented, with the date.

- [ ] **Step 8: Run the suite and commit**

```bash
.venv/bin/python -m pytest -n 4
git add deploy/compose.prod.yaml tests/test_compose.py .env.example deploy/local.env.example CLAUDE.md PLAN.md docs/
git commit -m "Ship the scheduler, and correct the hour the schema spec imagined"
```

---

## Notes for the reviewer

- **No command changed.** `screener.ingest` and `screener.scoring` are untouched; this decides only when they run.
- **The gate is prices-only on purpose**, and widens when the ratios cycle lands. `night.py` says so at the call site.
- **There is no hand-run task.** The deliverable is a container that waits until 23:00 UTC, so the honest verification is watching one night on the box after a deploy — which is the operator's, not this plan's.
