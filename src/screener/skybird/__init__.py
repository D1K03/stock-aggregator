"""Skybird: live streams in, transcript out.

Paste a YouTube or Twitch URL and the audio is pulled continuously, cut into
short chunks, transcribed by the service the bot and the dashboard already use,
and stored with the second each phrase was said at. Stop it when you like;
delete it when you like. Nothing here expires on its own.

**A different part of the platform, not a screener feature.** Nothing in
`skybird` is a fact or a score, no scoring query joins to it, and it has its own
lifetime — the three grounds on which `auth` and `audit` have their own schemas
too. What it is for comes later; capturing it honestly comes first.

Three shapes worth knowing before changing any of it:

- **The database is the control plane.** The status service writes a row in
  'requested'; the supervisor in the capture container polls for it. No internal
  HTTP surface between the two, and a capture outlives the process running it.
- **yt-dlp is the platform layer for capture.** A module in `platforms` only
  recognises a URL and builds an embed — it never touches the network. That is
  what makes the third platform one module, one registry entry and one row.
- **Audio is never written to a disk and never kept**, exactly as in
  `screener.transcribe`. Chunks live in a tmpfs for one POST each.

`supervisor` and `capture` are deliberately not re-exported: reaching them is
one import away from `yt-dlp` and a subprocess, and the status service — which
only ever writes a row and reads a transcript — must not pay for either.
"""

from screener.skybird.config import SkybirdConfig
from screener.skybird.platforms import (
    PLATFORMS,
    Platform,
    StreamRef,
    UnsupportedPlatform,
    resolve,
    supported,
)
from screener.skybird.store import (
    AlreadyLive,
    Segment,
    Session,
    create as create_session,
    delete as delete_session,
    get as get_session,
    listing as list_sessions,
    pause as pause_session,
    resume as resume_session,
    active_count as active_session_count,
    request_stop as stop_session,
    segments as session_segments,
)

__all__ = [
    "PLATFORMS",
    "AlreadyLive",
    "Platform",
    "Segment",
    "Session",
    "SkybirdConfig",
    "StreamRef",
    "UnsupportedPlatform",
    "active_session_count",
    "create_session",
    "delete_session",
    "get_session",
    "list_sessions",
    "pause_session",
    "resume_session",
    "resolve",
    "session_segments",
    "stop_session",
    "supported",
]
