"""What counts as the same page. Pure, and the most-tested file here.

A URL is a poor identity: the same article arrives with a tracking parameter
from an email, a fragment from a search result, and a capitalised host from
somebody's notes. Keying on the raw string makes one article three documents,
which is `screener.universe`'s lesson about matching on CIK rather than symbol —
match on the volatile key and one thing reads as several.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parameters that identify the referrer rather than the page.
#
# The prefixed families are matched as families rather than listed: a real ad
# link carries `gclid`, `gbraid`, `gclsrc`, `gad_source` and `gad_campaignid`
# together, and an earlier version of this caught the first two and let the rest
# through — so one landing page reached from two campaigns would have been two
# documents, which is the exact thing canonicalising exists to stop.
#
# Vendor-specific keys are deliberately not chased. `segmentID` on ft.com is
# real and there are thousands like it; a list of every publisher's own
# parameter is a list that is always out of date, and the cost of missing one is
# a duplicate row rather than a wrong answer.
_TRACKING = re.compile(
    r"^(utm_[a-z_]+|gad_[a-z_]+|_hs[a-z_]*|mkt_tok"
    r"|fbclid|gclid|gclsrc|gbraid|wbraid|dclid|msclkid|srsltid"
    r"|twclid|ttclid|li_fat_id|igshid|epik|yclid|spm"
    r"|mc_[ce]id|s_cid|cmpid|ito|ref|ref_src|referrer"
    r"|at_medium|at_campaign|__twitter_impression|_ga)$",
    re.IGNORECASE,
)

# Extensions that are not a page. `FetchResult` carries text and no
# Content-Type, so a PDF comes back as mojibake rather than as an error: the
# extractor finds nothing, the word floor fires, and the ladder escalates all
# the way to the billed rung to fail again. Refusing by path costs nothing and
# is the one place where the answer is not "escalate".
_BINARY = re.compile(
    r"\.(pdf|zip|gz|tar|docx?|xlsx?|pptx?|csv|mp[34]|m4a|wav|avi|mov|mkv"
    r"|jpe?g|png|gif|webp|svg|ico|woff2?|ttf|exe|dmg|iso)$",
    re.IGNORECASE,
)


def canonical(url: str) -> str:
    """The address a document is keyed on.

    Fragment dropped, tracking parameters stripped, scheme and host lower-cased,
    a default port removed, remaining parameters sorted. Two links to the same
    article from an email and a tweet then agree, which is the difference
    between a key that means something and one that only prevents exact repeats.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80)
        or (parts.scheme == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"

    kept = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING.match(key)
    )
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(kept), ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_http(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def looks_binary(url: str) -> bool:
    """Whether the path ends in something that is not a web page."""
    return bool(_BINARY.search(urlsplit(url).path))
