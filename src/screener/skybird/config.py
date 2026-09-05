"""Skybird's own configuration.

Nothing here lands in `screener.config.Settings`, which holds the database URL
and nothing else deliberately: a process posting a Discord alert should not need
a chunk length it will never read. `screener.fetch.config`, `screener.ai.config`
and `screener.notify.config` split the same way.
"""

from dataclasses import dataclass

from screener.config import env
from screener.transcribe import MAX_SECONDS

# How long a chunk of audio is, and therefore how far behind live the transcript
# runs. Short enough to read as live, long enough that Whisper has a sentence to
# work with and the boundaries do not dominate.
DEFAULT_CHUNK_SECONDS = 15

# Two, measured against one shared transcriber holding a semaphore of one. Three
# streams would spend more of their time queued behind each other than decoding,
# and would push the dashboard's mic button toward its 30 second busy wait.
DEFAULT_MAX_SESSIONS = 2

# Where the hostname mapping in Cloudflare points, plus local development.
# Twitch checks this against the page framing its player and answers a mismatch
# with a black frame rather than an error.
DEFAULT_EMBED_PARENTS = ("screener.edenmatrix.xyz", "localhost")

# A tmpfs in the container, so chunks never reach a disk. Two minutes of 16 kHz
# mono at the bound below is under 8 MB, which is why this can be memory.
DEFAULT_WORK_DIR = "/tmp/skybird"

# How often the supervisor asks the database what it should be doing. The cost
# of a start or a stop taking this long is nothing against a stream measured in
# hours, and the cost of the query is one indexed read of four live rows.
POLL_SECONDS = 2.0

# Two minutes of backlog at the default chunk length. Past this the oldest chunk
# is dropped and counted, because growing without limit would fill the tmpfs to
# hide a transcriber that is not keeping up.
MAX_PENDING_CHUNKS = 8

# Chunks are refused above this by `screener.transcribe` anyway; catching it
# here means a bad setting fails at startup rather than once an hour.
MAX_CHUNK_SECONDS = int(MAX_SECONDS)
MIN_CHUNK_SECONDS = 5


@dataclass(frozen=True)
class SkybirdConfig:
    """What the capture container and the API both need to agree on."""

    chunk_seconds: int = DEFAULT_CHUNK_SECONDS
    max_sessions: int = DEFAULT_MAX_SESSIONS
    embed_parents: tuple[str, ...] = DEFAULT_EMBED_PARENTS
    work_dir: str = DEFAULT_WORK_DIR

    @classmethod
    def from_env(cls) -> "SkybirdConfig":
        chunk = env.integer("SKYBIRD_CHUNK_SECONDS", DEFAULT_CHUNK_SECONDS)
        if not MIN_CHUNK_SECONDS <= chunk <= MAX_CHUNK_SECONDS:
            raise RuntimeError(
                f"SKYBIRD_CHUNK_SECONDS must be between {MIN_CHUNK_SECONDS} and "
                f"{MAX_CHUNK_SECONDS}, got {chunk}"
            )
        sessions = env.integer("SKYBIRD_MAX_SESSIONS", DEFAULT_MAX_SESSIONS)
        if sessions < 1:
            raise RuntimeError(
                f"SKYBIRD_MAX_SESSIONS must be at least 1, got {sessions}"
            )
        return cls(
            chunk_seconds=chunk,
            max_sessions=sessions,
            embed_parents=_parents(env.optional("SKYBIRD_EMBED_PARENTS")),
            work_dir=env.text("SKYBIRD_WORK_DIR", DEFAULT_WORK_DIR),
        )


def _parents(raw: str | None) -> tuple[str, ...]:
    """Hostnames only.

    A pasted `https://host/` would be sent to Twitch verbatim and match nothing,
    and the failure is a player that stays black — so the scheme and the path
    come off here rather than being left for someone to notice.
    """
    if raw is None:
        return DEFAULT_EMBED_PARENTS
    hosts: list[str] = []
    for entry in raw.split(","):
        host = entry.strip().removeprefix("https://").removeprefix("http://")
        host = host.split("/")[0].split(":")[0].strip().lower()
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts) or DEFAULT_EMBED_PARENTS
