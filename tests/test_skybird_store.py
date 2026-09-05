"""The tables, and the invariants they carry rather than the code.

Constraints are asserted by catching the specific psycopg error class, so a
test cannot pass because something else went wrong first.
"""

from datetime import UTC, datetime

import psycopg
import pytest

from screener.skybird import store
from screener.skybird.platforms import StreamRef

REF = StreamRef(
    platform="youtube",
    external_id="jNQXAC9IVRw",
    channel="@somebody",
    canonical_url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
    embed_url="https://www.youtube.com/embed/jNQXAC9IVRw?autoplay=1",
)


def _create(conn, ref: StreamRef = REF, *, by: str = "ehewes") -> store.Session:
    return store.create(conn, ref, requested_by=by, chunk_seconds=15)


def _segments(count: int, *, first: int = 1) -> list:
    now = datetime.now(UTC)
    return [
        (first + n, 0, now, float(n), 1.0, f"line {first + n}") for n in range(count)
    ]


# -- one live capture per stream -------------------------------------------


def test_a_new_capture_starts_out_requested_and_owns_no_process_yet(fresh_db):
    session = _create(fresh_db)
    assert session.state == "requested"
    assert session.live
    assert session.started_at is None
    assert session.chunks_ok == 0


def test_the_same_stream_cannot_be_captured_twice_at_once(fresh_db):
    # Pasting the same URL twice is the ordinary mistake, and two ffmpegs on one
    # manifest is fetched twice, transcribed twice and stored twice.
    _create(fresh_db)
    with pytest.raises(store.AlreadyLive):
        _create(fresh_db)


def test_the_same_stream_can_be_captured_again_once_the_first_has_ended(fresh_db):
    # The index is partial on the live states, deliberately: history must not
    # stop you starting again tomorrow.
    first = _create(fresh_db)
    store.finish(fresh_db, first.id, state="stopped", reason="asked to stop")
    second = _create(fresh_db)
    assert second.id != first.id


def test_two_different_streams_are_both_allowed(fresh_db):
    _create(fresh_db)
    other = StreamRef(
        platform="twitch",
        external_id="somestreamer",
        channel="somestreamer",
        canonical_url="https://www.twitch.tv/somestreamer",
        embed_url="https://player.twitch.tv/?channel=somestreamer",
    )
    assert _create(fresh_db, other).platform == "twitch"


