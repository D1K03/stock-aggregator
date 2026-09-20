"""The decision model, reached through OpenRouter. `httpx` and nothing else.

Jev is not a language model and this is not `screener.ai`. It generates no text
at all: it takes program state plus typed questions and returns typed answers
with a probability distribution attached to each. That is why it is here rather
than beside `complete` and `converse` -- a different endpoint, a different
request shape, a different response shape, and a rule in `DESIGN.md` that
applies to one and not the other.

**It never emits a sentiment number, and nothing here asks it for one.** Tone is
FinBERT's and only FinBERT's, which is what keeps `DESIGN.md`'s rule -- "an LLM
never emits a score" -- standing unchanged rather than reversed. Every question
in `questions.py` is a `choice` or a `noul`: which company, what kind of claim,
and three gates. The model's own `score` primitive is the worst-calibrated of
the three out of distribution and is deliberately unused.

**Through OpenRouter rather than TypeSafe directly**, so there is one key, one
bill and one dashboard rather than two -- and so `usage.cost` comes back from
the same place `screener.ai` already takes it from. A local price table is wrong
the first time a rate changes and silently wrong after that. The transport is a
separate alpha endpoint rather than chat completions, because a model that
returns decisions does not fit a response shape built around a string.

Hand-rolled over a five-field POST rather than taking TypeSafe's SDK, which does
not support OpenRouter anyway. Same call `screener.blobs` makes about SigV4 and
`screener.mcp` about the MCP SDK.
"""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from screener.ai import RouterConfig

logger = logging.getLogger(__name__)

# Not `/api/v1/chat/completions`. OpenRouter serves decision models through an
# endpoint of their own, still marked alpha.
API_URL = "https://openrouter.ai/api/alpha/decisions"

# Pinned rather than `~typesafe/jev-latest`. A resolution is evidence, the model
# is part of the key on every row that stores one, and a floating alias would
# silently change what a stored decision means. Upgrading is an edit here and a
# re-read of whatever needs re-reading.
MODEL = "typesafe/jev-1.13"

# Published as 70-500ms end to end. Thirty seconds is not a performance budget,
# it is the point at which something is wrong -- and it wants to stay well under
# the pass's own patience so a wedged call costs one item rather than a night.
DEFAULT_TIMEOUT = 30.0

# 32k on OpenRouter for state plus the longest question, so a text is capped far
# below that: a Reddit comment has a 9-word median and an article that needs
# 30,000 tokens to say which company it is about is not a case worth paying for.
# This is characters, applied by the caller through `candidates.excerpt`.
MAX_STATE_CHARS = 6_000

# 429 is the published rate limit (1,200/min) and 529 is the model being
# overloaded. Both are documented as wanting backoff rather than an immediate
# retry. Bounded low because a pass has thousands of items and the right answer
# to sustained refusal is to stop, not to keep asking politely.
RETRY_STATUSES = frozenset({429, 529})
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0


class DecideError(RuntimeError):
    """The decision could not be made, or came back unusable."""


class Throttled(DecideError):
    """The model refused for a reason that will pass. Stop the pass, not the item."""


@dataclass(frozen=True, slots=True)
class Choice:
    """One `choice` answer: the winner, the field, and how peaked it is.

    The distribution is kept beside the winner rather than collapsed into it,
    for the same reason `screener.sentiment.Sentiment` keeps three probabilities
    rather than one number. Here it matters more: an independent calibration
    test measured this model overconfident on `choice` out of distribution, so
    the confidence is a reading to threshold rather than a fact to trust, and
    only the distribution lets a threshold be re-cut without re-deciding.
    """

    chosen: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class Decision:
    """Every answer to one request, and what it cost."""

    choices: Mapping[str, Choice]
    nouls: Mapping[str, float]
    model: str
    input_tokens: int
    cost_usd: float


