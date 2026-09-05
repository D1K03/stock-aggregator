"""The transcription service: audio in, text out.

One POST and one GET, on the compose network only. Stdlib `http.server` for the
same reason `screener.health` is: a route that takes a body and answers with two
fields does not justify starlette, pydantic and an async runtime in an image
whose defining property is a dependency list you can read aloud.

**No authentication, by construction rather than by omission.** Caddy routes
`/auth/*`, `/health`, `/ready`, `/status` and `/api/*` to the status service and
everything else to the dashboard, so `/transcribe` matches the catch-all and
reaches Next.js, which does not have it. This container publishes no port. The
session that guards the browser's route lives in the status service, which is
why the browser goes through it rather than here.

One transcription at a time. Threading is on so `/health` still answers while a
clip is being decoded, not so four of them can fight over the two cores this
container is given.

The `transcriber` argument on `build_server` is the test seam. CI installs the
`dev` extra and not `voice`, so `faster_whisper` is not importable there — every
test passes an ordinary Python function instead, and the one import of the model
sits inside `load_model` where nothing but `serve` reaches it.
"""

import io
import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from screener.transcribe.client import MAX_AUDIO_BYTES, Utterance

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8081

# Baked into the image at build time. A model downloaded at runtime means a
# deploy that fails when huggingface.co is unwell, a first voice message that
# waits ninety seconds for weights, and a container that reports healthy while
# holding none.
DEFAULT_MODEL_PATH = "/opt/models/faster-whisper-base.en"

# Matched to the CPU limit in compose, and they have to agree: ctranslate2
# defaults to one thread per core it can see — four on this box — and four
# threads inside a two-core quota spend their time being descheduled, which is
# slower than two threads that are not.
DEFAULT_THREADS = 2

# A dozen words of the vocabulary this system actually uses, so decoding leans
# toward tickers rather than away from them. Free, and the first thing to reach
# for when the complaint is that it heard NVDA as "in video" — a bigger model is
# the second.
VOCABULARY = (
    "Tickers and terms: NVDA, AAPL, MSFT, AMZN, GOOGL, META, TSLA, AVGO, JPM, "
    "XOM. Valuation, quality, momentum, sentiment, insider, percentile, "
    "pillar, screener, threshold, drawdown, earnings."
)

# One at a time, deliberately. Two people cannot generate concurrent voice
# messages often enough to be worth the thrashing, and a queue of one is honest
# about that.
_TRANSCRIBING = threading.BoundedSemaphore(1)

# Long enough to cover the clip in front of you, short enough that a wedged
# worker is reported rather than waited on.
BUSY_WAIT_SECONDS = 30.0

@dataclass(frozen=True, slots=True)
class Heard:
    """One decode: the words, the length, and where each phrase falls."""

    text: str
    seconds: float
    segments: tuple[Utterance, ...] = ()


# What `build_server` hands the handler. The plain pair is still accepted, so a
# test seam stays two lines of Python and only the real model has to care about
# timings.
Transcriber = Callable[[bytes], "Heard | tuple[str, float]"]

# What `load_model` actually returns. Narrower than the seam above on purpose:
# the model always has timings, and only a test is allowed the shorter shape.
ModelRunner = Callable[[bytes], "Heard"]


def _heard(result: "Heard | tuple[str, float]") -> Heard:
    if isinstance(result, Heard):
        return result
    text, seconds = result
    return Heard(text=text, seconds=seconds)


