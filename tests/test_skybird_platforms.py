"""URL recognition and player embedding.

No network and no database: this is the layer that only reads a string, which
is what makes adding a platform cheap enough to be worth having done up front.
"""

import pytest

from screener.skybird import PLATFORMS, UnsupportedPlatform, resolve, supported
from screener.skybird.platforms import find

PARENTS = ("screener.edenmatrix.xyz", "localhost")


# -- YouTube ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtube.com/watch?v=jNQXAC9IVRw&t=42s",
        "https://m.youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw",
        "https://www.youtube.com/live/jNQXAC9IVRw",
        "https://www.youtube.com/embed/jNQXAC9IVRw",
        # No scheme is what a paste out of the address bar looks like.
        "youtube.com/watch?v=jNQXAC9IVRw",
        "  https://www.youtube.com/watch?v=jNQXAC9IVRw  ",
    ],
)
def test_every_youtube_url_shape_resolves_to_the_same_video(url):
    ref = resolve(url, parents=PARENTS)
    assert ref.platform == "youtube"
    assert ref.external_id == "jNQXAC9IVRw"
    assert ref.canonical_url == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    assert ref.embed_url == "https://www.youtube.com/embed/jNQXAC9IVRw?autoplay=1"


def test_a_youtube_channel_url_embeds_the_live_stream_without_knowing_the_video():
    # `live_stream?channel=` is the one YouTube embed that means "whatever this
    # channel is showing now", which is exactly what a channel URL asks for.
    ref = resolve(
        "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw/live",
        parents=PARENTS,
    )
    assert ref.external_id == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert ref.embed_url is not None
    assert "live_stream?channel=UCuAXFkgsw1L7xaCfnd5JJOw" in ref.embed_url


def test_a_youtube_handle_resolves_with_no_embed_yet():
    """The reason `StreamRef.embed_url` is allowed to be None.

    A handle names no video until YouTube is asked which broadcast is live, so
    the API stores nothing and the supervisor fills it in after the probe.
    Guessing one here would produce a player that shows an error.
    """
    ref = resolve("https://www.youtube.com/@Bloomberg/live", parents=PARENTS)
    assert ref.platform == "youtube"
    assert ref.external_id == "@Bloomberg"
    assert ref.embed_url is None


def test_a_video_id_of_the_wrong_length_is_not_a_video():
    # Eleven characters, anchored. A shorter path segment silently truncated
    # into an id would produce a capture of somebody else's stream.
    with pytest.raises(UnsupportedPlatform):
        resolve("https://www.youtube.com/watch?v=tooshort", parents=PARENTS)


# -- Twitch -----------------------------------------------------------------


def test_a_twitch_channel_embeds_a_player_carrying_every_parent():
    # Twitch checks `parent` against the host framing the player and answers a
    # mismatch with a black frame rather than an error, so a missing parent is
    # invisible until someone looks at the page.
    ref = resolve("https://www.twitch.tv/somestreamer", parents=PARENTS)
    assert ref.platform == "twitch"
    assert ref.external_id == "somestreamer"
    assert ref.embed_url is not None
    for parent in PARENTS:
        assert f"parent={parent}" in ref.embed_url


def test_the_parents_are_repeated_rather_than_joined():
    # A comma-joined value matches no host at all, and fails the same silent
    # way a missing one does.
    ref = resolve("https://twitch.tv/somestreamer", parents=PARENTS)
    assert ref.embed_url is not None
    assert ref.embed_url.count("parent=") == len(PARENTS)
    assert "%2C" not in ref.embed_url


def test_a_twitch_channel_name_is_folded_so_one_stream_is_one_capture():
    # `external_id` is half of the uniqueness that stops the same stream being
    # captured twice, and Twitch treats logins case-insensitively.
    assert resolve("https://twitch.tv/SomeStreamer").external_id == "somestreamer"


def test_a_twitch_vod_embeds_by_video_rather_than_by_channel():
    ref = resolve("https://www.twitch.tv/videos/1234567890", parents=PARENTS)
    assert ref.external_id == "1234567890"
    assert ref.embed_url is not None
    assert "video=1234567890" in ref.embed_url


@pytest.mark.parametrize("path", ["directory", "settings", "subscriptions", "drops"])
def test_twitchs_own_pages_are_not_channels(path):
    with pytest.raises(UnsupportedPlatform):
        resolve(f"https://www.twitch.tv/{path}", parents=PARENTS)


# -- the registry -----------------------------------------------------------


def test_an_unknown_host_says_what_is_supported():
    with pytest.raises(UnsupportedPlatform) as caught:
        resolve("https://vimeo.com/12345", parents=PARENTS)
    assert "YouTube" in str(caught.value) and "Twitch" in str(caught.value)


def test_an_empty_url_is_refused_rather_than_matched():
    with pytest.raises(UnsupportedPlatform):
        resolve("   ", parents=PARENTS)


def test_every_registered_platform_is_reachable_by_name():
    # `find` is what turns a stored `platform` column back into an adapter, so
    # a registry entry nobody can look up would break the embed after a probe.
    for platform in PLATFORMS:
        assert find(platform.name) is platform
    assert find("myspace") is None


def test_the_registry_and_the_supported_list_cannot_drift():
    assert supported() == ", ".join(p.display_name for p in PLATFORMS)
