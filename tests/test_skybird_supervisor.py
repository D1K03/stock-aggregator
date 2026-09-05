"""The capture loop, driven by fakes.

CI installs neither `stream` nor `voice`, so there is no yt-dlp and no ffmpeg
here. Everything the loop reaches outside itself is a constructor argument for
exactly that reason — the prober, the capture and the transcriber are three
plain Python objects, and what is under test is the state machine between them.
"""

from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from screener.skybird import store
from screener.skybird.capture import Probe, ProbeFailed, chunk_seconds_of
from screener.skybird.config import MAX_PENDING_CHUNKS, SkybirdConfig
from screener.skybird.platforms import StreamRef
from screener.skybird.supervisor import MAX_FAILURES, Supervisor
from screener.transcribe import Transcript, Utterance

REF = StreamRef(
    platform="youtube",
    external_id="jNQXAC9IVRw",
    channel=None,
    canonical_url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
    embed_url=None,
)

PROBE = Probe(
    manifest_url="https://example.invalid/audio.m3u8",
    video_id="jNQXAC9IVRw",
    title="Market open",
    channel="Somebody",
    is_live=True,
)


def _wav(seconds: float) -> bytes:
    """A payload whose *size* is the duration, which is all the loop reads."""
    return b"\0" * (44 + int(seconds * 16_000 * 2))


class FakeCapture:
    """A capture that produces whatever a test tells it to."""

    def __init__(self, manifest_url: str, *, chunk_seconds: int, work_dir: str) -> None:
        self.manifest_url = manifest_url
        self.chunk_seconds = chunk_seconds
        self.directory = Path(work_dir) / f"fake-{id(self):x}"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.running = True
        self.returncode: int | None = None
        self.error: str | None = None
        self.stopped = False
        self._pending: list[Path] = []
        self._n = 0

    def produce(self, count: int = 1, *, seconds: float = 1.0) -> None:
        for _ in range(count):
            path = self.directory / f"chunk{self._n:06d}.wav"
            path.write_bytes(_wav(seconds))
            self._pending.append(path)
            self._n += 1

    def die(self, error: str = "connection reset") -> None:
        self.running = False
        self.returncode = 1
        self.error = error

    def take(self) -> list[Path]:
        found, self._pending = self._pending, []
        return found

    def stop(self) -> None:
        self.stopped = True
        self.running = False


class Harness:
    """A supervisor wired to fakes, plus the handles to steer them."""

    def __init__(self, tmp_path: Path, **kwargs) -> None:
        self.captures: list[FakeCapture] = []
        self.heard: list[Transcript | None] = []
        self.probe_error: str | None = None
        self.is_live = True

        def prober(url: str) -> Probe:
            if self.probe_error:
                raise ProbeFailed(self.probe_error)
            return replace(PROBE, is_live=self.is_live)

        def factory(manifest_url: str, **opts) -> FakeCapture:
            capture = FakeCapture(manifest_url, **opts)
            self.captures.append(capture)
            return capture

        def transcriber(audio: bytes) -> Transcript | None:
            if self.heard:
                return self.heard.pop(0)
            return Transcript(
                text="hello there",
                seconds=chunk_seconds_of(audio),
                segments=(Utterance(start=0.0, end=1.0, text="hello there"),),
            )

        self.supervisor = Supervisor(
            SkybirdConfig(chunk_seconds=15, work_dir=str(tmp_path), **kwargs),
            prober=prober,
            capture_factory=factory,
            transcriber=transcriber,
        )

    @property
    def capture(self) -> FakeCapture:
        return self.captures[-1]

    def retry_now(self) -> None:
        """Skip the reconnect backoff, which is otherwise seconds of waiting."""
        for run in self.supervisor._running.values():
            run.retry_at = 0.0


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def _request(conn, ref: StreamRef = REF) -> store.Session:
    return store.create(conn, ref, requested_by="ehewes", chunk_seconds=15)


# -- starting ---------------------------------------------------------------


def test_a_requested_capture_starts_and_records_what_the_probe_found(
    fresh_db, harness
):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)

    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "running"
    assert read.title == "Market open"
    assert read.started_at is not None
    # The handle URL carried no embed; the probe named the video, so now it has
    # one. This is the whole reason `embed_url` is nullable.
    assert read.embed_url == "https://www.youtube.com/embed/jNQXAC9IVRw?autoplay=1"


def test_the_session_cap_holds_back_the_second_stream(tmp_path, fresh_db):
    harness = Harness(tmp_path, max_sessions=1)
    first = _request(fresh_db)
    second = _request(fresh_db, StreamRef(
        platform="twitch",
        external_id="somestreamer",
        channel="somestreamer",
        canonical_url="https://www.twitch.tv/somestreamer",
        embed_url=None,
    ))
    harness.supervisor.tick(fresh_db)

    assert len(harness.captures) == 1
    started = store.get(fresh_db, first.id)
    waiting = store.get(fresh_db, second.id)
    assert started is not None and started.state == "running"
    # Still requested rather than refused: it starts when the first one ends.
    assert waiting is not None and waiting.state == "requested"


