"""HTTP fetching with an ordered fallback chain.

Callers name the strategies they are willing to use, in order, and get back the
first result that succeeds along with which strategy produced it:

    result = fetch("https://example.com/api")                    # direct only
    result = fetch(url, ("direct", "isp_proxy"))                 # with fallback

The default is `("direct",)`. Bright Data is reachable only when a caller names
`isp_proxy` or `unlocker` *and* the credentials for it exist, which is what
keeps a proxy network out of the path of an API-first ingest that has no bot
problem to solve.
"""

from screener.fetch.chain import DEFAULT_STRATEGIES, DEFAULT_TIMEOUT, fetch
from screener.fetch.config import ProxyConfig
from screener.fetch.result import (
    EmptyResponse,
    FetchError,
    FetchResult,
    StrategyUnavailable,
)
from screener.fetch.strategies import BROWSER_HEADERS, STRATEGIES

__all__ = [
    "BROWSER_HEADERS",
    "DEFAULT_STRATEGIES",
    "DEFAULT_TIMEOUT",
    "EmptyResponse",
    "FetchError",
    "FetchResult",
    "ProxyConfig",
    "STRATEGIES",
    "StrategyUnavailable",
    "fetch",
]
