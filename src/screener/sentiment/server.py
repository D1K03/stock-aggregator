"""The sentiment service: text in, three probabilities out.

One POST and one GET, on the compose network only. Stdlib `http.server` for the
same reason `screener.transcribe.server` and `screener.health` are: a route that
takes a list of strings and answers with a list of triples does not justify
starlette, pydantic and an async runtime in an image whose defining property is
a dependency list you can read aloud.

**No authentication, by construction rather than by omission.** Caddy routes
`/auth/*`, `/health`, `/ready`, `/status` and `/api/*` to the status service and
everything else to the dashboard, so `/score` matches the catch-all and reaches
Next.js, which does not have it. This container publishes no port.

One batch at a time. Threading is on so `/health` still answers while a batch is
running, not so four of them can fight over the two cores this container is
given.

The `scorer` argument on `build_server` is the test seam. CI installs the `dev`
extra and not `sentiment`, so neither `onnxruntime` nor `tokenizers` is
importable there — every test passes an ordinary Python function instead, and
the one import of the model sits inside `load_model` where nothing but `serve`
reaches it.
"""

import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from screener.sentiment.client import LABELS, MAX_CHARS, MAX_TEXTS, Sentiment

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8083

# Baked into the image at build time, the way the transcriber's weights are. A
# model downloaded at runtime means a deploy that fails when huggingface.co is
# unwell, a first batch that waits on a 438 MB download, and a container that
# reports healthy while holding nothing.
DEFAULT_MODEL_DIR = "/opt/models/finbert"

# Matched to the CPU limit in compose, and they have to agree: onnxruntime
# defaults to one thread per core it can see -- four on this box -- and four
# threads inside a two-core quota spend their time being descheduled, which is
# slower than two threads that are not. Exactly the lesson WHISPER_THREADS
# already carries in the sibling container.
DEFAULT_THREADS = 2

# FinBERT is BERT-base: 512 word pieces and not one more. A Reddit comment
# longer than that is truncated rather than split, and that is a real limit
# worth knowing about -- the model reads the opening of a long post and nothing
# else. Splitting and averaging would invent a number for the rest of it.
MAX_TOKENS = 512

# The most rows that go through the graph at once, inside one request.
#
# Separate from MAX_TEXTS, which is how many a caller may send. Requests are run
# in length order, because a batch pads to its longest member: one 512-token
# comment in a chunk of one-line headlines makes every other row in that chunk
# cost 512 tokens of arithmetic to produce the same answer. Sorting first is
# free and keeps the padding local to texts that actually are that long.
INFERENCE_BATCH = 32

# And the bound that actually matters, because rows are the wrong unit.
#
# **This is a fix for an OOM kill in production, not a tuning knob.** Attention
# is quadratic in sequence length, so a chunk costs roughly rows times tokens
# squared: 32 one-line headlines is nothing and 32 texts at the 512-token cap
# reached 2,091 MB and was killed by the cgroup. Both are the same 32 rows,
# which is why counting rows cannot bound the memory.
#
# The length sort above is what makes a token budget work cleanly: rows in a
# chunk are already near-uniform, so dividing the budget by the longest of them
# gives a chunk that is full of short texts or a few long ones, and never 32
# rows of 512 tokens. 4,096 is eight rows at the cap, measured to sit inside the
# container's 2 GB with room for the 438 MB graph.
MAX_BATCH_TOKENS = 4096

# One batch at a time, deliberately. This saturates its whole CPU quota for as
# long as the batch is, and a second concurrent request would not finish sooner
# -- it would make both slower and hold twice the memory.
_SCORING = threading.BoundedSemaphore(1)

# Long enough to cover a batch already running, short enough that a wedged
# worker is reported rather than waited on. **Must stay well under the client's
# TIMEOUT_SECONDS**, and the arithmetic is in the comment there: a request that
# waits this long and then runs a full slow batch still has to answer before the
# caller gives up, or the wait is spent producing a reading nobody receives.
BUSY_WAIT_SECONDS = 60.0

