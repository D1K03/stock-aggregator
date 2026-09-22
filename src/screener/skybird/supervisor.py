"""The capture loop: one process, one advisory lock, N streams.

What it does every couple of seconds is ask the database what it should be
doing. That is the whole control plane — the status service writes a row and
this reads it — so there is no internal HTTP surface between the two containers
to authenticate, and a capture is durable: the row outlives the process, and
`store.reconcile` puts anything a dead one left behind back in the queue.

**A restart costs a seam, not the capture.** Every deploy recreates this
container. What was captured is in the database — the offset, the sequence
numbers, and the capture clock where the audio stops — so the next supervisor
starts the stream as far behind live as the gap, which the platform's own
playlist still holds, and reads the backlog faster than real time until it is
back at the live edge. The same path serves every reconnect, so an ffmpeg that
exits when its manifest expires stops costing the seconds it took to come back.

**One supervisor captures at a time**, held by a Postgres advisory lock, the way
`screener.boot` holds one for migrations. A second copy stands by and retries
rather than exiting, because the overlap during a rolling deploy is normal and a
container that exits on it is a crash loop with a healthy cause.

Every outside edge is a constructor argument — the prober, the capture, the
transcriber — for the reason `build_server` takes a `transcriber`: CI installs
neither `stream` nor `voice`, so the loop has to be exercisable with three plain
Python functions.
"""

import logging
import math
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import psycopg

from screener.config import settings
from screener.skybird import store
from screener.skybird.capture import (
    Capture,
    Probe,
    ProbeFailed,
    Recorder,
    Rewind,
    Window,
    chunk_seconds_of,
    probe,
    rewind,
)
from screener.skybird.config import (
    CHUNKS_PER_TICK,
    MAX_PENDING_CHUNKS,
    MIN_AUDIBLE_SECONDS,
    MIN_REPORTED_LOSS_SECONDS,
    POLL_SECONDS,
    TRANSCRIBER_PATIENCE_SECONDS,
    SkybirdConfig,
)
from screener.skybird.platforms import find as find_platform
from screener.transcribe import Transcript, transcribe

logger = logging.getLogger(__name__)

__all__ = ["Supervisor", "run"]

# Distinct from the migration lock in `screener.boot`, and numbered after the
# migration that created these tables so the two are traceable to each other.
SUPERVISOR_LOCK_ID: Final = 8_119_012

# Consecutive failures to get audio flowing — a probe that fails, an ffmpeg that
# will not start or exits before it has produced a chunk — before a capture is
# given up on. A live stream that has genuinely ended fails every one of these
# in about a minute.
MAX_FAILURES: Final = 5

# Backoff between attempts, doubling, capped. A stream that has just gone offline
# should not be probed once every two seconds for an hour.
RETRY_BASE_SECONDS: Final = 2.0
MAX_RETRY_SECONDS: Final = 60.0

Prober = Callable[[str], Probe]
CaptureFactory = Callable[..., Recorder]
Transcriber = Callable[[bytes], Transcript | None]


@dataclass
class Running:
    """One capture this process owns, and where its transcript has got to."""

    session_id: int
    source_url: str
    platform: str
    chunk_seconds: int
    capture: Recorder | None = None
    next_seq: int = 1
    chunk_seq: int = 0
    # Seconds of audio captured for this session, across reconnects. This is
    # what `offset_seconds` counts, and it is why a session started once keeps
    # one timeline even if ffmpeg is restarted six times.
    offset: float = 0.0
    # Wall clock corresponding to `offset == 0`. A reconnect keeps it when the
    # rewind recovers the whole gap, and moves it on by whatever the stream no
    # longer held, so an outage never pushes the transcript permanently behind
    # real time. Within a continuous run the two agree exactly.
    wall_origin: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Where the stored audio stops on that clock: `wall_origin + offset` once
    # anything has been heard, and read back from the row after a restart. None
    # when there is nothing to rewind for — a new capture, or a resumed pause.
    captured_until: datetime | None = None
    # Finished chunks not yet heard, oldest first. Held on the tmpfs rather than
    # read at once, so a transcriber that is restarting can be waited out and a
    # rewound backlog heard a few at a time.
    held: deque[Path] = field(default_factory=deque)
    # Extra backlog tolerated while a rewind is being read, or the chunks it
    # exists to recover would be the first ones shed for being old.
    allowance: int = 0
    # When the transcriber first stopped answering, on the monotonic clock.
    stalled_since: float | None = None
    # Whether this ffmpeg was started behind live, and whether it has produced
    # anything yet. One that rewound and died without a chunk is not trusted to
    # rewind again: the next connect starts at the live edge, as they used to.
    rewound: bool = False
    produced: bool = False
    no_rewind: bool = False
    failures: int = 0
    retry_at: float = 0.0


