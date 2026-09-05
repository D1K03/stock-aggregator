"""Tools that start, hold and stop a live stream capture.

Control only. Steven can put a stream on and take it off again; he cannot read
what was said on it, and there is deliberately no tool here that returns
transcript text. A line *count* is not the transcript — it is how you tell a
capture that is working from one that is quietly failing, which is the thing you
would ask him about.

Terse for the reason everything in this package is terse: a tool's name,
description and result are tokens paid on every message of every conversation,
not just the one that used it.
"""

import logging

import psycopg

from screener import skybird
from screener.bot.tools.registry import actor, tool
from screener.config import settings

logger = logging.getLogger(__name__)

# Enough to tell two captures apart in a list, and no more. The result is
# context on every following round, so a full broadcast title six times over is
# paid for repeatedly.
TITLE_CHARS = 44

# The list is for picking one out, not for browsing. Anything past this is
# history nobody is about to pause.
MAX_LISTED = 6

ACTIONS = ("pause", "resume", "stop")


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        settings().database_url, connect_timeout=3, autocommit=True
    )


def _short(session: skybird.Session) -> str:
    what = session.title or session.source_url
    if len(what) > TITLE_CHARS:
        what = what[: TITLE_CHARS - 1] + "…"
    return what


@tool("watch", "Start capturing a YouTube or Twitch live stream.")
def watch(url: str) -> str:
    """Request a capture. The supervisor picks it up within a couple of seconds.

    Reports the state it was left in rather than claiming it is running, which
    it is not yet — 'requested' is the honest answer at the moment this returns,
    and the next `captures` call says whether it connected.
    """
    # Who asked, not "steven": the row is shown in the dashboard beside the
    # capture, and a column that said the same thing for every row would tell
    # nobody anything.
    who, _ = actor()
    try:
        config = skybird.SkybirdConfig.from_env()
        ref = skybird.resolve(url, parents=config.embed_parents)
    except skybird.UnsupportedPlatform as exc:
        return str(exc)

    with _connect() as conn:
        running = skybird.active_session_count(conn)
        if running >= config.max_sessions:
            # Refused here as well as at the API, so the model is told the
            # limit in the same breath as being refused by it — otherwise it
            # tries again, or promises a second stream it cannot have.
            return (
                f"at the limit, {running}/{config.max_sessions} capturing. "
                "pause or stop one first"
            )
        try:
            session = skybird.create_session(
                conn, ref, requested_by=who, chunk_seconds=config.chunk_seconds
            )
        except skybird.AlreadyLive:
            return "that stream is already being captured"
    logger.info("watch(%s) -> session %d for %s", ref.platform, session.id, who)
    # The count comes back on success too. Four tokens, and it is what stops
    # "watch these three for me" from being attempted one call at a time.
    return (
        f"#{session.id} {session.state} {ref.platform} {_short(session)} "
        f"({running + 1}/{config.max_sessions})"
    )


@tool("captures", "Live captures: used/limit, and each id and state.")
def captures() -> str:
    """Ids, states, how much each has transcribed, and the cap.

    The id is the point: `hold` needs one, and asking a person for it would be
    asking them to read a number off a screen they may not be looking at.

    The `used/limit` comes first and is always there, even with nothing running.
    `SKYBIRD_MAX_SESSIONS` is configuration, so the system prompt cannot name it
    — that string is built once at import, before secrets are loaded, and a
    number in it would be the default frozen in for ever. This is where the
    model actually learns the limit.
    """
    config = skybird.SkybirdConfig.from_env()
    with _connect() as conn:
        running = skybird.active_session_count(conn)
        found = skybird.list_sessions(conn, limit=MAX_LISTED)
    header = f"{running}/{config.max_sessions} capturing"
    if not found:
        return f"{header}, nothing to show"
    listed = " | ".join(
        f"#{s.id} {s.state} {s.segment_count}L {_short(s)}" for s in found
    )
    return f"{header} · {listed}"


@tool("hold", "Change a capture: action pause, resume or stop.")
def hold(session: int, action: str) -> str:
    """One verb, three values, because three tools is three tool specs.

    A bad `action` comes back as a sentence rather than an exception, so the
    model can correct itself on the next turn instead of the conversation ending
    on a stack trace.
    """
    verb = action.strip().lower()
    if verb not in ACTIONS:
        return f"action must be one of {', '.join(ACTIONS)}"

    movers = {
        "pause": skybird.pause_session,
        "resume": skybird.resume_session,
        "stop": skybird.stop_session,
    }
    with _connect() as conn:
        moved = movers[verb](conn, session)
        found = skybird.get_session(conn, session)
    if found is None:
        return f"no capture #{session}"
    if not moved:
        return f"#{session} is {found.state}, cannot {verb} it"
    return f"#{found.id} {found.state}"
