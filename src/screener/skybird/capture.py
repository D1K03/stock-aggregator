"""Turning a stream URL into finished chunks of audio.

Two steps, and only the first one knows what a platform is. `probe` asks yt-dlp
which audio track to pull, and `Capture` runs one ffmpeg that cuts it into
fixed-length WAV files. Everything after this point is the same whether the
stream came from YouTube, Twitch or something added next year.

**The audio is never written to a disk.** The chunk directory is a tmpfs in the
container, each file is unlinked as soon as it has been heard, and the directory
goes when the capture does. `screener.transcribe` holds itself to the same rule
and for the same reason: what is worth keeping is the text.

**A capture can start behind live.** A live HLS playlist is a window onto the
recent past rather than a pointer at the present — YouTube's holds an hour,
Twitch's thirty seconds — and ffmpeg can be told to start that many segments
from its end. `rewind` works out how many, so a capture that went away for a
deploy comes back where its audio stopped and reads the backlog at about ninety
times real time before settling at the live edge. The playlist is read once by
`probe`, which is the only thing here that knows what the stream is.

yt-dlp is imported inside `probe`, the way `faster_whisper` is inside
`load_model`. CI installs the `dev` extra and not `stream`, so this module has
to stay importable without it or the supervisor could not be tested at all.
"""

import logging
import math
import queue
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "Capture",
    "Probe",
    "ProbeFailed",
    "Recorder",
    "Rewind",
    "Window",
    "chunk_seconds_of",
    "ffmpeg_command",
    "parse_window",
    "probe",
    "replay_window",
    "rewind",
]

# 16 kHz mono signed 16-bit, which is exactly what Whisper wants — so nothing
# downstream resamples, and a 15 second chunk is 480 KB against a 4 MB cap.
SAMPLE_RATE: Final = 16_000
BYTES_PER_SAMPLE: Final = 2
WAV_HEADER_BYTES: Final = 44

# How long to give ffmpeg to exit on its own before insisting.
TERMINATE_TIMEOUT: Final = 5.0

# Enough stderr to say what went wrong, not enough to hold a log in memory for
# the length of a broadcast.
STDERR_LINES: Final = 12

# Where ffmpeg starts a live HLS playlist when it is not told otherwise: three
# segments from the end (`live_start_index`, libavformat/hls.c). A rewind is
# counted from here, because this is where every capture that did not rewind
# started, and so where its clock was anchored.
LIVE_EDGE_SEGMENTS: Final = 3

# Reading the playlist is one small GET, and a probe that has just spent
# seconds in yt-dlp should not spend many more on something optional.
WINDOW_TIMEOUT_SECONDS: Final = 5.0


class ProbeFailed(Exception):
    """yt-dlp could not tell us where the audio is."""


class Recorder(Protocol):
    """What the supervisor needs from a capture, and nothing more.

    `Capture` below is the one that runs ffmpeg. This is the seam, so the loop
    can be driven by a fake that writes files into a directory — CI installs
    neither `stream` nor `voice`, so a test that needed the real thing could not
    run at all.
    """

    @property
    def running(self) -> bool: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def error(self) -> str | None: ...

    def take(self) -> list[Path]: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Window:
    """How much of a live stream its playlist still holds.

    The mean segment length rather than the target duration, because the two
    differ: Twitch declares six seconds and serves two. Zero segments is "cannot
    rewind" — a playlist that could not be read, a master playlist, or a stream
    that is not HLS at all — and is never an error.

    `ended` is a playlist that has said `#EXT-X-ENDLIST`: the broadcast is over
    whatever yt-dlp still reports. Worth knowing because ffmpeg starts a
    finished playlist at its *first* segment, so treating one as live would read
    the last hour of the stream back in from the top.
    """

    segment_seconds: float = 0.0
    segments: int = 0
    ended: bool = False


@dataclass(frozen=True, slots=True)
class Probe:
    """What yt-dlp knows about a stream right now.

    `manifest_url` expires — hours, on both platforms — which is why the
    supervisor probes again on every reconnect instead of holding one. So does
    the window, which slides a segment at a time.
    """

    manifest_url: str
    video_id: str | None
    title: str | None
    channel: str | None
    is_live: bool
    window: Window = Window()


