"""Every skybird statement, in one module.

The tables are also the control plane: the status service writes a row in
'requested' and the supervisor in the capture container reads it. That is why
there is no internal HTTP surface between the two, and why a capture survives
the container that was running it — a session left 'running' by a process that
died is still there on the next boot, and `reconcile` is what notices.

Connections are passed in and never opened here. Nothing in this project pools,
and the two callers want opposite lifetimes: the status service opens one per
request, and the supervisor holds one for as long as it is up.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg

from screener.skybird.platforms import StreamRef

logger = logging.getLogger(__name__)

__all__ = [
    "AlreadyLive",
    "Segment",
    "Session",
    "append_segments",
    "count_chunk",
    "create",
    "delete",
    "describe",
    "finish",
    "get",
    "listing",
    "active_count",
    "next_seq",
    "pause",
    "pending",
    "reconcile",
    "request_stop",
    "resume",
    "segments",
    "start",
]

# The states in which a capture is somebody's: it holds its slot in the partial
# unique index, the supervisor still has to look at it, and the interface should
# show it above history. Repeated in the partial indexes in migration 015, and
# the two have to agree or the poll stops using the index.
LIVE_STATES: Final = ("requested", "starting", "running", "paused", "stopping")

# The subset that is actually costing something. `paused` is live but idle -- no
# ffmpeg, no chunks, nothing queued at the transcriber -- so it holds its stream
# without holding a slot against the session cap. This is the one place the two
# lists have to differ, and the reason they are two lists.
ACTIVE_STATES: Final = ("requested", "starting", "running", "stopping")

_COLUMNS: Final = """
    s.id, s.platform, s.external_id, s.channel, s.title, s.source_url,
    s.embed_url, s.state, s.stop_reason, s.requested_by, s.requested_at,
    s.started_at, s.stopped_at, s.chunk_seconds, s.chunks_ok, s.chunks_failed,
    s.chunks_dropped, s.last_error, s.captured_seconds
"""


class AlreadyLive(Exception):
    """That stream is already being captured."""


@dataclass(frozen=True, slots=True)
class Session:
    id: int
    platform: str
    external_id: str
    channel: str | None
    title: str | None
    source_url: str
    embed_url: str | None
    state: str
    stop_reason: str | None
    requested_by: str
    requested_at: datetime
    started_at: datetime | None
    stopped_at: datetime | None
    chunk_seconds: int
    chunks_ok: int
    chunks_failed: int
    chunks_dropped: int
    last_error: str | None
    # Seconds of audio captured across every reconnect and every pause. This is
    # what the transcript's offsets count, and it lives here rather than in the
    # supervisor because a pause has to come back and carry on counting.
    captured_seconds: float = 0.0
    segment_count: int = 0
    last_segment_at: datetime | None = None

    @property
    def live(self) -> bool:
        """Still somebody's, including paused."""
        return self.state in LIVE_STATES

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    def as_json(self) -> dict[str, Any]:
        """The shape the dashboard reads. Timestamps as ISO 8601, in UTC."""
        return {
            "id": self.id,
            "platform": self.platform,
            "external_id": self.external_id,
            "channel": self.channel,
            "title": self.title,
            "source_url": self.source_url,
            "embed_url": self.embed_url,
            "state": self.state,
            "stop_reason": self.stop_reason,
            "requested_by": self.requested_by,
            "requested_at": _iso(self.requested_at),
            "started_at": _iso(self.started_at),
            "stopped_at": _iso(self.stopped_at),
            "chunk_seconds": self.chunk_seconds,
            "chunks_ok": self.chunks_ok,
            "chunks_failed": self.chunks_failed,
            "chunks_dropped": self.chunks_dropped,
            "last_error": self.last_error,
            "captured_seconds": self.captured_seconds,
            "segment_count": self.segment_count,
            "last_segment_at": _iso(self.last_segment_at),
            "live": self.live,
        }


