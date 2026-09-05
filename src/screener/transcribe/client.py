"""Asking the transcription service for text.

`httpx` and nothing else, so the gateway bot and the status service can import
this without either of them paying for ctranslate2, onnxruntime and PyAV. The
model lives in `screener.transcribe.server`, which only its own container runs.

Shaped after `screener.bot.render`, which reaches the web container the same way
and for the same reason: the work belongs somewhere else, the call has to be
allowed to fail, and a failure must not take the caller down with it.
"""

import json
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Where the transcription container answers inside the compose network.
# Overridable because "transcribe" is a service name, which only means anything
# in that network.
DEFAULT_TRANSCRIBER = "http://transcribe:8081/transcribe"

# The weights are loaded before the service opens its socket, so this covers
# decode and inference only. A clip at the cap runs well inside it on the two
# cores the container is given; the rest is headroom for a busy box.
TIMEOUT_SECONDS = 60.0

# Separately, and much shorter: a wrong service name should fail in seconds
# rather than in sixty of them.
CONNECT_TIMEOUT_SECONDS = 3.0

# Two minutes. A cost ceiling rather than a technical one — inference is linear
# in the length of the audio, and there are four shared cores on this box.
# Enforced by the callers, which know the duration before any bytes move.
MAX_SECONDS = 120.0

# What a clip at the cap could plausibly weigh, with room to spare. Discord
# picks its own bitrate and the browser is told to use 32 kbps, so neither comes
# close; this is here to stop something that is not a voice note.
MAX_AUDIO_BYTES = 4_000_000


@dataclass(frozen=True, slots=True)
class Utterance:
    """One phrase, and where in the audio it falls.

    Seconds from the start of the clip. Whisper separates these itself and the
    service used to join them and throw the boundaries away; a voice note does
    not miss them, and a transcript of a two hour stream is unusable without
    them.
    """

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Transcript:
    """What was heard, and how long it took to say."""

    text: str
    seconds: float
    # Empty is a legitimate answer, not a missing one: silence has no
    # utterances, and neither does a response from a service too old to send
    # them. Callers that only want the words read `text` and never notice.
    segments: tuple[Utterance, ...] = ()


def transcriber_url() -> str:
    return os.environ.get("TRANSCRIBER_URL", DEFAULT_TRANSCRIBER)


def transcribe(
    audio: bytes,
    *,
    content_type: str = "",
    client: httpx.Client | None = None,
) -> Transcript | None:
    """Audio as text, or `None` if it could not be transcribed.

    Never raises. Unlike a missing chart this does cost the reader their answer,
    because without it there is no question — but a caller can say one honest
    sentence about that, which an exception on the gateway's worker thread
    cannot.

    An empty `text` is **not** None. It is silence, and it comes back as an empty
    transcript, because "I could not hear that" and "you did not say anything"
    are different things to be told.

    `content_type` is passed through as a hint and nothing turns on it: PyAV
    sniffs the container itself, which is the whole reason it is the decoder.
    """
    if not audio:
        return None
    if len(audio) > MAX_AUDIO_BYTES:
        logger.warning("refusing %d bytes of audio, over the cap", len(audio))
        return None

    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    )
    try:
        response = client.post(
            transcriber_url(),
            content=audio,
            headers={"Content-Type": content_type or "application/octet-stream"},
        )
        response.raise_for_status()
        # The mirror of the PNG sniff in `render`: a 200 carrying a sign-in page
        # would otherwise become a transcript, and Steven would answer a
        # question composed of somebody's login form.
        body = json.loads(response.text)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            logger.warning(
                "transcriber returned %s, not a transcript",
                response.headers.get("content-type"),
            )
            return None
        return Transcript(
            text=body["text"].strip(),
            seconds=float(body.get("seconds") or 0.0),
            segments=_utterances(body.get("segments")),
        )
    except Exception as exc:
        logger.warning("could not transcribe: %s", type(exc).__name__)
        return None
    finally:
        if owned:
            client.close()


def _utterances(raw: object) -> tuple[Utterance, ...]:
    """The timed lines, or none of them.

    Defensive to the point of dropping the lot, deliberately: the words are in
    `text` regardless, so a service sending a segment list this cannot read
    should cost the timings and not the transcript.
    """
    if not isinstance(raw, list):
        return ()
    found: list[Utterance] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or 0.0)
        except (TypeError, ValueError):
            return ()
        text = item["text"].strip()
        if text:
            found.append(Utterance(start=start, end=max(end, start), text=text))
    return tuple(found)
