"""Twitch URLs.

Two shapes: a channel, which means whatever it is broadcasting now, and a
numbered VOD. Both embed, and both need `parent` — Twitch checks it against the
host serving the page and answers a mismatch with a black frame rather than an
error, which is why the parents are configuration and are built in here rather
than guessed at in the browser.
"""

import re
import urllib.parse
from collections.abc import Sequence

from screener.skybird.platforms.base import Platform, StreamRef

NAME = "twitch"
DISPLAY_NAME = "Twitch"

HOSTS = frozenset({"twitch.tv", "www.twitch.tv", "m.twitch.tv"})

# Twitch logins: 4-25 of letters, digits and underscore, and they cannot be all
# digits — which is what keeps a channel name and a VOD id from colliding in
# `external_id`.
LOGIN = re.compile(r"^[A-Za-z0-9_]{3,25}$")
VIDEO_ID = re.compile(r"^\d+$")

# Path segments that are Twitch's own pages rather than somebody's channel.
RESERVED = frozenset({
    "videos", "video", "directory", "settings", "subscriptions", "following",
    "friends", "inventory", "wallet", "drops", "downloads", "jobs", "turbo",
    "store", "search", "team", "p", "products", "prime", "broadcast",
})


def _player(query: dict[str, str], parents: Sequence[str]) -> str:
    # `parent` is repeated, not comma joined: Twitch reads each occurrence and a
    # joined value matches no host at all.
    pairs = [(key, value) for key, value in query.items()]
    pairs += [("parent", parent) for parent in parents]
    pairs.append(("autoplay", "true"))
    return f"https://player.twitch.tv/?{urllib.parse.urlencode(pairs)}"


def embed_video(video_id: str, parents: Sequence[str]) -> str | None:
    if not VIDEO_ID.match(video_id):
        return None
    return _player({"video": video_id}, parents)


def match(url: str, parents: Sequence[str]) -> StreamRef | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname is None or parsed.hostname.lower() not in HOSTS:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None

    # twitch.tv/videos/12345 and the older twitch.tv/<channel>/video/12345
    if parts[0] in {"videos", "video"} and len(parts) >= 2:
        video_id = parts[1]
        if not VIDEO_ID.match(video_id):
            return None
        return StreamRef(
            platform=NAME,
            external_id=video_id,
            channel=None,
            canonical_url=f"https://www.twitch.tv/videos/{video_id}",
            embed_url=embed_video(video_id, parents),
        )
    if len(parts) >= 3 and parts[1] in {"videos", "video"} and VIDEO_ID.match(parts[2]):
        video_id = parts[2]
        return StreamRef(
            platform=NAME,
            external_id=video_id,
            channel=parts[0].lower(),
            canonical_url=f"https://www.twitch.tv/videos/{video_id}",
            embed_url=embed_video(video_id, parents),
        )

    # twitch.tv/<channel>
    login = parts[0].lower()
    if login in RESERVED or not LOGIN.match(login):
        return None
    return StreamRef(
        platform=NAME,
        external_id=login,
        channel=login,
        canonical_url=f"https://www.twitch.tv/{login}",
        # A channel player follows the channel, so this stays correct across a
        # reconnect and never needs the probe's video id.
        embed_url=_player({"channel": login}, parents),
    )


PLATFORM = Platform(
    name=NAME,
    display_name=DISPLAY_NAME,
    match=match,
    embed_video=embed_video,
)