# The most a request body may weigh. Refused on the header, so nothing is read
# and nothing is decoded.
#
# Four bytes per character, not one: httpx encodes with `ensure_ascii=False`, so
# a body is UTF-8 rather than escaped ASCII and one emoji is four bytes — and
# r/wallstreetbets is not short of them. At two bytes a legal batch of rockets
# would have been refused as oversized. The real bound on work is MAX_TEXTS and
# MAX_CHARS, checked after parsing; this only stops something absurd arriving.
MAX_BODY_BYTES = MAX_TEXTS * MAX_CHARS * 4 + 1024


# What `build_server` hands the handler: texts in, one reading each, in order.
# A test passes a plain function; `serve` passes the model.
Scorer = Callable[[Sequence[str]], Sequence[Sentiment]]


def read_labels(directory: Path) -> tuple[str, ...]:
    """The model's output columns, in the order the graph emits them.

    Written beside the weights at export time from the checkpoint's own
    `id2label`, and **this is the one mapping in the service that must not be
    assumed.** FinBERT's columns are not alphabetical and not the order anyone
    would guess; read them wrong and every reading comes out sign-flipped,
    confidently, with nothing to notice. So it is refused rather than defaulted:
    a container that cannot name its own columns does not serve.
    """
    labels = json.loads(directory.joinpath("labels.json").read_text())
    if not isinstance(labels, list) or sorted(labels) != sorted(LABELS):
        raise RuntimeError(
            f"{directory}/labels.json must list {sorted(LABELS)}, found {labels!r}"
        )
    return tuple(labels)


def plan_chunks(lengths: Sequence[int]) -> list[list[int]]:
    """Row indices grouped into chunks that fit both bounds, shortest first.

    Pure, and at module scope rather than inside `load_model`, because this is
    the arithmetic that decides whether the container survives a request: a
    chunk of 32 rows at the 512-token cap reached 2,091 MB and was killed by the
    cgroup. Keeping it out here is what lets a CI with no model check it.

    Sorted by length first, so the longest row in a chunk is the one being
    added: the width the chunk will pad to is known before it is added, and the
    budget is checked against that width rather than an average a single long
    row would blow past.
    """
    order = sorted(range(len(lengths)), key=lambda index: lengths[index])
    chunks: list[list[int]] = []
    current: list[int] = []
    width = 0
    for index in order:
        widest = max(width, lengths[index])
        if current and (
            len(current) >= INFERENCE_BATCH
            or (len(current) + 1) * widest > MAX_BATCH_TOKENS
        ):
            chunks.append(current)
            current, widest = [], lengths[index]
        current.append(index)
        width = widest
    if current:
        chunks.append(current)
    return chunks


