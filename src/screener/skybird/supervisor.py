"""The capture loop: one process, one advisory lock, N streams.

What it does every couple of seconds is ask the database what it should be
doing. That is the whole control plane — the status service writes a row and
this reads it — so there is no internal HTTP surface between the two containers
to authenticate, and a capture is durable: the row outlives the process, and
`store.reconcile` settles anything a dead one left behind.

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
import signal
import threading
import time
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
    chunk_seconds_of,
    probe,
)
from screener.skybird.config import (
    MAX_PENDING_CHUNKS,
    POLL_SECONDS,
    SkybirdConfig,
)
from screener.skybird.platforms import find as find_platform
from screener.transcribe import Transcript, transcribe

logger = logging.getLogger(__name__)

__all__ = ["Supervisor", "run"]

# Distinct from the migration lock in `screener.boot`, and numbered after the
# migration that created these tables so the two are traceable to each other.
SUPERVISOR_LOCK_ID: Final = 8_119_012

# Consecutive probe or start failures before a capture is given up on. A live
# stream that has genuinely ended fails every one of these in about a minute.
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
    # Wall clock corresponding to `offset == 0`, re-anchored on every reconnect
    # so an outage does not push the whole transcript permanently behind real
    # time. Within a continuous run the two agree exactly.
    wall_origin: datetime = field(default_factory=lambda: datetime.now(UTC))
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
            "skybird supervising: %ds chunks, %d session(s) at once",
            self.config.chunk_seconds,
            self.config.max_sessions,
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

        try:
            run.capture = self._capture(
                found.manifest_url,
                chunk_seconds=run.chunk_seconds,
                work_dir=self.config.work_dir,
            )
        except Exception as exc:
            self._stumble(conn, run, f"could not start ffmpeg: {exc}")
            return

        run.failures = 0
        run.wall_origin = datetime.now(UTC) - timedelta(seconds=run.offset)
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

        if not run.capture.running:
            code = run.capture.returncode
            error = run.capture.error
            run.capture.stop()
            run.capture = None
            # A manifest URL expires after a few hours, so an exit part way
            # through a broadcast is expected rather than exceptional. Probe
            # again: that is also what notices the stream has ended.
            logger.info(
                "ffmpeg exited (%s) for session %d, reconnecting", code, run.session_id
            )
            self._stumble(conn, run, error, quiet=True)

    def _drain(self, conn: psycopg.Connection, run: Running) -> None:
        assert run.capture is not None
        chunks = run.capture.take()
        if not chunks:
            return

        if len(chunks) > MAX_PENDING_CHUNKS:
            # The transcriber is not keeping up. Dropping the oldest bounds
            # memory and leaves a gap that the offsets make visible, which is
            # better than a tmpfs quietly filling to hide it.
            stale, chunks = chunks[:-MAX_PENDING_CHUNKS], chunks[-MAX_PENDING_CHUNKS:]
            lost = 0.0
            for path in stale:
                lost += self._discard(path)
            run.offset += lost
            store.count_chunk(
                conn,
                run.session_id,
                dropped=len(stale),
                seconds=lost,
                error=f"dropped {len(stale)} chunk(s): the transcriber is behind",
            )

        for path in chunks:
            audio = self._read(path)
            if audio is None:
                store.count_chunk(conn, run.session_id, failed=1,
                                  error="a chunk went missing before it was read")
                continue
            self._store_chunk(conn, run, audio)

    def _store_chunk(
        self, conn: psycopg.Connection, run: Running, audio: bytes
    ) -> None:
        seconds = chunk_seconds_of(audio)
        heard = self._transcribe(audio)
        if heard is None:
            store.count_chunk(
                conn,
                run.session_id,
                failed=1,
                seconds=seconds,
                error="the transcriber did not answer",
            )
            run.offset += seconds
            run.chunk_seq += 1
            return

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
        written = store.append_segments(conn, run.session_id, rows)
        run.next_seq += written
        run.offset += seconds
        run.chunk_seq += 1
        store.count_chunk(conn, run.session_id, ok=1, seconds=seconds)

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

    def _read(self, path: Path) -> bytes | None:
        """The chunk, and then it is gone. Nothing keeps audio."""
        try:
            return path.read_bytes()
        except OSError:
            return None
        finally:
            path.unlink(missing_ok=True)

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
        """Stop everything, and mark the rows so nothing is left saying 'running'."""
        for session_id in list(self._running):
            self._release(session_id)
        if self._conn is not None and not self._conn.closed:
            try:
                store.reconcile(self._conn)
            except Exception as exc:
                logger.warning("could not settle sessions on the way out: %s", exc)
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
