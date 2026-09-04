"""The fallback chain: try each strategy in turn, return the first that works."""

import logging
from collections.abc import Callable, Mapping, Sequence

import httpx

from screener.fetch.result import EmptyResponse, FetchError, FetchResult
from screener.fetch.strategies import BROWSER_HEADERS, STRATEGIES, redact, sanitise

logger = logging.getLogger(__name__)

# Direct only. Every proxy path has to be asked for by name, so depending on
# Bright Data is a visible decision in a caller rather than something a default
# quietly turned on.
DEFAULT_STRATEGIES: tuple[str, ...] = ("direct",)

DEFAULT_TIMEOUT = 60.0


def fetch(
    url: str,
    strategies: Sequence[str] = DEFAULT_STRATEGIES,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    validate: Callable[[FetchResult], None] | None = None,
    allow_empty: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    """Fetch `url`, trying each strategy in order until one succeeds.

    There is no retry and no backoff inside a strategy. The chain is the retry:
    re-issuing the same request down the path that just failed rarely helps,
    and when it would, the next scheduled run picks it up anyway.

    A 2xx with an empty body is rejected and escalates to the next strategy,
    because more than one provider answers a rate-limited request that way and
    it is indistinguishable from "nothing to report". Sources where empty is a
    real answer pass `allow_empty=True`; the default is the safe direction,
    since the fact layer is append-only and a bogus empty observation is not
    something a later run corrects.

    `validate` adds a caller's own check on top, and raising from it has the
    same effect: demote the response and try the next strategy.
    """
    if not strategies:
        raise ValueError("fetch() needs at least one strategy")

    merged = {**BROWSER_HEADERS, **(headers or {})}
    errors: list[str] = []
    attempted: list[str] = []

    for name in strategies:
        strategy = STRATEGIES.get(name)
        if strategy is None:
            errors.append(f"{name}: unknown strategy")
            continue

        attempted.append(name)
        try:
            result = strategy(url, timeout, merged, transport)
            if not allow_empty and not result.text.strip():
                raise EmptyResponse(f"{name} returned {result.status_code} with no body")
            if validate is not None:
                validate(result)
        except Exception as exc:
            # Broad by design: the next strategy is the handler. A strategy
            # that fails for a reason nobody anticipated should still fall
            # through rather than take the whole run down.
            detail = sanitise(exc, url)
            errors.append(f"{name}: {type(exc).__name__}: {detail}")
            logger.warning(
                "fetch strategy %s failed for %s: %s", name, redact(url), detail
            )
            continue

        result.attempts = tuple(attempted)
        if name != strategies[0]:
            logger.warning("fetch fell back to %s for %s", name, redact(url))
        return result

    raise FetchError(f"all strategies failed for {redact(url)} — " + " | ".join(errors))