def probe(url: str) -> Probe:
    """Ask yt-dlp for the audio track, without downloading any of it."""
    try:
        # Stubs are bundled with pyright, the package is not installed here:
        # only the skybird image takes the `stream` extra, the same way only
        # the transcribe image takes `voice`.
        from yt_dlp import YoutubeDL  # pyright: ignore[reportMissingModuleSource]
    except ImportError as exc:  # pragma: no cover - the image always has it
        raise ProbeFailed("yt-dlp is not installed in this image") from exc

    try:
        with YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Audio only. The video track is the overwhelming majority of the
            # bandwidth and none of it reaches the transcriber.
            "format": "bestaudio/best",
        }) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise ProbeFailed(f"{type(exc).__name__}: {exc}") from exc

    if not isinstance(info, dict):
        raise ProbeFailed("yt-dlp returned nothing for that URL")
    # A channel URL can still come back as a playlist even with `noplaylist`;
    # the live broadcast is the first entry.
    entries = info.get("entries")
    if isinstance(entries, list):
        first = next((entry for entry in entries if isinstance(entry, dict)), None)
        if first is None:
            raise ProbeFailed("that channel has nothing playing")
        info = first

    chosen = info
    manifest = info.get("url")
    if not manifest:
        requested = info.get("requested_formats")
        if isinstance(requested, list) and requested and isinstance(requested[0], dict):
            chosen = requested[0]
            manifest = chosen.get("url")
    if not isinstance(manifest, str) or not manifest:
        raise ProbeFailed("yt-dlp found no audio track")

    is_live = bool(info.get("is_live"))
    protocol = chosen.get("protocol")
    hls = isinstance(protocol, str) and protocol.startswith("m3u8")
    window = replay_window(manifest) if is_live and hls else Window()
    return Probe(
        manifest_url=manifest,
        video_id=_text(info.get("id")),
        title=_text(info.get("title")),
        channel=_text(info.get("uploader") or info.get("channel")),
        # The playlist has the last word: yt-dlp can go on calling a broadcast
        # live for a while after the stream itself has said it is over.
        is_live=is_live and not window.ended,
        window=window,
    )


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def replay_window(url: str, *, client: httpx.Client | None = None) -> Window:
    """Read a live playlist for how far back it reaches. Never raises.

    A failure here costs a rewind, not a capture: the probe has already found
    the stream, and a capture that starts at the live edge is what every capture
    did before this existed. The URL is not logged — it carries a signature.
    """
    owned = client is None
    client = client or httpx.Client(
        timeout=WINDOW_TIMEOUT_SECONDS, follow_redirects=True
    )
    try:
        response = client.get(url)
        response.raise_for_status()
        return parse_window(response.text)
    except Exception as exc:
        logger.warning("could not read the replay window: %s", type(exc).__name__)
        return Window()
    finally:
        if owned:
            client.close()


def parse_window(playlist: str) -> Window:
    """The segments an HLS media playlist lists, and how long they are.

    A master playlist names variants rather than segments, so it has no window
    of its own; nor does anything that is not a playlist. Both come back empty,
    which is "cannot rewind", rather than as a guess.
    """
    text = playlist.lstrip("\ufeff").lstrip()
    if not text.startswith("#EXTM3U") or "#EXT-X-STREAM-INF" in text:
        return Window()
    durations: list[float] = []
    for line in text.splitlines():
        if not line.startswith("#EXTINF:"):
            continue
        try:
            durations.append(float(line[len("#EXTINF:"):].split(",", 1)[0]))
        except ValueError:
            return Window()
    ended = "#EXT-X-ENDLIST" in text
    durations = [seconds for seconds in durations if seconds > 0]
    if not durations:
        return Window(ended=ended)
    return Window(
        segment_seconds=sum(durations) / len(durations),
        segments=len(durations),
        ended=ended,
    )


@dataclass(frozen=True, slots=True)
class Rewind:
    """Where in a live playlist to start, so the audio picks up where it stopped.

    `skipped` is the stream between where the audio stopped and where it starts
    again: a hole when positive, heard twice when negative. It is what moves
    the clock, so a line after a hole is still stamped at the second it was
    said.
    """

    live_start_index: int | None
    skipped: float