def load_model() -> ModelRunner:
    """The Whisper model, as a function from audio to text and seconds.

    Called once, by `serve`, before the socket opens. Loading lazily on the
    first request would put the model load in front of somebody's first voice
    message, and would let a container holding no weights start successfully,
    report healthy, and fail only when someone used it.
    """
    # Imported here rather than at module scope so this module stays importable
    # without the `voice` extra, which is what lets the HTTP layer be tested in
    # CI. pyright cannot resolve it for the same reason.
    from faster_whisper import WhisperModel  # pyright: ignore[reportMissingImports]

    path = os.environ.get("WHISPER_MODEL_PATH", DEFAULT_MODEL_PATH)
    threads = int(os.environ.get("WHISPER_THREADS") or DEFAULT_THREADS)
    logger.info("loading %s on %d threads", path, threads)
    started = time.perf_counter()
    model = WhisperModel(
        path,
        device="cpu",
        compute_type="int8",
        cpu_threads=threads,
        num_workers=1,
    )
    logger.info("model ready in %.1fs", time.perf_counter() - started)

    def run(audio: bytes) -> Heard:
        segments, info = model.transcribe(
            io.BytesIO(audio),
            language="en",
            beam_size=1,
            # Silero, bundled in the wheel, which is what `onnxruntime` is in
            # the dependency list for. Without it three seconds of room noise
            # comes back as "Thank you for watching!" and Steven answers it.
            vad_filter=True,
            # Off. The default feeds each window the previous window's text,
            # which is how Whisper gets into a loop repeating one phrase.
            condition_on_previous_text=False,
            initial_prompt=VOCABULARY,
        )
        # Kept, rather than joined away. Whisper already knows where each
        # phrase starts, and a transcript of a two hour stream is unusable
        # without it — `text` is still the join, so nothing that only wants the
        # words has to change.
        utterances = tuple(
            Utterance(
                start=float(segment.start),
                end=float(segment.end),
                text=segment.text.strip(),
            )
            for segment in segments
            if segment.text.strip()
        )
        return Heard(
            text=" ".join(utterance.text for utterance in utterances).strip(),
            seconds=float(info.duration),
            segments=utterances,
        )

    return run


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # A half-open socket must not pin a worker thread indefinitely. The body
    # read raises this for its own duration, then puts it straight back.
    timeout = 5

    # How long to wait for the request body specifically. Generous next to the
    # class timeout because an upload crosses the network and a half-open
    # socket does not.
    body_timeout = 30

    server_version = "screener-transcribe"
    sys_version = ""

    transcriber: Transcriber

    def log_message(self, format: str, *args: Any) -> None:
        """Silenced. The container healthcheck polls /health continuously and
        the default access log would bury everything that mattered."""

    def _send(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode())

    def _fail(self, status: HTTPStatus, message: str) -> None:
        # Every failure carries an `error` key, so a client has one shape to
        # parse rather than one per status.
        self._respond(status, {"error": message})

    def _read_body(self) -> bytes | None:
        """The request body, or `None` having already answered.

        Under HTTP/1.1 an unread body is the next request as far as the
        connection is concerned, so every rejection here closes rather than
        leaving half a clip to be parsed as a request line.

        No chunked encoding: the two clients are the status service and the bot,
        both `httpx`, and both send a length.
        """
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.strip().isdigit():
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "no audio")
            return None
        length = int(raw)
        if length == 0:
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "no audio")
            return None
        if length > MAX_AUDIO_BYTES:
            # Refused on the header, so nothing is read and nothing is decoded.
            self.close_connection = True
            self._fail(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"audio over {MAX_AUDIO_BYTES} bytes",
            )
            return None

        self.connection.settimeout(self.body_timeout)
        try:
            body = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "could not read the audio")
            return None
        finally:
            self.connection.settimeout(self.timeout)

        if len(body) != length:
            # Half a clip decodes into half a sentence, which reads as something
            # the speaker said rather than as a truncated upload.
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "the upload ended early")
            return None
        return body

    def do_GET(self) -> None:
        if self.path == "/health":
            # Touches neither the model nor the semaphore: this answers while a
            # transcription is running, which is the whole reason for threading.
            self._respond(HTTPStatus.OK, {"status": "ok"})
        else:
            self._fail(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        if self.path != "/transcribe":
            self._fail(HTTPStatus.NOT_FOUND, "not found")
            return

        audio = self._read_body()
        if audio is None:
            return

        if not _TRANSCRIBING.acquire(timeout=BUSY_WAIT_SECONDS):
            self._fail(HTTPStatus.SERVICE_UNAVAILABLE, "busy")
            return
        try:
            started = time.perf_counter()
            heard = _heard(self.transcriber(audio))
        except Exception as exc:
            # A container this cannot decode is a bad request rather than a
            # broken service, and the caller can say so in one sentence.
            logger.warning("could not decode %d bytes: %s", len(audio), type(exc).__name__)
            self._fail(HTTPStatus.UNPROCESSABLE_ENTITY, "could not decode the audio")
            return
        finally:
            _TRANSCRIBING.release()

        logger.info(
            "transcribed %.1fs of audio in %.1fs, %d characters",
            heard.seconds,
            time.perf_counter() - started,
            len(heard.text),
        )
        self._respond(
            HTTPStatus.OK,
            {
                "text": heard.text,
                "seconds": heard.seconds,
                "segments": [
                    {
                        "start": utterance.start,
                        "end": utterance.end,
                        "text": utterance.text,
                    }
                    for utterance in heard.segments
                ],
            },
        )


def build_server(
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    *,
    transcriber: Transcriber | None = None,
) -> ThreadingHTTPServer:
    """A server bound to `host:port`, not yet serving.

    `transcriber` is the test seam and production never passes it: `serve` loads
    the real model and hands it in. A test passes a plain function, which is how
    every route here is exercised in a CI that does not install the model.
    """
    handler = type(
        "BoundHandler",
        (Handler,),
        {"transcriber": staticmethod(transcriber or load_model())},
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    """Load the weights, then serve until SIGTERM or SIGINT.

    The load happens before the socket opens, so a container that is up is one
    whose weights are in memory. That is what makes the compose healthcheck and
    the deploy smoke test mean anything.
    """
    server = build_server(host, port, transcriber=load_model())

    def stop(*_: Any) -> None:
        # `shutdown()` blocks until `serve_forever()` returns, which it cannot
        # do while the handler that called it is still on the stack. This is
        # PID 1 in the container, so this is the difference between a clean
        # `compose down` and a ten-second SIGKILL wait on every deploy.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info("transcriber serving on %s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        logger.info("transcriber stopped")
