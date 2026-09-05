"""YouTube URLs.

Five shapes reach the same place, and two of them do not name a video at all: a
handle or a channel URL says "whatever this channel is streaming right now",
which nothing knows until YouTube is asked. Those come back with no video id and
no embed, and the supervisor fills both in once the probe has resolved the
broadcast — which is the reason `StreamRef.embed_url` is allowed to be None.
"""

import re
import urllib.parse
from collections.abc import Sequence

from screener.skybird.platforms.base import Platform, StreamRef

NAME = "youtube"
DISPLAY_NAME = "YouTube"

HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})

# Eleven characters of URL-safe base64, which is what a video id has been for
# fifteen years. Anchored, so a longer path segment is not truncated into one.
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# UC + 22 more. The only channel id form the live_stream embed accepts.
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def _video_ref(video_id: str, channel: str | None = None) -> StreamRef:
    return StreamRef(
        platform=NAME,
        external_id=video_id,
        channel=channel,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        embed_url=embed_video(video_id, ()),
    )


def embed_video(video_id: str, parents: Sequence[str]) -> str | None:
    """A player for one broadcast.

    `parents` is unused and the signature carries it anyway: YouTube does not
    ask who is framing it, Twitch does, and one shape for both is what lets the
    registry hold them side by side.
    """
    del parents
    if not VIDEO_ID.match(video_id):
        return None
    return f"https://www.youtube.com/embed/{video_id}?autoplay=1"


def match(url: str, parents: Sequence[str]) -> StreamRef | None:
    del parents
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname is None or parsed.hostname.lower() not in HOSTS:
        return None

    host = parsed.hostname.lower()
    parts = [part for part in parsed.path.split("/") if part]

    # youtu.be/VIDEOID
    if host == "youtu.be":
        if parts and VIDEO_ID.match(parts[0]):
            return _video_ref(parts[0])
        return None

    # youtube.com/watch?v=VIDEOID
    if parts[:1] == ["watch"]:
        candidates = urllib.parse.parse_qs(parsed.query).get("v", [])
        if candidates and VIDEO_ID.match(candidates[0]):
            return _video_ref(candidates[0])
        return None

    # youtube.com/live/VIDEOID and youtube.com/embed/VIDEOID
    if len(parts) >= 2 and parts[0] in {"live", "embed", "v", "shorts"}:
        if VIDEO_ID.match(parts[1]):
            return _video_ref(parts[1])
        return None

    # youtube.com/channel/UCxxxx/live — the one channel form with an embed,
    # because live_stream takes a channel id and nothing else.
    if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID.match(parts[1]):
        channel_id = parts[1]
        return StreamRef(
            platform=NAME,
            external_id=channel_id,
            channel=channel_id,
            canonical_url=f"https://www.youtube.com/channel/{channel_id}/live",
            embed_url=(
                f"https://www.youtube.com/embed/live_stream"
                f"?channel={channel_id}&autoplay=1"
            ),
        )

    # youtube.com/@handle[/live], and the older /c/ and /user/ forms. No embed
    # until the probe names the broadcast.
    handle: str | None = None
    if parts and parts[0].startswith("@"):
        handle = parts[0]
    elif len(parts) >= 2 and parts[0] in {"c", "user"}:
        handle = parts[1]
    if handle:
        return StreamRef(
            platform=NAME,
            external_id=handle,
            channel=handle,
            canonical_url=f"https://www.youtube.com/{handle}/live",
            embed_url=None,
        )
    return None


PLATFORM = Platform(
    name=NAME,
    display_name=DISPLAY_NAME,
    match=match,
    embed_video=embed_video,
)
