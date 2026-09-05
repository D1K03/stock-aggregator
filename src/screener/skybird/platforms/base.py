"""The seam every platform adapter implements.

Separate from `__init__` so the registry can import the adapters and the
adapters can import these, without the two importing each other.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass


class UnsupportedPlatform(Exception):
    """The URL is not one any adapter recognises."""


@dataclass(frozen=True, slots=True)
class StreamRef:
    """A stream, identified well enough to capture it and to show it.

    `external_id` is whatever the adapter can match again tomorrow — a video id
    where the URL names one, a channel where it does not. With `platform` it is
    the uniqueness that stops one stream being captured twice at once.

    `embed_url` may be None. A YouTube handle URL names no video until something
    asks YouTube which broadcast is live, so the supervisor fills it in after the
    probe rather than the API guessing at it.
    """

    platform: str
    external_id: str
    channel: str | None
    canonical_url: str
    embed_url: str | None


Matcher = Callable[[str, Sequence[str]], StreamRef | None]
VideoEmbedder = Callable[[str, Sequence[str]], str | None]


@dataclass(frozen=True, slots=True)
class Platform:
    """One site's worth of URL recognition.

    `name` is the primary key in `skybird.platform`, so the two have to agree —
    a mismatch is a foreign key violation at the moment somebody pastes a URL,
    which is a good place for it to surface.
    """

    name: str
    display_name: str
    match: Matcher
    # Used only when `match` could not name a video: once the probe resolves the
    # broadcast, this turns that id into a player.
    embed_video: VideoEmbedder


_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def with_scheme(url: str) -> str:
    """`youtube.com/watch?v=x` is what a paste from the address bar looks like."""
    url = url.strip()
    if not url or _SCHEME.match(url):
        return url
    return f"https://{url}"
