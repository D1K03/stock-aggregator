"""Turning a stream URL into finished chunks of audio.

Two steps, and only the first one knows what a platform is. `probe` asks yt-dlp
which audio track to pull, and `Capture` runs one ffmpeg that cuts it into
fixed-length WAV files. Everything after this point is the same whether the
stream came from YouTube, Twitch or something added next year.

**The audio is never written to a disk.** The chunk directory is a tmpfs in the
container, each file is unlinked as soon as it has been read, and the directory
goes when the capture does. `screener.transcribe` holds itself to the same rule
and for the same reason: what is worth keeping is the text.

yt-dlp is imported inside `probe`, the way `faster_whisper` is inside
`load_model`. CI installs the `dev` extra and not `stream`, so this module has
to stay importable without it or the supervisor could not be tested at all.
"""

import logging
import queue
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "Capture",
    "Probe",
    "ProbeFailed",
    "Recorder",
    "chunk_seconds_of",
    "probe",
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
class Probe:
    """What yt-dlp knows about a stream right now.

    `manifest_url` expires — hours, on both platforms — which is why the
    supervisor probes again on every reconnect instead of holding one.
    """

    manifest_url: str
    video_id: str | None
    title: str | None
    channel: str | None
    is_live: bool


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

    manifest = info.get("url")
    if not manifest:
        requested = info.get("requested_formats")
        if isinstance(requested, list) and requested:
            manifest = requested[0].get("url")
    if not isinstance(manifest, str) or not manifest:
        raise ProbeFailed("yt-dlp found no audio track")

    return Probe(
        manifest_url=manifest,
        video_id=_text(info.get("id")),
        title=_text(info.get("title")),
        channel=_text(info.get("uploader") or info.get("channel")),
        is_live=bool(info.get("is_live")),
    )


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def chunk_seconds_of(audio: bytes) -> float:
    """How long a chunk is, from its size rather than from the decoder.

    The offsets in the transcript are what let a mention be lined up against
    anything else later, so they are computed from the bytes we actually
    captured rather than from a duration the transcriber reports after its voice
    filter has removed the silence.
    """
    payload = max(len(audio) - WAV_HEADER_BYTES, 0)
    return payload / (SAMPLE_RATE * BYTES_PER_SAMPLE)


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
    ) -> None:
        self._chunk_seconds = chunk_seconds
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="capture-", dir=work_dir))
        self._ready: queue.Queue[Path] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._process = subprocess.Popen(
            self._command(manifest_url),
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

    def _command(self, manifest_url: str) -> list[str]:
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
            "-i", manifest_url,
            "-vn",
            "-ac", "1",
            "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            "-f", "segment",
            "-segment_time", str(self._chunk_seconds),
            "-reset_timestamps", "1",
            # The point of this flag: ffmpeg names each segment on stdout the
            # moment it is closed, so nothing has to guess from a modification
            # time whether a file is still being written to.
            "-segment_list", "pipe:1",
            "-segment_list_type", "flat",
            str(self.directory / "chunk%06d.wav"),
        ]

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
        """Every chunk finished since the last call. Never blocks."""
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