def load_model() -> tuple[Scorer, tuple[str, ...]]:
    """FinBERT as a function from texts to readings, and its column order.

    Called once, by `serve`, before the socket opens. Loading lazily on the
    first request would put a 438 MB graph in front of somebody's first batch,
    and would let a container holding no weights start, report healthy, and fail
    only when used.
    """
    # Imported here rather than at module scope so this module stays importable
    # without the `sentiment` extra, which is what lets the HTTP layer be tested
    # in a CI that does not install a BERT. pyright cannot resolve them for the
    # same reason.
    import numpy as np  # pyright: ignore[reportMissingImports]
    import onnxruntime as ort  # pyright: ignore[reportMissingImports]
    from tokenizers import Tokenizer  # pyright: ignore[reportMissingImports]

    directory = Path(os.environ.get("SENTIMENT_MODEL_DIR") or DEFAULT_MODEL_DIR)
    threads = int(os.environ.get("SENTIMENT_THREADS") or DEFAULT_THREADS)
    labels = read_labels(directory)

    # The tokenizer, not one of ours. WordPiece is short enough to write and
    # subtle enough to get quietly wrong -- accents, control characters, the
    # CJK spacing rule -- and a tokenizer that differs from the one FinBERT was
    # trained with does not fail, it returns a slightly wrong number. This is
    # the same wheel `faster-whisper` already installs in the sibling model
    # container, so it costs this image nothing the other does not pay.
    tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    # Padding is done below rather than by the tokenizer, so the whole request
    # is tokenized once and each chunk is padded to its own longest row. Asking
    # the tokenizer to pad would pad every row to the longest in the *request*,
    # which is the waste the length sort exists to avoid.
    pad_id = tokenizer.token_to_id("[PAD]")

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    logger.info("loading %s on %d threads", directory, threads)
    started = time.perf_counter()
    session = ort.InferenceSession(
        str(directory / "finbert.onnx"), options, providers=["CPUExecutionProvider"]
    )
    wanted = {tensor.name for tensor in session.get_inputs()}
    logger.info(
        "model ready in %.1fs, columns %s", time.perf_counter() - started, labels
    )

    def infer(encodings: list[Any]) -> Any:
        """One chunk of already-tokenized rows, padded to its own longest."""
        width = max(len(encoding.ids) for encoding in encodings)
        shape = (len(encodings), width)
        input_ids = np.full(shape, pad_id, dtype=np.int64)
        attention_mask = np.zeros(shape, dtype=np.int64)
        token_type_ids = np.zeros(shape, dtype=np.int64)
        for row, encoding in enumerate(encodings):
            length = len(encoding.ids)
            input_ids[row, :length] = encoding.ids
            attention_mask[row, :length] = encoding.attention_mask
            token_type_ids[row, :length] = encoding.type_ids

        feed = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }
        logits = session.run(
            ["logits"], {name: value for name, value in feed.items() if name in wanted}
        )[0]
        # Softmax, shifted by the row maximum so a large logit cannot overflow
        # the exponential.
        shifted = np.exp(logits - logits.max(axis=-1, keepdims=True))
        return shifted / shifted.sum(axis=-1, keepdims=True)

    column = {label: index for index, label in enumerate(labels)}

    def run(texts: Sequence[str]) -> list[Sentiment]:
        # Tokenized once for the whole request: the counts decide both the order
        # and the chunking, and re-encoding per chunk would tokenize every text
        # twice to learn something already known.
        encoded = tokenizer.encode_batch(list(texts))
        out: list[Sentiment | None] = [None] * len(texts)
        for chunk in plan_chunks([len(encoding.ids) for encoding in encoded]):
            for position, row in zip(chunk, infer([encoded[i] for i in chunk])):
                out[position] = Sentiment(
                    positive=float(row[column["positive"]]),
                    negative=float(row[column["negative"]]),
                    neutral=float(row[column["neutral"]]),
                )
        # Every position was filled: the chunks partition the indices.
        return [reading for reading in out if reading is not None]

    return run, labels


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # A half-open socket must not pin a worker thread indefinitely. The body
    # read raises this for its own duration, then puts it straight back.
    timeout = 5

    # How long to wait for the request body specifically. Generous next to the
    # class timeout because an upload crosses the network and a half-open
    # socket does not.
    body_timeout = 30

    server_version = "screener-sentiment"
    sys_version = ""

    scorer: Scorer
    labels: tuple[str, ...]

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

    def _texts(self) -> list[str] | None:
        """The texts to score, or `None` having already answered.

        Under HTTP/1.1 an unread body is the next request as far as the
        connection is concerned, so every rejection here closes rather than
        leaving half a payload to be parsed as a request line.
        """
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.strip().isdigit():
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "no texts")
            return None
        length = int(raw)
        if length == 0:
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "no texts")
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._fail(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body over {MAX_BODY_BYTES} bytes"
            )
            return None

        self.connection.settimeout(self.body_timeout)
        try:
            body = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "could not read the body")
            return None
        finally:
            self.connection.settimeout(self.timeout)

        if len(body) != length:
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "the upload ended early")
            return None

        try:
            payload = json.loads(body)
        except ValueError:
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "the body is not JSON")
            return None

        texts = payload.get("texts") if isinstance(payload, dict) else None
        if not isinstance(texts, list) or not texts:
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "no texts")
            return None
        if len(texts) > MAX_TEXTS:
            self.close_connection = True
            self._fail(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"over {MAX_TEXTS} texts"
            )
            return None
        if not all(isinstance(text, str) and text.strip() for text in texts):
            # Refused rather than scored as neutral. A blank string put through
            # a classifier produces three confident numbers about nothing, and
            # the client already drops blanks before sending, so one arriving
            # here is a caller doing something it did not mean to.
            self.close_connection = True
            self._fail(HTTPStatus.BAD_REQUEST, "every text must be non-empty")
            return None
        return [text[:MAX_CHARS] for text in texts]

    def do_GET(self) -> None:
        if self.path == "/health":
            # Touches neither the model nor the semaphore: this answers while a
            # batch is running, which is the whole reason for threading.
            self._respond(HTTPStatus.OK, {"status": "ok", "labels": list(self.labels)})
        else:
            self._fail(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        if self.path != "/score":
            self._fail(HTTPStatus.NOT_FOUND, "not found")
            return

        texts = self._texts()
        if texts is None:
            return

        if not _SCORING.acquire(timeout=BUSY_WAIT_SECONDS):
            self._fail(HTTPStatus.SERVICE_UNAVAILABLE, "busy")
            return
        try:
            started = time.perf_counter()
            readings = list(self.scorer(texts))
        except Exception as exc:
            logger.warning(
                "could not score %d texts: %s", len(texts), type(exc).__name__
            )
            self._fail(HTTPStatus.UNPROCESSABLE_ENTITY, "could not score the texts")
            return
        finally:
            _SCORING.release()

        if len(readings) != len(texts):
            # One reading per text is the contract the client checks its
            # response against, and a caller lining readings up with database
            # rows would otherwise attribute every one of them to the wrong row.
            logger.error("scored %d texts into %d readings", len(texts), len(readings))
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, "the batch came back short")
            return

        elapsed = time.perf_counter() - started
        logger.info(
            "scored %d texts in %.1fs, %.1f/s", len(texts), elapsed, len(texts) / elapsed
        )
        self._respond(
            HTTPStatus.OK,
            {
                # The column order travels with the numbers, so the client maps
                # rather than assumes. It is the only thing in this response
                # that a caller cannot recompute.
                "labels": list(self.labels),
                "scores": [
                    [getattr(reading, label) for label in self.labels]
                    for reading in readings
                ],
            },
        )


