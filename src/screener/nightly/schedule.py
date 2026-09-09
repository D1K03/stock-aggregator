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
