"""What a fetch returns, and what it raises."""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class FetchResult:
    """One successful response, and how it was obtained.

    `strategy` is carried because a run that quietly fell back to a proxy costs
    money and says something about the source; recording which path served the
    request is the difference between noticing that and not. `attempts` keeps
    the ones that failed first, so a log line can say "got it via unlocker
    after direct returned 403" rather than just naming the winner.
    """

    text: str
    status_code: int
    strategy: str
    url: str
    attempts: tuple[str, ...] = field(default_factory=tuple)

    def json(self) -> Any:
        return json.loads(self.text)


class FetchError(RuntimeError):
    """Every strategy failed. The message names what each one did."""


class StrategyUnavailable(RuntimeError):
    """A strategy was named but is not configured.

    Raised rather than skipped so it lands in the chain's error list beside
    genuine failures: "isp_proxy: no credentials" is a far more useful thing to
    read in a log than a silent omission.
    """


class EmptyResponse(RuntimeError):
    """A 2xx with no body.

    Its own type because it is usually not an empty result — more than one
    provider answers a rate-limited request this way, and treating it as "no
    data" writes a bogus observation into an append-only fact layer.
    """