def rewind(gap: float, window: Window, *, limit: float) -> Rewind:
    """How far behind live to start a capture whose audio stopped `gap` ago.

    `gap` is measured on the capture clock: now, less the moment the stored
    audio reaches. That clock was anchored where a default start puts the audio
    — `LIVE_EDGE_SEGMENTS` from the end of the playlist — so the rewind is
    counted from there too, one segment per `segment_seconds` of gap. It can
    run the other way: a quick reconnect finds the clock *ahead* of now, and
    starting nearer the edge is what stops it hearing the same words twice.

    Whole segments, rounded down, so a seam leans toward losing part of one
    segment rather than repeating it. That is also its precision: ffmpeg counts
    from the end of its own fetch of the playlist, which moves a segment at a
    time, so the seam is good to about one segment — five seconds on YouTube,
    two on Twitch. Bounded by what the playlist still holds and by `limit`,
    which at zero turns rewinding off; what lies beyond either is lost, and
    `skipped` says how much.
    """
    seconds = window.segment_seconds
    if limit <= 0 or seconds <= 0 or window.segments <= LIVE_EDGE_SEGMENTS:
        return Rewind(live_start_index=None, skipped=gap)
    reach = min(window.segments - LIVE_EDGE_SEGMENTS, int(limit // seconds))
    back = max(min(math.floor(gap / seconds), reach), 1 - LIVE_EDGE_SEGMENTS)
    index = -(LIVE_EDGE_SEGMENTS + back)
    return Rewind(
        live_start_index=None if index == -LIVE_EDGE_SEGMENTS else index,
        skipped=gap - back * seconds,
    )


def chunk_seconds_of(audio: bytes) -> float:
    """How long a chunk is, from its size rather than from the decoder.

    The offsets in the transcript are what let a mention be lined up against
    anything else later, so they are computed from the bytes we actually
    captured rather than from a duration the transcriber reports after its voice
    filter has removed the silence.
    """
    payload = max(len(audio) - WAV_HEADER_BYTES, 0)
    return payload / (SAMPLE_RATE * BYTES_PER_SAMPLE)


def ffmpeg_command(
    manifest_url: str,
    *,
    chunk_seconds: int,
    directory: Path,
    live_start_index: int | None = None,
) -> list[str]:
    """The one ffmpeg a capture runs, as an argument list.

    `-live_start_index` only when a rewind asks for it, and before `-i`: it is
    an option of the HLS demuxer, so it belongs to the input, and handed to any
    other kind of input ffmpeg refuses to start at all.
    """
    rewinding = (
        ["-live_start_index", str(live_start_index)]
        if live_start_index is not None
        else []
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        # Ride out the ordinary blip. The manifest still expires, and that
        # is the supervisor's problem rather than this one's.
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        *rewinding,
        "-i", manifest_url,
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        "-f", "segment",
        "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1",
        # The point of this flag: ffmpeg names each segment on stdout the
        # moment it is closed, so nothing has to guess from a modification
        # time whether a file is still being written to.
        "-segment_list", "pipe:1",
        "-segment_list_type", "flat",
        str(directory / "chunk%06d.wav"),
    ]


class Capture:
    """One ffmpeg, cutting one stream into finished WAV files.

    Chunks arrive on a queue rather than by blocking on the process, because the
    supervisor is running the database poll and possibly a second capture on the
    same thread. Two reader threads: one draining the segment list, one draining
    stderr — that second one is not optional, because a stderr pipe nobody reads
    fills up and stops ffmpeg dead.
    """

    def __init__(
        self,
        manifest_url: str,
        *,
        chunk_seconds: int,
        work_dir: str,
        live_start_index: int | None = None,
    ) -> None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="capture-", dir=work_dir))
        self._ready: queue.Queue[Path] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._process = subprocess.Popen(
            ffmpeg_command(
                manifest_url,
                chunk_seconds=chunk_seconds,
                directory=self.directory,
                live_start_index=live_start_index,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._readers = [
            threading.Thread(target=self._drain_segments, daemon=True),
            threading.Thread(target=self._drain_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _drain_segments(self) -> None:
        if self._process.stdout is None:  # pragma: no cover - PIPE is always set
            return
        for line in self._process.stdout:
            name = line.strip()
            if name:
                # Rejoined onto the chunk directory rather than used as given.
                # The segment muxer writes `av_basename` of each file into its
                # list, not the path it was handed, so these arrive as bare
                # `chunk000001.wav` — and a relative path resolves against the
                # process working directory, where there is nothing at all.
                self._ready.put(self.directory / Path(name).name)

    def _drain_stderr(self) -> None:
        if self._process.stderr is None:  # pragma: no cover - PIPE is always set
            return
        for line in self._process.stderr:
            line = line.strip()
            if line:
                self._stderr.append(line)
                logger.debug("ffmpeg: %s", line)

    def take(self) -> list[Path]:
        """Every chunk finished since the last call.

        Never blocks while ffmpeg runs. Once it has exited, the reader is given
        a moment to pass on the last name ffmpeg wrote on the way out, or the
        supervisor would see a dead process with nothing left to hear and tear
        the capture down with the end of the stream still in it.
        """
        if self._process.poll() is not None:
            self._readers[0].join(timeout=1.0)
        finished: list[Path] = []
        while True:
            try:
                finished.append(self._ready.get_nowait())
            except queue.Empty:
                return finished

    @property
    def running(self) -> bool:
        return self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    @property
    def error(self) -> str | None:
        """The last thing ffmpeg complained about, for `last_error`."""
        return self._stderr[-1] if self._stderr else None

    def stop(self) -> None:
        """End the capture and take the chunk directory with it.

        Terminate before kill, so ffmpeg closes the segment it is part way
        through rather than leaving a truncated file — which would otherwise be
        transcribed into half a sentence that reads like something somebody said.
        """
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=TERMINATE_TIMEOUT)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg did not exit on terminate; killing it")
                self._process.kill()
                self._process.wait(timeout=TERMINATE_TIMEOUT)
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()
        for reader in self._readers:
            reader.join(timeout=TERMINATE_TIMEOUT)
        shutil.rmtree(self.directory, ignore_errors=True)
