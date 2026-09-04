"""The individual ways of making a request.

Every strategy takes the same arguments and returns a `FetchResult`. Adding one
means writing that function and putting it in `STRATEGIES`; nothing else
changes, which is the point of the registry.

The `transport` argument exists so tests can pass `httpx.MockTransport` and
exercise the real client code without a network, a credential, or a mocking
library. Production never passes it.
"""

import json
import secrets as token_source
import urllib.parse
from collections.abc import Mapping
from typing import Protocol

import httpx

from screener.fetch.config import ProxyConfig
from screener.fetch.result import FetchResult, StrategyUnavailable

# A default browser fingerprint. Not evasion — several public endpoints return
# a different or empty representation to a client that sends no User-Agent at
# all, and getting the same bytes a browser gets makes a parser reproducible.
BROWSER_HEADERS: Mapping[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

UNLOCKER_URL = "https://api.brightdata.com/request"


class Strategy(Protocol):
    def __call__(
        self,
        url: str,
        timeout: float,
        headers: Mapping[str, str],
        transport: httpx.BaseTransport | None = None,
    ) -> FetchResult: ...


def direct(
    url: str,
    timeout: float,
    headers: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    """A plain request from wherever this process happens to run."""
    with httpx.Client(
        follow_redirects=True, timeout=timeout, transport=transport
    ) as client:
        response = client.get(url, headers=dict(headers))
        response.raise_for_status()
        return FetchResult(
            text=response.text,
            status_code=response.status_code,
            strategy="direct",
            url=url,
        )


def isp_proxy(
    url: str,
    timeout: float,
    headers: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    """A request through a Bright Data ISP proxy, on a fresh session.

    A new session suffix per request draws a different exit IP, which only
    helps if the zone holds more than one — with a single-IP zone this is an
    ordinary request that happens to cost bandwidth.
    """
    config = ProxyConfig.from_env()
    proxy = config.proxy_url(session=token_source.token_hex(4))
    if proxy is None:
        raise StrategyUnavailable("isp_proxy: no Bright Data proxy credentials set")

    # verify=False because Bright Data terminates TLS on its own chain: the
    # certificate presented is theirs, not the origin's, so validating it
    # against the origin hostname fails by construction. The alternative is to
    # ship their CA and pass verify="<path>", which is worth doing if this
    # strategy ever carries anything but public market data.
    #
    # `transport` wins when supplied, because a MockTransport under test must
    # not also try to dial a proxy.
    with httpx.Client(
        proxy=None if transport is not None else proxy,
        verify=False,
        follow_redirects=True,
        timeout=timeout,
        transport=transport,
    ) as client:
        response = client.get(url, headers=dict(headers))
        response.raise_for_status()
        return FetchResult(
            text=response.text,
            status_code=response.status_code,
            strategy="isp_proxy",
            url=url,
        )


def unlocker(
    url: str,
    timeout: float,
    headers: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    """Bright Data's Web Unlocker, billed per successful request.

    Last in any chain that names it: it is the only strategy with a per-call
    price, so it should never be reached while a cheaper one still works.
    """
    config = ProxyConfig.from_env()
    if not config.unlocker_enabled:
        raise StrategyUnavailable("unlocker: no Bright Data API key or zone set")

    payload = json.dumps(
        {"zone": config.unlocker_zone, "url": url, "format": "raw"}
    ).encode()
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(
            UNLOCKER_URL,
            content=payload,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return FetchResult(
            text=response.text,
            status_code=response.status_code,
            strategy="unlocker",
            url=url,
        )


STRATEGIES: dict[str, Strategy] = {
    "direct": direct,
    "isp_proxy": isp_proxy,
    "unlocker": unlocker,
}


def redact(url: str) -> str:
    """A URL with its query string dropped, safe to put in a log or an error.

    API keys travel as query parameters often enough that echoing a full URL
    into an exception message is a reliable way to leak one into a log
    aggregator.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitise(exc: BaseException, url: str) -> str:
    """An exception message with the request URL's query string removed.

    Redacting only the URL we format ourselves is not enough: httpx puts the
    full URL into `raise_for_status()`'s message, so a 403 on a request
    carrying an API key would carry that key into the log by itself. The query
    is replaced separately from the whole URL because a client may render the
    URL slightly differently from the string it was given.
    """
    text = str(exc)
    text = text.replace(url, redact(url))
    query = urllib.parse.urlsplit(url).query
    if query:
        text = text.replace(query, "<redacted>")
    return text