def build_server(
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    *,
    scorer: Scorer | None = None,
    labels: tuple[str, ...] | None = None,
) -> ThreadingHTTPServer:
    """A server bound to `host:port`, not yet serving.

    `scorer` is the test seam and `serve` does not use it: it loads the real
    model and hands both halves in. A test passes a plain function, which is how
    every route here is exercised in a CI that does not install a BERT.

    **The two arguments travel together on purpose.** An earlier version let
    `scorer` default to `load_model()` while `labels` defaulted to the constant
    in `client`, which put the exact failure this module is built to prevent
    into its own constructor: a server that loaded one model's columns and
    announced another's. Loading the model without taking its column order with
    it is now impossible to express — omit `scorer` and the labels come from the
    same call, pass `scorer` and you must say which columns it speaks.
    """
    if scorer is None:
        scorer, loaded = load_model()
        labels = labels or loaded
    return ThreadingHTTPServer(
        (host, port),
        type(
            "BoundHandler",
            (Handler,),
            {"scorer": staticmethod(scorer), "labels": labels or LABELS},
        ),
    )


def serve(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    """Load the weights, then serve until SIGTERM or SIGINT.

    The load happens before the socket opens, so a container that is up is one
    whose weights are in memory and whose columns have been read. That is what
    makes the compose healthcheck and the deploy smoke test mean anything.
    """
    scorer, labels = load_model()
    server = build_server(host, port, scorer=scorer, labels=labels)

    def stop(*_: Any) -> None:
        # `shutdown()` blocks until `serve_forever()` returns, which it cannot
        # do while the handler that called it is still on the stack. This is
        # PID 1 in the container, so this is the difference between a clean
        # `compose down` and a ten-second SIGKILL wait on every deploy.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info("sentiment serving on %s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        logger.info("sentiment stopped")
