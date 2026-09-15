"""Asking the sentiment service how a piece of text reads.

`httpx` and nothing else, so the nightly pipeline, the status service and the
bot can import this without any of them paying for onnxruntime, numpy and a
438 MB BERT to make one HTTP call. The model lives in
`screener.sentiment.server`, which only its own container runs.

Shaped after `screener.transcribe.client`, which reaches its own model container
the same way and for the same reason: the work belongs somewhere else, the call
has to be allowed to fail, and a failure must not take the caller down with it.

**The three probabilities travel, not just the number.** `score` is
`positive - negative` and is derived here rather than sent, so there is exactly
one definition of it and a stored reading always carries the raw inputs it came
from. That is the same rule the pillars follow: a percentile is kept beside the
metric it ranks, because a score that moved is a different fact from a metric
that moved.
"""

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Where the sentiment container answers inside the compose network. 8080 is the
# status service, 8081 the transcriber, 8082 the scraper. Overridable because
# "sentiment" is a service name, which only means anything in that network.
DEFAULT_SCORER = "http://sentiment:8083/score"

# The weights are loaded before the service opens its socket, so this covers
# inference only — and it is derived from MAX_TEXTS below rather than chosen.
# Measured on the VPS at two threads: ~0.9 texts/sec at the 512-token cap, so a
# full batch of the slowest legal input is roughly 70 seconds, and a request
# that waited its turn behind another adds BUSY_WAIT_SECONDS to that. Three
# minutes covers both with room to spare.
#
# The two numbers have to be kept in step. A timeout shorter than the slowest
# legal batch is the worst possible arrangement: the caller gives up, retries,
# and the service scores the abandoned batch anyway — so the queue grows while
# every caller sees a failure.
TIMEOUT_SECONDS = 180.0

# Separately, and much shorter: a wrong service name should fail in seconds
# rather than in three minutes of them.
CONNECT_TIMEOUT_SECONDS = 3.0

# How many texts one call may carry.
#
# **Measured, not chosen.** The first draft said 256, which is wrong on this
# hardware in a way that only shows up under real input: the VPS scores ~0.9
# texts/sec at the 512-token cap, so 256 long Reddit comments is nearly five
# minutes and every caller would time out while the service kept burning CPU on
# a batch nobody was waiting for any more. Sixty-four is about seventy seconds
# of the slowest legal input and under four seconds of the fastest.
#
# Enforced here rather than by chunking silently, because the service scores a
# batch under a semaphore of one: a request is also a decision about how long to
# hold it, and a caller with sixty headlines and a caller with a week of
# r/wallstreetbets want different answers to that. Same reason
# `screener.transcribe` leaves its two-minute cap to the callers, who know the
# length of the clip before any bytes move.
MAX_TEXTS = 64

# Past this, nothing more is read anyway: FinBERT sees 512 word pieces and the
# tokenizer truncates. This is roughly four times that in characters, so the
# truncation stays the model's decision rather than a silent cut here, and a
# request still cannot carry a megabyte per item.
MAX_CHARS = 4_000

# What FinBERT emits, in the order this client presents it. The service sends
# its own column order alongside the numbers, so this is a presentation
# decision and never a mapping one.
LABELS = ("positive", "negative", "neutral")


@dataclass(frozen=True, slots=True)
class Sentiment:
    """How one text reads: three probabilities that sum to one.

    Kept whole rather than reduced to `score` on the way in. "Confidently
    neutral" and "torn between positive and negative" both come out near zero
    and are not the same reading, and only the distribution can tell them apart.
    """

    positive: float
    negative: float
    neutral: float

    @property
    def score(self) -> float:
        """Polarity in [-1, 1], positive minus negative.

        Derived, never transmitted. One definition, computed where the inputs
        are, so a persisted score and its probabilities cannot disagree.
        """
        return self.positive - self.negative

    @property
    def label(self) -> str:
        """Whichever of the three the model put most weight on."""
        return max(zip(LABELS, (self.positive, self.negative, self.neutral)),
                   key=lambda pair: pair[1])[0]


def scorer_url() -> str:
    return os.environ.get("SENTIMENT_URL", DEFAULT_SCORER)


def score(
    texts: Sequence[str],
    *,
    client: httpx.Client | None = None,
) -> tuple[Sentiment | None, ...] | None:
    """One reading per text, in order, or `None` if nothing could be scored.

    Never raises.

    **Two different absences, deliberately kept apart.** `None` for the whole
    call means the service could not be reached or did not answer with readings
    — nothing was scored and the caller should come back later. `None` in a
    position means that text had nothing to score: it was blank, and a blank
    string put through a classifier still produces three confident-looking
    numbers. Collapsing the two would make "the scorer is down" indistinguishable
    from "nobody posted", which is the shape of every silent failure in this
    project.

    An empty `texts` is an empty answer, not an error, and costs no request.
    """
    if not texts:
        return ()
    if len(texts) > MAX_TEXTS:
        logger.warning("refusing %d texts, over the cap of %d", len(texts), MAX_TEXTS)
        return None

    # Blanks never leave, so the cap and the timeout are spent on real text, and
    # the service is never asked to have an opinion about "".
    prepared = [text.strip()[:MAX_CHARS] for text in texts]
    wanted = [i for i, text in enumerate(prepared) if text]
    if not wanted:
        return tuple(None for _ in prepared)

    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    )
    try:
        response = client.post(
            scorer_url(), json={"texts": [prepared[i] for i in wanted]}
        )
        response.raise_for_status()
        # The mirror of the PNG sniff in `render` and the transcript sniff in
        # `transcribe`: a 200 carrying a sign-in page would otherwise be parsed
        # into something that looks like a reading.
        body = json.loads(response.text)
        readings = _readings(body, len(wanted))
        if readings is None:
            logger.warning(
                "sentiment service returned %s, not readings",
                response.headers.get("content-type"),
            )
            return None
    except Exception as exc:
        logger.warning("could not score %d texts: %s", len(wanted), type(exc).__name__)
        return None
    finally:
        if owned:
            client.close()

    out: list[Sentiment | None] = [None] * len(prepared)
    for position, reading in zip(wanted, readings):
        out[position] = reading
    return tuple(out)


def _readings(body: object, expected: int) -> list[Sentiment] | None:
    """The readings from a response body, or `None` if it is not one.

    All or nothing, unlike `transcribe`'s tolerance of a segment list it cannot
    parse. There the words survive a dropped timing; here the reading *is* the
    numbers, and half a batch would be scored against peers that were not.

    The label order comes from the response rather than from `LABELS` above. It
    is written into the image beside the weights at export time, so a model
    whose columns are ordered differently is read correctly instead of being
    read backwards — which is the one failure here that produces a plausible
    number rather than an error.
    """
    if not isinstance(body, dict):
        return None
    labels = body.get("labels")
    scores = body.get("scores")
    if not isinstance(scores, list) or len(scores) != expected:
        return None
    if not isinstance(labels, list) or sorted(labels) != sorted(LABELS):
        return None

    found: list[Sentiment] = []
    for row in scores:
        if not isinstance(row, list) or len(row) != len(labels):
            return None
        try:
            by_label = {label: float(value) for label, value in zip(labels, row)}
        except (TypeError, ValueError):
            return None
        found.append(
            Sentiment(
                positive=by_label["positive"],
                negative=by_label["negative"],
                neutral=by_label["neutral"],
            )
        )
    return found