def decide(
    *,
    state: Any,
    questions: Mapping[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
    sleep=None,
) -> Decision:
    """Put one state and its questions to the model, and return the answers.

    Raises `DecideError` on anything unusable and `Throttled` on a refusal the
    caller should back off from. Deliberately *does* raise, unlike
    `screener.sentiment.score` which never does: a sentiment reading is one
    optional column on a row that is still worth writing, and a resolution is
    the row.

    `transport` and `sleep` exist so tests can exercise this without a key, a
    network or a wait. Production passes neither.
    """
    config = RouterConfig.from_env()
    if config.api_key is None:
        raise DecideError("OPENROUTER_API_KEY is not set")

    pause = sleep or time.sleep
    payload = {"model": MODEL, "state": state, "questions": dict(questions)}
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        # The same attribution `screener.ai` sends, so spend on this endpoint is
        # traceable to this project on the OpenRouter dashboard rather than
        # appearing as a bare key doing something unexplained.
        "HTTP-Referer": config.app_base_url,
        "X-Title": "stock-aggregator",
    }

    response: httpx.Response | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with httpx.Client(timeout=timeout, transport=transport) as client:
                response = client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise DecideError(f"decision request failed: {exc}") from exc
        if response.status_code not in RETRY_STATUSES:
            break
        if attempt == RETRY_ATTEMPTS - 1:
            # Out of patience, and this is the one failure the caller must treat
            # as "stop asking" rather than "this item did not work".
            raise Throttled(f"decisions endpoint returned HTTP {response.status_code}")
        pause(RETRY_DELAY * (attempt + 1))

    assert response is not None
    if response.status_code >= 400:
        raise DecideError(f"decisions endpoint returned HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise DecideError("decisions endpoint did not return JSON") from exc
    if not isinstance(body, dict):
        raise DecideError("decisions endpoint did not return an object")
    if "error" in body:
        # A 200 carrying an error object, which is how OpenRouter reports an
        # upstream provider failure. Status alone is not enough, exactly as in
        # `screener.ai.converse`.
        raise DecideError(f"decisions endpoint reported an error: {body['error']}")

    answers = body.get("answers")
    if not isinstance(answers, dict):
        raise DecideError("decisions response carried no answers")

    choices: dict[str, Choice] = {}
    nouls: dict[str, float] = {}
    for key, answer in answers.items():
        if not isinstance(answer, dict):
            raise DecideError(f"answer {key!r} is not an object")
        kind = answer.get("type")
        if kind == "noul":
            value = answer.get("noul")
            if not isinstance(value, int | float):
                raise DecideError(f"answer {key!r} carries no probability")
            nouls[str(key)] = float(value)
        elif kind == "choice":
            parsed = _choice(answer)
            if parsed is None:
                # **Raised rather than skipped**, which is the opposite of what
                # `screener.ai.converse` does with an unparseable tool call, and
                # the difference is what a missing answer would mean. There, the
                # loop simply has one fewer result. Here, an absent `which` is
                # read downstream as "this text is about no security" -- so
                # dropping a malformed one would quietly reclassify every item
                # in the pass as about nothing, with a plausible row for each.
                raise DecideError(f"answer {key!r} is not a usable choice")
            choices[str(key)] = parsed

    if not choices and not nouls:
        raise DecideError("decisions response carried no usable answers")

    usage = body.get("usage") or {}
    return Decision(
        choices=choices,
        nouls=nouls,
        # The response names the exact build -- `typesafe/jev-1.13-20260917`
        # where the request said `typesafe/jev-1.13`. That is what gets stored,
        # because it is what actually decided.
        model=str(body.get("model") or MODEL),
        input_tokens=int(usage.get("input_tokens") or 0),
        cost_usd=float(usage.get("cost") or 0.0),
    )


def _choice(answer: Mapping[str, Any]) -> Choice | None:
    """One choice answer, or None if it is not shaped like one.

    The probabilities are required rather than optional. A winner without its
    distribution is exactly the "confident number with nothing behind it" that
    `DESIGN.md` refuses, and silently accepting one would let a change at the
    provider quietly strip the evidence off every row written after it.
    """
    chosen = answer.get("choice")
    probabilities = answer.get("probabilities")
    confidence = answer.get("confidence")
    if not isinstance(chosen, str) or not isinstance(probabilities, dict):
        return None
    if not isinstance(confidence, int | float):
        return None
    try:
        spread = {str(k): float(v) for k, v in probabilities.items()}
    except (TypeError, ValueError):
        return None
    if chosen not in spread:
        # The winner has to be one of the options it was weighed against, or
        # the distribution is not a distribution over this question.
        return None
    return Choice(chosen=chosen, probabilities=spread, confidence=float(confidence))
