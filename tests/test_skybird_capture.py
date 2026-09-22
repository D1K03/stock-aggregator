"""Where a capture starts in a live playlist, worked out without one.

Pure, like `plan_chunks` in the sentiment tests: no database, no ffmpeg, no
yt-dlp, and no network — the playlist read goes through `httpx.MockTransport`.
The shapes here are what the live playlists measured as on 2026-09-22: YouTube
hands out an hour of five-second segments, Twitch thirty seconds of two-second
ones with a date on every segment.
"""

from pathlib import Path

import httpx
import pytest

from screener.skybird.capture import (
    Rewind,
    Window,
    ffmpeg_command,
    parse_window,
    replay_window,
    rewind,
)
from screener.skybird.config import DEFAULT_MAX_REWIND_SECONDS, SkybirdConfig

YOUTUBE_WINDOW = Window(segment_seconds=5.0, segments=720)
TWITCH_WINDOW = Window(segment_seconds=2.0, segments=15)


def _youtube_playlist(segments: int = 720, *, ended: bool = False) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:5",
        "#EXT-X-MEDIA-SEQUENCE:86697",
        "#EXT-X-DISCONTINUITY-SEQUENCE:0",
        "#EXT-X-PROGRAM-DATE-TIME:2026-09-22T18:19:00.000+00:00",
    ]
    for n in range(segments):
        lines += ["#EXTINF:5.0,", f"https://example.invalid/sq/{86697 + n}/seg.ts"]
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _twitch_playlist() -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6"]
    for n in range(15):
        lines += [
            f"#EXT-X-PROGRAM-DATE-TIME:2026-09-22T19:25:{n * 2:02d}.000Z",
            "#EXTINF:2.000,live",
            f"https://example.invalid/v1/segment/{n}.ts",
        ]
    # Low-latency hints name segments that do not exist yet; not the window.
    lines.append("#EXT-X-TWITCH-PREFETCH:https://example.invalid/v1/segment/15.ts")
    return "\n".join(lines) + "\n"


# -- reading the window -----------------------------------------------------


def test_a_youtube_playlist_reads_as_an_hour_of_five_second_segments():
    assert parse_window(_youtube_playlist()) == YOUTUBE_WINDOW


def test_a_twitch_playlist_reads_as_its_segments_not_its_target_duration():
    # It declares six seconds and serves two, which is why the mean is taken.
    assert parse_window(_twitch_playlist()) == TWITCH_WINDOW


def test_a_master_playlist_names_variants_and_has_no_window_of_its_own():
    master = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=130000,CODECS="mp4a.40.2"\n'
        "https://example.invalid/234/index.m3u8\n"
    )
    assert parse_window(master) == Window()


@pytest.mark.parametrize("text", ["", "<html>sign in</html>", "#EXTM3U\n#EXTINF:x,\n"])
def test_anything_that_is_not_a_media_playlist_reads_as_cannot_rewind(text):
    assert parse_window(text) == Window()


def test_a_finished_playlist_says_so():
    # ffmpeg starts one of these at its first segment whatever it is told, so
    # it must never be mistaken for live.
    assert parse_window(_youtube_playlist(ended=True)).ended is True
    assert parse_window(_youtube_playlist()).ended is False


def test_the_window_is_read_over_http():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=_youtube_playlist())
    )
    with httpx.Client(transport=transport) as client:
        assert replay_window("https://example.invalid/a.m3u8", client=client) == (
            YOUTUBE_WINDOW
        )


def test_a_playlist_that_cannot_be_read_costs_the_rewind_not_the_capture():
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    with httpx.Client(transport=transport) as client:
        assert replay_window("https://example.invalid/a.m3u8", client=client) == (
            Window()
        )


# -- how far back ------------------------------------------------------------