class Supervisor:
    def __init__(
        self,
        config: SkybirdConfig | None = None,
        *,
        prober: Prober | None = None,
        capture_factory: CaptureFactory | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        self.config = config or SkybirdConfig.from_env()
        self._probe = prober or probe
        self._capture = capture_factory or Capture
        self._client: httpx.Client | None = None
        self._transcribe = transcriber or self._transcribe_over_http
        self._running: dict[int, Running] = {}
        self._conn: psycopg.Connection | None = None
        self._reconciled = False
        self._standing_by = False
        self._stopping = threading.Event()

    # -- the loop ---------------------------------------------------------

    def serve(self) -> None:
        """Run until SIGTERM or SIGINT, then stop every capture cleanly."""
        def stop(*_: Any) -> None:
            self._stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        logger.info(
            "skybird supervising: %ds chunks, %d session(s) at once, "
            "rewinding up to %ds after a restart",
            self.config.chunk_seconds,
            self.config.max_sessions,
            self.config.max_rewind_seconds,
        )
        try:
            while not self._stopping.is_set():
                try:
                    conn = self._connection()
                    if conn is not None:
                        self.tick(conn)
                except Exception as exc:
                    # A supervisor that dies on a database blip takes every
                    # running capture with it. Log, drop the connection, and
                    # come back on the next tick.
                    logger.warning("tick failed: %s: %s", type(exc).__name__, exc)
                    self._drop_connection()
                self._stopping.wait(POLL_SECONDS)
        finally:
            self.shutdown()

    def tick(self, conn: psycopg.Connection) -> None:
        """One pass: settle the roster, then move audio."""
        live = {session.id: session for session in store.pending(conn)}

        # A session that has gone from the live set was deleted or finished
        # under us. Stop the ffmpeg before anything else, or it captures a
        # stream nobody is asking for.
        for session_id in set(self._running) - set(live):
            self._release(session_id)

        for session in live.values():
            if session.state == "stopping":
                self._release(session.id)
                store.finish(
                    conn, session.id, state="stopped", reason="asked to stop"
                )
            elif session.state == "paused":
                # The ffmpeg goes and the row stays. Before the branch below,
                # which would otherwise read a held capture as one that lost its
                # process and fail it on the next poll.
                self._release(session.id)
            elif session.state == "requested":
                # Paused and resumed between two polls: the row is back in the
                # queue but its old ffmpeg is still here, and starting a second
                # one beside it would leave the first writing into the tmpfs
                # with nothing ever reading it.
                self._release(session.id)
                # A probe can take seconds, and a SIGTERM allows ten.
                if self._stopping.is_set():
                    continue
                if len(self._running) < self.config.max_sessions:
                    self._begin(conn, session)
            elif session.id in self._running:
                self._advance(conn, self._running[session.id])
            else:
                # 'starting' or 'running' with nothing behind it. `reconcile`
                # clears these at boot, so reaching here means the row changed
                # under us; treat it as ended rather than adopting it blind.
                store.finish(
                    conn,
                    session.id,
                    state="failed",
                    reason="supervisor_restart",
                    error="no capture was running for this session",
                )

    # -- one capture ------------------------------------------------------

    def _begin(self, conn: psycopg.Connection, session: store.Session) -> None:
        if not store.start(conn, session.id):
            return
        run = Running(
            session_id=session.id,
            source_url=session.source_url,
            platform=session.platform,
            chunk_seconds=session.chunk_seconds,
            next_seq=store.next_seq(conn, session.id),
            # Both read back from the database rather than started at zero,
            # which is what lets a resumed capture carry on counting instead of
            # laying a second timeline over the first.
            offset=session.captured_seconds,
            # And where that audio stops, which is what a restart rewinds to.
            captured_until=session.captured_until,
        )
        self._running[session.id] = run
        self._connect_stream(conn, run)

    def _connect_stream(self, conn: psycopg.Connection, run: Running) -> None:
        """Probe, then start ffmpeg. Counts a failure rather than raising."""
        try:
            found = self._probe(run.source_url)
        except ProbeFailed as exc:
            self._stumble(conn, run, f"could not reach the stream: {exc}")
            return

        if not found.is_live and run.offset > 0:
            # It was live and now is not: an ordinary ending, not a fault.
            self._release(run.session_id)
            store.finish(
                conn, run.session_id, state="stopped", reason="stream_ended"
            )
            return

        now = datetime.now(UTC)
        if run.captured_until is None:
            # Nothing to recover: start at the live edge, as a capture always
            # did, and let the clock begin here.
            plan = Rewind(live_start_index=None, skipped=0.0)
            anchor = now
            recovered = 0.0
        else:
            gap = (now - run.captured_until).total_seconds()
            window = found.window if found.is_live and not run.no_rewind else Window()
            plan = rewind(gap, window, limit=self.config.max_rewind_seconds)
            # Where the first new audio falls on the clock. The whole gap
            # recovered keeps the old timeline exactly; none of it recovered
            # is `now`, which is where every reconnect used to put it.
            anchor = run.captured_until + timedelta(seconds=plan.skipped)
            recovered = gap - plan.skipped

        try:
            run.capture = self._capture(
                found.manifest_url,
                chunk_seconds=run.chunk_seconds,
                work_dir=self.config.work_dir,
                live_start_index=plan.live_start_index,
            )
        except Exception as exc:
            self._stumble(conn, run, f"could not start ffmpeg: {exc}")
            return

        # `failures` is not reset here but on the first chunk: an ffmpeg that
        # starts and exits at once, every time, is not a stream that is working.
        run.rewound = plan.live_start_index is not None
        run.produced = False
        run.wall_origin = anchor - timedelta(seconds=run.offset)
        run.allowance = (
            math.ceil(recovered / run.chunk_seconds) + 1 if recovered > 0 else 0
        )
        if plan.live_start_index is not None:
            logger.info(
                "session %d starts at live_start_index %d: %.0fs recovered, "
                "%.0fs skipped",
                run.session_id,
                plan.live_start_index,
                recovered,
                plan.skipped,
            )
        if run.captured_until is not None and plan.skipped >= max(
            found.window.segment_seconds, MIN_REPORTED_LOSS_SECONDS
        ):
            # More than the one segment a rewind is good to: the playlist no
            # longer held it, or the platform keeps no playlist to rewind into.
            # Said on the row, because the transcript will not say it — its
            # offsets carry straight on across the hole.
            store.count_chunk(
                conn,
                run.session_id,
                error=f"missed {plan.skipped:.0f}s of the stream between connections",
            )
        store.describe(
            conn,
            run.session_id,
            title=found.title,
            channel=found.channel,
            embed_url=self._embed(run.platform, found),
            running=True,
        )
        logger.info(
            "capturing session %d: %s", run.session_id, found.title or run.source_url
        )

    def _embed(self, platform_name: str, found: Probe) -> str | None:
        """A player for what the probe actually resolved.

        Only ever fills a gap: `store.describe` coalesces, so a channel URL that
        already had a working embed keeps it, and a YouTube handle — which names
        no video until now — gets one.
        """
        if not found.video_id:
            return None
        platform = find_platform(platform_name)
        if platform is None:
            return None
        return platform.embed_video(found.video_id, self.config.embed_parents)

    def _advance(self, conn: psycopg.Connection, run: Running) -> None:
        """Move whatever audio is ready, and reconnect if ffmpeg has gone."""
        if run.capture is None:
            if time.monotonic() >= run.retry_at:
                self._connect_stream(conn, run)
            return

        self._drain(conn, run)

        # Not torn down while chunks are still waiting to be heard: when a
        # stream ends, the last of them are the end of it.
        if not run.capture.running and not run.held:
            code = run.capture.returncode
            error = run.capture.error
            run.capture.stop()
            run.capture = None
            if run.rewound and not run.produced:
                logger.warning(
                    "session %d's rewound ffmpeg produced nothing; "
                    "reconnecting at the live edge",
                    run.session_id,
                )
                run.no_rewind = True
            # A manifest URL expires after a few hours, so an exit part way
            # through a broadcast is expected rather than exceptional. Probe
            # again: that is also what notices the stream has ended.
            logger.info(
                "ffmpeg exited (%s) for session %d, reconnecting", code, run.session_id
            )
            self._stumble(conn, run, error, quiet=True)

    def _drain(self, conn: psycopg.Connection, run: Running) -> None:
        """Hear the oldest chunks, a few at a time, in the order they were cut.

        A few rather than all: a rewind hands over dozens at once, and hearing
        them in one pass would hold the other capture and any stop request
        behind minutes of transcription. The rest wait on the tmpfs.
        """
        assert run.capture is not None
        finished = run.capture.take()
        if finished:
            # Audio is flowing, which is the only evidence that a connect worked.
            run.failures = 0
            run.produced = True
            run.held.extend(finished)
        self._shed(conn, run)
        for _ in range(CHUNKS_PER_TICK):
            if not run.held or self._stopping.is_set():
                return
            if not self._hear(conn, run, run.held[0]):
                return
            run.held.popleft()

    def _shed(self, conn: psycopg.Connection, run: Running) -> None:
        """Drop the oldest chunks past the bound, and count them.

        The transcriber is not keeping up. Dropping bounds the tmpfs and leaves
        a gap the offsets make visible, which is better than filling quietly to
        hide it. A rewind raises the bound by what it recovered, or the backlog
        it exists to hear would be the first thing thrown away.
        """
        excess = len(run.held) - (MAX_PENDING_CHUNKS + run.allowance)
        if excess <= 0:
            return
        lost = 0.0
        for _ in range(excess):
            lost += self._discard(run.held.popleft())
        run.offset += lost
        run.captured_until = run.wall_origin + timedelta(seconds=run.offset)
        store.count_chunk(
            conn,
            run.session_id,
            dropped=excess,
            seconds=lost,
            until=run.captured_until,
            error=f"dropped {excess} chunk(s): the transcriber is behind",
        )

    def _hear(self, conn: psycopg.Connection, run: Running, path: Path) -> bool:
        """Transcribe one chunk and account for it. False to keep it for later.

        The file stays until the chunk is accounted for, so a transcriber that
        does not answer costs a wait rather than the audio: a deploy recreates
        it beside this container, and a chunk that arrives while it loads its
        model has nothing wrong with it. `TRANSCRIBER_PATIENCE_SECONDS` of that
        and chunks are counted as failed again, as they always were, until one
        is heard.
        """
        try:
            audio = path.read_bytes()
        except OSError:
            # Counted at its nominal length: the audio happened, and a clock
            # that skipped it would make the next rewind fetch it twice.
            self._account(
                conn, run, float(run.chunk_seconds), failed=1,
                error="a chunk went missing before it was read",
            )
            return True

        seconds = chunk_seconds_of(audio)
        if seconds < MIN_AUDIBLE_SECONDS:
            # The sliver ffmpeg closes on its way out. Nothing to hear, and a
            # refusal from the transcriber would read as the transcriber down.
            path.unlink(missing_ok=True)
            self._account(conn, run, seconds, ok=1)
            return True

        heard = self._transcribe(audio)
        if heard is None:
            now = time.monotonic()
            if run.stalled_since is None:
                run.stalled_since = now
                logger.warning(
                    "the transcriber did not answer; holding session %d's chunks",
                    run.session_id,
                )
            if now - run.stalled_since < TRANSCRIBER_PATIENCE_SECONDS:
                return False
            path.unlink(missing_ok=True)
            self._account(
                conn, run, seconds, failed=1, error="the transcriber did not answer"
            )
            return True
        run.stalled_since = None

        rows: list[tuple[int, int, datetime, float, float, str]] = []
        for start, end, text in _utterances(heard, seconds):
            rows.append((
                run.next_seq + len(rows),
                run.chunk_seq,
                run.wall_origin + timedelta(seconds=run.offset + start),
                run.offset + start,
                max(end - start, 0.0),
                text,
            ))
        # The lines and the clock in one transaction. Apart, a restart between
        # them would leave the lines stored and the clock short of them, and
        # the rewind would hear that audio a second time.
        with conn.transaction():
            written = store.append_segments(conn, run.session_id, rows)
            self._account(conn, run, seconds, ok=1)
        run.next_seq += written
        # Unlinked only now it is accounted for. Nothing keeps audio.
        path.unlink(missing_ok=True)
        return True

    def _account(
        self,
        conn: psycopg.Connection,
        run: Running,
        seconds: float,
        *,
        ok: int = 0,
        failed: int = 0,
        error: str | None = None,
    ) -> None:
        """Move the clock past one chunk, here and on the row."""
        run.offset += seconds
        run.chunk_seq += 1
        run.captured_until = run.wall_origin + timedelta(seconds=run.offset)
        run.allowance = max(run.allowance - 1, 0)
        store.count_chunk(
            conn,
            run.session_id,
            ok=ok,
            failed=failed,
            seconds=seconds,
            until=run.captured_until,
            error=error,
        )

    def _stumble(
        self,
        conn: psycopg.Connection,
        run: Running,
        error: str | None,
        *,
        quiet: bool = False,
    ) -> None:
        """A failed probe or a dead ffmpeg: back off, or give up."""
        run.failures += 1
        if not quiet:
            store.count_chunk(conn, run.session_id, error=error)
        if run.failures >= MAX_FAILURES:
            self._release(run.session_id)
            store.finish(
                conn,
                run.session_id,
                state="failed",
                reason="could not stay connected",
                error=error,
            )
            return
        delay = min(RETRY_BASE_SECONDS * 2 ** (run.failures - 1), MAX_RETRY_SECONDS)
        run.retry_at = time.monotonic() + delay

    def _release(self, session_id: int) -> None:
        run = self._running.pop(session_id, None)
        if run is not None and run.capture is not None:
            run.capture.stop()

    # -- plumbing ---------------------------------------------------------

    def _discard(self, path: Path) -> float:
        try:
            seconds = chunk_seconds_of(path.read_bytes())
        except OSError:
            seconds = 0.0
        path.unlink(missing_ok=True)
        return seconds

    def _transcribe_over_http(self, audio: bytes) -> Transcript | None:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(60.0, connect=3.0))
        return transcribe(audio, content_type="audio/wav", client=self._client)

    def _connection(self) -> psycopg.Connection | None:
        """The database, with the lock. None while another supervisor holds it."""
        if self._conn is not None and not self._conn.closed:
            return self._conn

        conn = psycopg.connect(
            settings().database_url, connect_timeout=5, autocommit=True
        )
        row = conn.execute(
            "select pg_try_advisory_lock(%s)", (SUPERVISOR_LOCK_ID,)
        ).fetchone()
        if row is None or not row[0]:
            conn.close()
            if not self._standing_by:
                # Once, not every two seconds. The overlap during a rolling
                # deploy is normal and should read as one line, not a log.
                logger.info("another supervisor holds the lock; standing by")
                self._standing_by = True
            return None

        self._standing_by = False
        self._conn = conn
        if not self._reconciled:
            store.reconcile(conn)
            self._reconciled = True
        return conn

    def _drop_connection(self) -> None:
        """Let go of the connection, and of the lock that rides on it.

        Every capture goes too. The lock is session scoped, so a broken
        connection means this process is no longer the one supervisor — and two
        ffmpegs on one manifest is exactly what the lock exists to prevent.
        """
        for session_id in list(self._running):
            self._release(session_id)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._reconciled = False

    def shutdown(self) -> None:
        """Put every capture back in the queue, then stop its ffmpeg.

        The rows first. Docker allows ten seconds between SIGTERM and SIGKILL,
        and an ffmpeg idle at the live edge can take most of one segment to
        notice it has been asked to go, so the one step that has to happen is
        done before the ones that can run long. Anything half-heard is thrown
        away with the tmpfs: `captured_until` never counted it, so the next
        supervisor rewinds and hears it then.
        """
        if self._conn is not None and not self._conn.closed:
            try:
                store.reconcile(self._conn)
            except Exception as exc:
                logger.warning("could not settle sessions on the way out: %s", exc)
        for session_id in list(self._running):
            self._release(session_id)
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None
        if self._client is not None:
            self._client.close()
            self._client = None
        logger.info("skybird stopped")


def _utterances(
    heard: Transcript, chunk_seconds: float
) -> list[tuple[float, float, str]]:
    """The chunk as timed lines.

    Falls back to one line spanning the chunk when the transcriber returned no
    segments, which is what an older service or a very short clip does. The
    transcript is then coarser and still correct, rather than absent.
    """
    if heard.segments:
        return [
            (segment.start, segment.end, segment.text)
            for segment in heard.segments
            if segment.text.strip()
        ]
    if heard.text.strip():
        return [(0.0, chunk_seconds, heard.text)]
    return []


def run(config: SkybirdConfig | None = None) -> int:
    Supervisor(config).serve()
    return 0