# -- moving audio -----------------------------------------------------------


def test_chunks_become_segments_with_monotonic_sequence_numbers(fresh_db, harness):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)

    harness.capture.produce(3)
    harness.supervisor.tick(fresh_db)

    found = store.segments(fresh_db, session.id)
    assert [s.seq for s in found] == [1, 2, 3]
    assert [s.chunk_seq for s in found] == [0, 1, 2]
    read = store.get(fresh_db, session.id)
    assert read is not None and read.chunks_ok == 3


def test_offsets_accumulate_so_a_mention_keeps_the_second_it_was_said_at(
    fresh_db, harness
):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    harness.capture.produce(3, seconds=15.0)
    harness.supervisor.tick(fresh_db)

    found = store.segments(fresh_db, session.id)
    assert [s.offset_seconds for s in found] == [0.0, 15.0, 30.0]
    # And the wall clock moves with them, because that is what a later query
    # joins on.
    assert found[0].captured_at < found[1].captured_at < found[2].captured_at


def test_the_audio_is_gone_the_moment_it_has_been_read(fresh_db, harness):
    _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    harness.capture.produce(2)
    harness.supervisor.tick(fresh_db)

    assert list(harness.capture.directory.iterdir()) == []


def test_a_chunk_of_silence_advances_the_clock_without_writing_a_line(
    fresh_db, harness
):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)

    harness.heard = [Transcript(text="", seconds=15.0, segments=())]
    harness.capture.produce(1, seconds=15.0)
    harness.supervisor.tick(fresh_db)
    harness.capture.produce(1, seconds=15.0)
    harness.supervisor.tick(fresh_db)

    found = store.segments(fresh_db, session.id)
    assert len(found) == 1
    # The silent chunk still happened, so the line after it is not at zero.
    assert found[0].offset_seconds == 15.0


def test_a_transcriber_that_does_not_answer_is_counted_and_the_run_goes_on(
    fresh_db, harness
):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)

    harness.heard = [None]
    harness.capture.produce(2)
    harness.supervisor.tick(fresh_db)

    read = store.get(fresh_db, session.id)
    assert read is not None
    assert (read.chunks_failed, read.chunks_ok) == (1, 1)
    assert read.state == "running"
    assert read.last_error is not None


def test_a_transcript_without_timings_still_lands_as_one_line(fresh_db, harness):
    # What an older transcribe service sends. Coarser, and still correct.
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    harness.heard = [Transcript(text="all one line", seconds=15.0, segments=())]
    harness.capture.produce(1, seconds=15.0)
    harness.supervisor.tick(fresh_db)

    found = store.segments(fresh_db, session.id)
    assert len(found) == 1
    assert found[0].duration_seconds == pytest.approx(15.0)


def test_a_backlog_is_bounded_by_dropping_the_oldest(fresh_db, harness):
    """Unbounded queuing would fill the tmpfs to hide a transcriber that is
    behind. The gap is left visible in the offsets instead."""
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)

    harness.capture.produce(MAX_PENDING_CHUNKS + 3, seconds=15.0)
    harness.supervisor.tick(fresh_db)

    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.chunks_dropped == 3
    assert read.chunks_ok == MAX_PENDING_CHUNKS
    # The dropped audio still advanced the clock, so the first line kept is at
    # the second it was actually said, not at zero.
    found = store.segments(fresh_db, session.id)
    assert found[0].offset_seconds == pytest.approx(45.0)


# -- reconnecting and ending ------------------------------------------------


def test_ffmpeg_exiting_part_way_through_reconnects_rather_than_failing(
    fresh_db, harness
):
    # A manifest URL expires after a few hours on both platforms, so this is
    # expected rather than exceptional.
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    harness.capture.produce(1, seconds=15.0)
    harness.supervisor.tick(fresh_db)

    first = harness.capture
    first.die()
    harness.supervisor.tick(fresh_db)   # notices, tears down, backs off
    harness.retry_now()
    harness.supervisor.tick(fresh_db)   # probes again

    assert len(harness.captures) == 2
    assert first.stopped
    read = store.get(fresh_db, session.id)
    assert read is not None and read.state == "running"

    # And the transcript carries on counting rather than starting again.
    harness.capture.produce(1, seconds=15.0)
    harness.supervisor.tick(fresh_db)
    assert [s.seq for s in store.segments(fresh_db, session.id)] == [1, 2]


def test_a_stream_that_has_ended_stops_rather_than_fails(fresh_db, harness):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    harness.capture.produce(1, seconds=15.0)
    harness.supervisor.tick(fresh_db)

    harness.is_live = False
    harness.capture.die()
    harness.supervisor.tick(fresh_db)
    harness.retry_now()
    harness.supervisor.tick(fresh_db)

    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "stopped"
    assert read.stop_reason == "stream_ended"