def test_a_platform_with_no_adapter_row_is_refused_by_the_database(fresh_db):
    # `skybird.platform` is a table rather than a check constraint so a new
    # adapter is an insert — which only works if the foreign key is real.
    unknown = StreamRef(
        platform="myspace",
        external_id="x",
        channel=None,
        canonical_url="https://myspace.com/x",
        embed_url=None,
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _create(fresh_db, unknown)


def test_a_chunk_length_of_zero_is_refused(fresh_db):
    with pytest.raises(psycopg.errors.CheckViolation):
        store.create(fresh_db, REF, requested_by="ehewes", chunk_seconds=0)


def test_an_unknown_state_cannot_be_written(fresh_db):
    session = _create(fresh_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            "update skybird.stream_session set state = 'napping' where id = %s",
            (session.id,),
        )


# -- the transcript --------------------------------------------------------


def test_segments_come_back_after_a_sequence_number_in_order(fresh_db):
    session = _create(fresh_db)
    store.append_segments(fresh_db, session.id, _segments(5))
    assert [s.seq for s in store.segments(fresh_db, session.id, after=2)] == [3, 4, 5]


def test_silence_is_not_stored_as_an_empty_line(fresh_db):
    # Whisper runs with a voice filter, so quiet comes back as nothing at all.
    # A row saying nothing was said is not worth the space or the scroll.
    session = _create(fresh_db)
    now = datetime.now(UTC)
    written = store.append_segments(
        fresh_db,
        session.id,
        [(1, 0, now, 0.0, 1.0, "  "), (2, 0, now, 1.0, 1.0, "something")],
    )
    assert written == 1
    assert [s.text for s in store.segments(fresh_db, session.id)] == ["something"]


def test_the_next_sequence_continues_rather_than_colliding(fresh_db):
    # What a reconnect reads: ffmpeg restarts, the session does not, and the
    # transcript has to carry on counting.
    session = _create(fresh_db)
    store.append_segments(fresh_db, session.id, _segments(3))
    assert store.next_seq(fresh_db, session.id) == 4
    store.append_segments(fresh_db, session.id, _segments(2, first=4))
    assert [s.seq for s in store.segments(fresh_db, session.id)] == [1, 2, 3, 4, 5]


def test_a_negative_offset_is_refused(fresh_db):
    session = _create(fresh_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        store.append_segments(
            fresh_db, session.id, [(1, 0, datetime.now(UTC), -1.0, 1.0, "backwards")]
        )


def test_deleting_a_session_takes_its_transcript_with_it(fresh_db):
    # The whole retention mechanism: nothing here expires on its own, so the
    # cascade is what "delete the data" actually means.
    session = _create(fresh_db)
    store.append_segments(fresh_db, session.id, _segments(4))
    assert store.delete(fresh_db, session.id) is True

    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from skybird.transcript_segment")
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from skybird.stream_session")
        assert cur.fetchone()[0] == 0


def test_deleting_something_that_is_not_there_says_so(fresh_db):
    assert store.delete(fresh_db, 999) is False


def test_the_session_carries_its_own_segment_count(fresh_db):
    session = _create(fresh_db)
    store.append_segments(fresh_db, session.id, _segments(7))
    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.segment_count == 7
    assert read.last_segment_at is not None


# -- state ------------------------------------------------------------------


def test_a_requested_capture_stops_outright_rather_than_waiting_to_be_torn_down(
    fresh_db,
):
    # Nothing is running behind it, so waiting for a supervisor to notice would
    # leave the row live forever.
    session = _create(fresh_db)
    assert store.request_stop(fresh_db, session.id) is True
    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "stopped"
    assert read.stopped_at is not None


def test_a_running_capture_is_asked_to_stop_rather_than_stopped(fresh_db):
    session = _create(fresh_db)
    store.start(fresh_db, session.id)
    store.describe(fresh_db, session.id, title="A stream", running=True)
    assert store.request_stop(fresh_db, session.id) is True
    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "stopping"


def test_stopping_something_already_finished_changes_nothing(fresh_db):
    session = _create(fresh_db)
    store.finish(fresh_db, session.id, state="failed", reason="gave up")
    assert store.request_stop(fresh_db, session.id) is False


def test_only_one_supervisor_can_claim_a_requested_capture(fresh_db):
    session = _create(fresh_db)
    assert store.start(fresh_db, session.id) is True
    assert store.start(fresh_db, session.id) is False


def test_a_capture_left_running_by_a_dead_supervisor_reconciles_to_failed(fresh_db):
    """The reason the state lives in the database at all.

    Nobody asked for this to end, so it is failed rather than stopped: a row
    saying 'stopped' would read as a clean finish nobody ordered.
    """
    session = _create(fresh_db)
    store.start(fresh_db, session.id)
    store.describe(fresh_db, session.id, running=True)

    assert store.reconcile(fresh_db) == 1

    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "failed"
    assert read.stop_reason == "supervisor_restart"
    assert read.stopped_at is not None


def test_reconcile_leaves_a_requested_capture_alone(fresh_db):
    # It has no process behind it, so a restarting supervisor should pick it up
    # rather than fail it.
    session = _create(fresh_db)
    assert store.reconcile(fresh_db) == 0
    read = store.get(fresh_db, session.id)
    assert read is not None and read.state == "requested"


def test_started_at_survives_a_reconnect(fresh_db):
    # It anchors every offset in the transcript, so a second probe must not
    # move it.
    session = _create(fresh_db)
    store.start(fresh_db, session.id)
    store.describe(fresh_db, session.id, running=True)
    first = store.get(fresh_db, session.id)
    store.describe(fresh_db, session.id, running=True)
    second = store.get(fresh_db, session.id)
    assert first is not None and second is not None
    assert first.started_at == second.started_at


def test_a_probe_without_a_title_does_not_erase_the_one_we_have(fresh_db):
    session = _create(fresh_db)
    store.describe(fresh_db, session.id, title="Market open", running=True)
    store.describe(fresh_db, session.id, title=None, running=True)
    read = store.get(fresh_db, session.id)
    assert read is not None and read.title == "Market open"


def test_counters_accumulate_and_the_last_error_is_kept(fresh_db):
    session = _create(fresh_db)
    store.count_chunk(fresh_db, session.id, ok=1)
    store.count_chunk(fresh_db, session.id, ok=1)
    store.count_chunk(fresh_db, session.id, failed=1, error="the transcriber went away")
    store.count_chunk(fresh_db, session.id, dropped=2)
    read = store.get(fresh_db, session.id)
    assert read is not None
    assert (read.chunks_ok, read.chunks_failed, read.chunks_dropped) == (2, 1, 2)
    assert read.last_error == "the transcriber went away"


def test_finish_refuses_a_state_that_is_not_an_ending(fresh_db):
    session = _create(fresh_db)
    with pytest.raises(ValueError):
        store.finish(fresh_db, session.id, state="running", reason="no")


# -- listing ----------------------------------------------------------------


def test_live_captures_are_listed_above_history(fresh_db):
    # A running capture is the thing you opened the page for, and it is not
    # necessarily the most recently requested one.
    old = _create(fresh_db)
    store.finish(fresh_db, old.id, state="stopped", reason="asked to stop")
    live = _create(fresh_db, StreamRef(
        platform="twitch",
        external_id="somestreamer",
        channel="somestreamer",
        canonical_url="https://www.twitch.tv/somestreamer",
        embed_url=None,
    ))
    assert [s.id for s in store.listing(fresh_db)] == [live.id, old.id]


def test_the_active_count_is_what_the_session_cap_reads(fresh_db):
    assert store.active_count(fresh_db) == 0
    session = _create(fresh_db)
    assert store.active_count(fresh_db) == 1
    store.finish(fresh_db, session.id, state="stopped", reason="asked to stop")
    assert store.active_count(fresh_db) == 0


# -- pausing ----------------------------------------------------------------


def test_a_paused_capture_is_still_live_but_not_active(fresh_db):
    """The one place the two state lists have to differ.

    Live: it holds its stream and its place in the list. Not active: no ffmpeg
    and no share of the transcriber, so counting it against the cap would refuse
    a new stream on behalf of one that is not running.
    """
    session = _create(fresh_db)
    store.start(fresh_db, session.id)
    store.describe(fresh_db, session.id, running=True)
    assert store.pause(fresh_db, session.id) is True

    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "paused"
    assert read.live is True and read.paused is True
    assert store.active_count(fresh_db) == 0


def test_a_paused_capture_still_holds_its_stream(fresh_db):
    # Otherwise pausing would be an invitation for the same stream to be started
    # again beside itself.
    session = _create(fresh_db)
    store.start(fresh_db, session.id)
    store.pause(fresh_db, session.id)
    with pytest.raises(store.AlreadyLive):
        _create(fresh_db)


def test_a_paused_capture_survives_a_supervisor_restart(fresh_db):
    # It has no process behind it, so there is nothing for `reconcile` to
    # settle — failing it would throw away a hold nobody released.
    session = _create(fresh_db)
    store.start(fresh_db, session.id)
    store.pause(fresh_db, session.id)
    assert store.reconcile(fresh_db) == 0
    read = store.get(fresh_db, session.id)
    assert read is not None and read.state == "paused"


def test_resuming_goes_back_through_the_queue_rather_than_straight_to_running(
    fresh_db,
):
    # The manifest it had has expired, and the session cap has to apply again,
    # so it takes exactly the path a new capture takes.
    session = _create(fresh_db)
    store.start(fresh_db, session.id)
    store.pause(fresh_db, session.id)
    assert store.resume(fresh_db, session.id) is True
    read = store.get(fresh_db, session.id)
    assert read is not None and read.state == "requested"


def test_the_capture_clock_outlives_the_process_that_kept_it(fresh_db):
    """What makes a resumed transcript carry on rather than start again.

    Every chunk moves it, including one that failed and one that was dropped:
    the audio happened either way, and an offset that skipped it would put every
    line after the gap at the wrong second.
    """
    session = _create(fresh_db)
    store.count_chunk(fresh_db, session.id, ok=1, seconds=15.0)
    store.count_chunk(fresh_db, session.id, failed=1, seconds=15.0)
    store.count_chunk(fresh_db, session.id, dropped=1, seconds=15.0)
    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.captured_seconds == 45.0


def test_pausing_something_that_is_not_running_changes_nothing(fresh_db):
    session = _create(fresh_db)
    store.finish(fresh_db, session.id, state="stopped", reason="asked to stop")
    assert store.pause(fresh_db, session.id) is False


def test_resuming_something_that_is_not_paused_changes_nothing(fresh_db):
    session = _create(fresh_db)
    assert store.resume(fresh_db, session.id) is False


def test_a_paused_capture_stops_outright_rather_than_waiting_to_be_torn_down(
    fresh_db,
):
    # Nothing is running behind it, so routing it through 'stopping' would ask a
    # supervisor to tear down an ffmpeg that was never there.
    session = _create(fresh_db)
    store.start(fresh_db, session.id)
    store.pause(fresh_db, session.id)
    assert store.request_stop(fresh_db, session.id) is True
    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "stopped"
    assert read.stopped_at is not None