@pytest.mark.parametrize(
    ("gap", "window", "limit", "expected"),
    [
        # A deploy's worth: nine whole segments back past ffmpeg's own three,
        # and the two seconds that do not make a whole one are skipped.
        (47.0, YOUTUBE_WINDOW, 600, Rewind(live_start_index=-12, skipped=2.0)),
        # Longer than the limit: the most recent ten minutes, and the rest lost.
        (3600.0, YOUTUBE_WINDOW, 600, Rewind(live_start_index=-123, skipped=3000.0)),
        # Longer than Twitch keeps: everything it holds, and the rest lost.
        (47.0, TWITCH_WINDOW, 600, Rewind(live_start_index=-15, skipped=23.0)),
        # Less than a segment: ffmpeg's own start is already the right place.
        (4.0, YOUTUBE_WINDOW, 600, Rewind(live_start_index=None, skipped=4.0)),
        # The clock ahead of now, after a quick reconnect: start nearer the edge
        # rather than hear the same words twice.
        (-7.0, YOUTUBE_WINDOW, 600, Rewind(live_start_index=-1, skipped=3.0)),
        # ...but never past the newest segment there is.
        (-20.0, YOUTUBE_WINDOW, 600, Rewind(live_start_index=-1, skipped=-10.0)),
    ],
)
def test_a_rewind_is_whole_segments_counted_from_where_ffmpeg_starts(
    gap, window, limit, expected
):
    assert rewind(gap, window, limit=limit) == expected


@pytest.mark.parametrize(
    "window",
    [Window(), Window(segment_seconds=5.0, segments=3), Window(segments=720)],
)
def test_no_playlist_to_rewind_into_means_the_live_edge_and_the_whole_gap_lost(
    window,
):
    assert rewind(47.0, window, limit=600) == Rewind(live_start_index=None, skipped=47.0)


def test_a_limit_of_zero_turns_rewinding_off():
    # `SKYBIRD_MAX_REWIND_SECONDS=0`: every connect starts where it always did.
    assert rewind(47.0, YOUTUBE_WINDOW, limit=0) == Rewind(None, 47.0)
    assert rewind(-7.0, YOUTUBE_WINDOW, limit=0) == Rewind(None, -7.0)


# -- the command -------------------------------------------------------------


def test_the_command_leaves_the_start_to_ffmpeg_unless_told():
    command = ffmpeg_command(
        "https://example.invalid/a.m3u8", chunk_seconds=15, directory=Path("/tmp/x")
    )
    assert "-live_start_index" not in command


def test_a_rewound_command_names_the_start_before_the_input():
    # An option of the HLS demuxer, so it belongs to the input: after `-i` it
    # would be read as an output option and refused.
    command = ffmpeg_command(
        "https://example.invalid/a.m3u8",
        chunk_seconds=15,
        directory=Path("/tmp/x"),
        live_start_index=-12,
    )
    at = command.index("-live_start_index")
    assert command[at + 1] == "-12"
    assert at < command.index("-i")


# -- the switch --------------------------------------------------------------


def test_the_rewind_limit_defaults_to_what_the_tmpfs_holds(monkeypatch):
    monkeypatch.delenv("SKYBIRD_MAX_REWIND_SECONDS", raising=False)
    assert SkybirdConfig.from_env().max_rewind_seconds == DEFAULT_MAX_REWIND_SECONDS


def test_a_rewind_limit_of_zero_is_how_it_is_turned_off(monkeypatch):
    monkeypatch.setenv("SKYBIRD_MAX_REWIND_SECONDS", "0")
    assert SkybirdConfig.from_env().max_rewind_seconds == 0


@pytest.mark.parametrize("value", ["-1", str(DEFAULT_MAX_REWIND_SECONDS + 1)])
def test_a_rewind_limit_past_the_tmpfs_or_below_zero_stops_the_process(
    monkeypatch, value
):
    # Past the ceiling ffmpeg would fill the disk it writes to and take both
    # captures down, so a bad value fails at startup rather than on the day.
    monkeypatch.setenv("SKYBIRD_MAX_REWIND_SECONDS", value)
    with pytest.raises(RuntimeError, match="SKYBIRD_MAX_REWIND_SECONDS"):
        SkybirdConfig.from_env()