def test_a_stream_that_cannot_be_reached_is_given_up_on_eventually(
    fresh_db, harness
):
    session = _request(fresh_db)
    harness.probe_error = "no such broadcast"

    for _ in range(MAX_FAILURES):
        harness.retry_now()
        harness.supervisor.tick(fresh_db)

    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "failed"
    assert read.last_error is not None and "no such broadcast" in read.last_error


def test_asking_a_capture_to_stop_tears_down_the_ffmpeg(fresh_db, harness):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    store.request_stop(fresh_db, session.id)

    harness.supervisor.tick(fresh_db)

    assert harness.capture.stopped
    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "stopped"
    assert read.stop_reason == "asked to stop"


def test_deleting_a_running_capture_stops_it_on_the_next_poll(fresh_db, harness):
    # Which is why the API does not make you stop it first.
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    store.delete(fresh_db, session.id)

    harness.supervisor.tick(fresh_db)

    assert harness.capture.stopped


def test_a_row_left_running_with_nothing_behind_it_is_not_adopted(fresh_db, harness):
    # `reconcile` clears these at boot, so reaching this state means the row
    # changed underneath us. Adopting it blind would report a capture that is
    # not happening.
    session = _request(fresh_db)
    store.start(fresh_db, session.id)
    store.describe(fresh_db, session.id, running=True)

    harness.supervisor.tick(fresh_db)

    read = store.get(fresh_db, session.id)
    assert read is not None
    assert read.state == "failed"
    assert len(harness.captures) == 0


def test_shutdown_stops_every_capture_and_settles_every_row(db_url, fresh_db, harness):
    """Nothing is left saying 'running' by a container that exited on purpose.

    `shutdown` closes the connection it was holding, so the check that follows
    opens its own.
    """
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    harness.supervisor._conn = fresh_db

    harness.supervisor.shutdown()
    assert harness.capture.stopped

    with psycopg.connect(db_url, autocommit=True) as conn:
        read = store.get(conn, session.id)
    assert read is not None and not read.live


# -- pausing and resuming ---------------------------------------------------


def test_pausing_tears_the_ffmpeg_down_and_leaves_the_row_held(fresh_db, harness):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    store.pause(fresh_db, session.id)

    harness.supervisor.tick(fresh_db)

    assert harness.capture.stopped
    read = store.get(fresh_db, session.id)
    assert read is not None
    # Held, not failed. The branch that fails a live row with nothing behind it
    # would otherwise catch this on the very next poll.
    assert read.state == "paused"


def test_a_held_capture_is_not_failed_by_later_polls(fresh_db, harness):
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    store.pause(fresh_db, session.id)
    for _ in range(3):
        harness.supervisor.tick(fresh_db)

    read = store.get(fresh_db, session.id)
    assert read is not None and read.state == "paused"


def test_resuming_carries_on_the_transcript_rather_than_starting_it_again(
    fresh_db, harness
):
    """The reason the capture clock is in the database at all.

    A resumed capture is a second ffmpeg over the same session, so without a
    persisted offset it would lay a second timeline over the first — every line
    after the pause claiming to be from the opening minute.
    """
    session = _request(fresh_db)
    harness.supervisor.tick(fresh_db)
    harness.capture.produce(2, seconds=15.0)
    harness.supervisor.tick(fresh_db)

    store.pause(fresh_db, session.id)
    harness.supervisor.tick(fresh_db)
    store.resume(fresh_db, session.id)
    harness.supervisor.tick(fresh_db)

    # A second capture, and the session picked up where it left off.
    assert len(harness.captures) == 2
    harness.capture.produce(1, seconds=15.0)
    harness.supervisor.tick(fresh_db)

    found = store.segments(fresh_db, session.id)
    assert [s.seq for s in found] == [1, 2, 3]
    assert [s.offset_seconds for s in found] == [0.0, 15.0, 30.0]


def test_a_held_capture_frees_its_slot_for_another_stream(tmp_path, fresh_db):
    # Pause exists so you can put something else on without giving up the first
    # stream, so this is the whole point of it.
    harness = Harness(tmp_path, max_sessions=1)
    first = _request(fresh_db)
    second = _request(fresh_db, StreamRef(
        platform="twitch",
        external_id="somestreamer",
        channel="somestreamer",
        canonical_url="https://www.twitch.tv/somestreamer",
        embed_url=None,
    ))
    harness.supervisor.tick(fresh_db)
    assert len(harness.captures) == 1

    store.pause(fresh_db, first.id)
    harness.supervisor.tick(fresh_db)   # tears the first down
    harness.supervisor.tick(fresh_db)   # starts the second

    assert len(harness.captures) == 2
    held = store.get(fresh_db, first.id)
    started = store.get(fresh_db, second.id)
    assert held is not None and held.state == "paused"
    assert started is not None and started.state == "running"