@dataclass(frozen=True, slots=True)
class Segment:
    """One utterance, with the second it was said at."""

    seq: int
    chunk_seq: int
    captured_at: datetime
    offset_seconds: float
    duration_seconds: float
    text: str

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "chunk_seq": self.chunk_seq,
            "captured_at": _iso(self.captured_at),
            "offset_seconds": self.offset_seconds,
            "duration_seconds": self.duration_seconds,
            "text": self.text,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _session(row: Sequence[Any]) -> Session:
    return Session(
        id=row[0],
        platform=row[1],
        external_id=row[2],
        channel=row[3],
        title=row[4],
        source_url=row[5],
        embed_url=row[6],
        state=row[7],
        stop_reason=row[8],
        requested_by=row[9],
        requested_at=row[10],
        started_at=row[11],
        stopped_at=row[12],
        chunk_seconds=row[13],
        chunks_ok=row[14],
        chunks_failed=row[15],
        chunks_dropped=row[16],
        last_error=row[17],
        captured_seconds=float(row[18]),
        segment_count=row[19] if len(row) > 19 else 0,
        last_segment_at=row[20] if len(row) > 20 else None,
    )


def create(
    conn: psycopg.Connection,
    ref: StreamRef,
    *,
    requested_by: str,
    chunk_seconds: int,
) -> Session:
    """Ask for a capture. `AlreadyLive` if that stream already has one.

    The refusal comes from the partial unique index rather than from a read
    first: two people pasting the same URL at once would both pass a check and
    both insert, and the result is one manifest fetched twice and transcribed
    twice.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into skybird.stream_session (
                    platform, external_id, channel, title, source_url,
                    embed_url, requested_by, chunk_seconds
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    ref.platform,
                    ref.external_id,
                    ref.channel,
                    None,
                    ref.canonical_url,
                    ref.embed_url,
                    requested_by,
                    chunk_seconds,
                ),
            )
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise AlreadyLive(
            f"{ref.canonical_url} is already being captured"
        ) from exc
    assert row is not None
    created = get(conn, row[0])
    assert created is not None
    return created


def get(conn: psycopg.Connection, session_id: int) -> Session | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select {_COLUMNS},
                   coalesce(t.segment_count, 0), t.last_segment_at
            from skybird.stream_session s
            left join lateral (
                select count(*) as segment_count, max(captured_at) as last_segment_at
                from skybird.transcript_segment
                where session_id = s.id
            ) t on true
            where s.id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
    return _session(row) if row is not None else None


def listing(conn: psycopg.Connection, *, limit: int = 50) -> list[Session]:
    """Sessions newest first, live ones above history.

    Ordering on `live` before the timestamp because a running capture is the
    thing you came to the page for, and it is not necessarily the most recently
    requested one.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select {_COLUMNS},
                   coalesce(t.segment_count, 0), t.last_segment_at
            from skybird.stream_session s
            left join lateral (
                select count(*) as segment_count, max(captured_at) as last_segment_at
                from skybird.transcript_segment
                where session_id = s.id
            ) t on true
            order by (s.state in ('requested','starting','running','paused','stopping')) desc,
                     s.requested_at desc
            limit %s
            """,
            (limit,),
        )
        return [_session(row) for row in cur.fetchall()]


def active_count(conn: psycopg.Connection) -> int:
    """How many captures are costing something, which is what the cap bounds.

    Deliberately not `live`: a paused capture holds its stream and its place in
    the list, but no ffmpeg and no share of the transcriber, so counting it
    against the cap would refuse a stream on behalf of one that is not running.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) from skybird.stream_session
            where state in ('requested','starting','running','stopping')
            """
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def pending(conn: psycopg.Connection) -> list[Session]:
    """What the supervisor should act on: everything still live.

    Includes 'starting' and 'running' so a supervisor that has just taken the
    lock can see captures it does not own — `reconcile` is what settles those,
    and it runs before this does.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select {_COLUMNS}
            from skybird.stream_session s
            where s.state in ('requested','starting','running','paused','stopping')
            order by s.requested_at
            """
        )
        return [_session(row) for row in cur.fetchall()]


def start(conn: psycopg.Connection, session_id: int) -> bool:
    """Claim a requested capture. False if somebody else got there first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            update skybird.stream_session
               set state = 'starting', last_error = null
             where id = %s and state = 'requested'
            """,
            (session_id,),
        )
        return cur.rowcount == 1


def describe(
    conn: psycopg.Connection,
    session_id: int,
    *,
    title: str | None = None,
    channel: str | None = None,
    embed_url: str | None = None,
    running: bool = False,
) -> None:
    """What the probe learned: the title, and often the embed.

    `coalesce` on every column, so a probe that came back without a title does
    not erase the one the previous probe found. `started_at` is stamped once and
    survives a reconnect for the same reason — it anchors every offset in the
    transcript.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update skybird.stream_session
               set title      = coalesce(%s, title),
                   channel    = coalesce(%s, channel),
                   embed_url  = coalesce(%s, embed_url),
                   state      = case when %s::boolean then 'running' else state end,
                   started_at = case when %s::boolean then coalesce(started_at, now())
                                     else started_at end
             where id = %s
            """,
            (title, channel, embed_url, running, running, session_id),
        )


def request_stop(conn: psycopg.Connection, session_id: int) -> bool:
    """Ask for a capture to end. False if it was not live to begin with.

    'requested' and 'paused' have no process behind them, so they go straight to
    'stopped'. Routing them through 'stopping' would resolve on the next poll
    anyway, but it would mean a row asking a supervisor to tear down an ffmpeg
    that was never there — and reading as though something were still winding
    down when nothing is.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update skybird.stream_session
               set state = case when state in ('requested','paused') then 'stopped'
                                else 'stopping' end,
                   stopped_at = case when state in ('requested','paused') then now()
                                     else stopped_at end,
                   stop_reason = 'asked to stop'
             where id = %s and state in ('requested','starting','running','paused')
            """,
            (session_id,),
        )
        return cur.rowcount == 1


def pause(conn: psycopg.Connection, session_id: int) -> bool:
    """Hold a capture without giving up its stream. False if it was not running.

    The ffmpeg goes; the row stays live. It keeps its slot in the partial unique
    index, so nobody can start a second capture of the same stream while it is
    held, and it survives a supervisor restart untouched -- `reconcile` settles
    only the states that imply a process, and this is not one of them.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update skybird.stream_session
               set state = 'paused', stop_reason = null
             where id = %s and state in ('requested','starting','running')
            """,
            (session_id,),
        )
        return cur.rowcount == 1


def resume(conn: psycopg.Connection, session_id: int) -> bool:
    """Put a paused capture back in the queue. False if it was not paused.

    Back to 'requested' rather than straight to 'running', so it goes through
    exactly the path a new capture does: the supervisor probes again -- the
    manifest it had will have expired -- and the session cap applies, so
    resuming a third stream waits its turn rather than overrunning the
    transcriber. The transcript carries on from where it stopped, because
    `captured_seconds` and the sequence numbers are both in the database.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update skybird.stream_session
               set state = 'requested', last_error = null
             where id = %s and state = 'paused'
            """,
            (session_id,),
        )
        return cur.rowcount == 1


def finish(
    conn: psycopg.Connection,
    session_id: int,
    *,
    state: str,
    reason: str,
    error: str | None = None,
) -> None:
    """The end of a capture, however it ended."""
    if state not in {"stopped", "failed"}:
        raise ValueError(f"{state!r} is not an ending")
    with conn.cursor() as cur:
        cur.execute(
            """
            update skybird.stream_session
               set state = %s, stop_reason = %s, stopped_at = now(),
                   last_error = coalesce(%s, last_error)
             where id = %s
            """,
            (state, reason, error, session_id),
        )


def reconcile(conn: psycopg.Connection) -> int:
    """Settle captures left behind by a supervisor that died.

    Runs once, after the advisory lock is taken and before anything is started,
    so 'starting' or 'running' at that moment can only mean a process that is no
    longer there. Marked failed rather than stopped: nobody asked for it to end,
    and a row that says otherwise would read as a clean finish.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update skybird.stream_session
               set state = 'failed', stop_reason = 'supervisor_restart',
                   stopped_at = now()
             where state in ('starting','running','stopping')
            """
        )
        settled = cur.rowcount
    if settled:
        logger.warning("reconciled %d capture(s) left by a previous run", settled)
    return settled


def count_chunk(
    conn: psycopg.Connection,
    session_id: int,
    *,
    ok: int = 0,
    failed: int = 0,
    dropped: int = 0,
    seconds: float = 0.0,
    error: str | None = None,
) -> None:
    """Move the chunk counters, and record the last thing that went wrong.

    Counted rather than inferred from the transcript: a stream failing every
    chunk and a stream nobody is talking on produce the same empty transcript,
    and the interface has to be able to tell them apart.

    `seconds` moves the capture clock in the same statement, which is what lets
    a paused capture come back and carry on counting. Every chunk advances it,
    including one that failed to transcribe and one that was dropped -- the
    audio happened either way, and an offset that skipped it would put every
    line after the gap at the wrong second.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update skybird.stream_session
               set chunks_ok        = chunks_ok + %s,
                   chunks_failed    = chunks_failed + %s,
                   chunks_dropped   = chunks_dropped + %s,
                   captured_seconds = captured_seconds + %s,
                   last_error       = coalesce(%s, last_error)
             where id = %s
            """,
            (ok, failed, dropped, seconds, error, session_id),
        )


def next_seq(conn: psycopg.Connection, session_id: int) -> int:
    """Where the transcript left off, so a reconnect continues rather than collides."""
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce(max(seq), 0) + 1 from skybird.transcript_segment "
            "where session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 1


def append_segments(
    conn: psycopg.Connection,
    session_id: int,
    rows: Iterable[tuple[int, int, datetime, float, float, str]],
) -> int:
    """Write a chunk's utterances. Returns how many landed.

    Empty text is dropped rather than stored. Whisper is run with a voice
    filter, so silence comes back as nothing at all, and a row saying nothing
    was said is not worth the space or the scroll.
    """
    values = [row for row in rows if row[5].strip()]
    if not values:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into skybird.transcript_segment (
                session_id, seq, chunk_seq, captured_at, offset_seconds,
                duration_seconds, text
            ) values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (session_id, seq) do nothing
            """,
            [(session_id, *value) for value in values],
        )
    return len(values)


def segments(
    conn: psycopg.Connection,
    session_id: int,
    *,
    after: int = 0,
    limit: int = 500,
) -> list[Segment]:
    """Everything said after a sequence number — what the dashboard polls."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select seq, chunk_seq, captured_at, offset_seconds,
                   duration_seconds, text
            from skybird.transcript_segment
            where session_id = %s and seq > %s
            order by seq
            limit %s
            """,
            (session_id, after, limit),
        )
        return [
            Segment(
                seq=row[0],
                chunk_seq=row[1],
                captured_at=row[2],
                offset_seconds=float(row[3]),
                duration_seconds=float(row[4]),
                text=row[5],
            )
            for row in cur.fetchall()
        ]


def delete(conn: psycopg.Connection, session_id: int) -> bool:
    """Remove a session and its transcript. False if it was not there.

    The segments go with it through `on delete cascade`, which is the whole
    retention mechanism: nothing here expires on its own, and this is how it
    goes away.
    """
    with conn.cursor() as cur:
        cur.execute(
            "delete from skybird.stream_session where id = %s", (session_id,)
        )
        return cur.rowcount == 1
