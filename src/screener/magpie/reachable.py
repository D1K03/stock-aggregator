"""Where the scraper is allowed to go.

Magpie fetches an address somebody else chose. On the dashboard that is a link
you pasted; on a document page it is a link taken out of a page written by a
stranger, whose visible words say one thing and whose target says another. And
because the extracted body is stored and shown back, anything fetched is
readable — which is what turns "it made a request" into "it read something".

Nothing else in this project needed this, because every other caller fetches an
address the code itself chose: `screener.universe` knows it wants Wikipedia,
`screener.ingest` knows it wants Yahoo. This is the first place the destination
is the input.

**The check runs on every hop, not on the URL.** Validating what was submitted
and then handing it to a client that follows redirects checks an address nobody
fetches: a public URL can answer `302 Location: http://169.254.169.254/…` and
the request goes there with nothing having looked. `screener.fetch` takes an
`on_request` hook for exactly this, and `guard` is what belongs in it.

What this does not defend against is a name that passes here and resolves to
something else on the next lookup. Pinning the resolved address through the
request would close that, and it means replacing the transport rather than
inspecting it; recorded here as a known limit rather than left to look like an
oversight, because the shape of this file would be the same either way.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


class NotReachable(RuntimeError):
    """The address is not on the public internet, so we will not fetch it."""


def _private(address: str) -> bool:
    """Whether an address is somewhere only this network can see.

    `is_global` is deliberately not the test: it is false for a handful of
    ranges that are perfectly ordinary to fetch, and this list says which
    properties are being refused rather than delegating the decision.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # Not an address at all. The caller resolves names first, so reaching
        # here means something is wrong; refuse rather than guess.
        return True
    return (
        parsed.is_private          # 10/8, 172.16/12, 192.168/16, fc00::/7
        or parsed.is_loopback      # 127/8, ::1
        or parsed.is_link_local    # 169.254/16 — the cloud metadata address
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
    )


def addresses(host: str) -> list[str]:
    """Every address a name resolves to. Empty when it resolves to nothing."""
    try:
        found = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    # sockaddr is (host, port) for IPv4 and (host, port, flow, scope) for IPv6,
    # so the address is element zero either way and the rest is not wanted.
    return [str(info[4][0]) for info in found]


def check(url: str) -> None:
    """Raise `NotReachable` unless this address is on the public internet.

    **Every** address a name resolves to has to be public. A name answering with
    one public address and one private one is the ordinary way this check is
    got around, and taking the first answer would let it through half the time.
    """
    host = urlsplit(url).hostname
    if not host:
        raise NotReachable("no host in that address")

    # A literal needs no lookup, and passing one to getaddrinfo would accept
    # forms like `0x7f.1` that ip_address refuses.
    try:
        ipaddress.ip_address(host)
        found = [host]
    except ValueError:
        found = addresses(host)

    if not found:
        raise NotReachable(f"{host} does not resolve")

    private = [address for address in found if _private(address)]
    if private:
        # The address is in the message and the URL is not: a caller logs this,
        # and `screener.fetch` redacts query strings for a reason.
        raise NotReachable(f"{host} is not a public address ({private[0]})")


def guard(request: httpx.Request) -> None:
    """The hook `screener.fetch` runs before every request, redirects included."""
    url = str(request.url)
    try:
        check(url)
    except NotReachable:
        logger.warning("refused an internal address on %s", urlsplit(url).hostname)
        raise
