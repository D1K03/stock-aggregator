"""The places a document points at.

Read from the page already in the blob store, so this opens no socket at all.
The whole feature is a parse of bytes we kept the first time.

**The links are the article's, not the site's.** Walking every `<a href>` on a
Wikipedia page returns 248 external links whose most frequent are "Donate",
"Privacy Policy", "Edit links" and the interwiki language list, none of which
the article has anything to do with. trafilatura's extracted `body` is the same
tree the text comes from, with the navigation, the footer and the sidebars
already removed, and its `ref` elements are the links the article itself makes.
On that same page it turns 248 into 214 real citations. The boilerplate removal
that earns the library its place earns it twice.
"""

import logging
from collections import Counter
from typing import Any, cast
from urllib.parse import urljoin

from screener.magpie.models import Reference
from screener.magpie.urls import canonical, host_of, is_http, looks_binary

logger = logging.getLogger(__name__)

# Enough of the link's own words to say what it is. A citation's anchor is often
# a full title, and the rest is not worth a column that wide.
MAX_ANCHOR = 160


def _words(element: Any) -> str:
    """An element's text. `itertext`, because these are etree nodes, not html."""
    return " ".join("".join(element.itertext()).split())


def references(html: str, url: str) -> list[Reference]:
    """Every other site this page points at, in the order it points at them.

    Same-host links are dropped: a page linking to its own site is navigation
    however deep in the article it sits, and what is wanted here is the places
    it sends you *away* to.

    Never raises. A page that will not parse has no references, which is the
    same answer as a page that makes none, and neither is worth failing a page
    view over.
    """
    try:
        from trafilatura import bare_extraction

        parsed = bare_extraction(html, url=url, with_metadata=True, include_links=True)
    except Exception as exc:
        logger.warning("could not read links from %s: %s", url[:80], type(exc).__name__)
        return []

    body = getattr(parsed, "body", None) if parsed is not None else None
    if body is None:
        return []

    here = host_of(url)
    counts: Counter[str] = Counter()
    anchors: dict[str, str] = {}

    for element in cast(Any, body).xpath(".//ref[@target]"):
        target = (element.get("target") or "").strip()
        if not target:
            continue
        # Resolved against the page, because a citation is as likely to be
        # written relative as absolute.
        absolute = urljoin(url, target)
        if not is_http(absolute) or looks_binary(absolute):
            continue

        key = canonical(absolute)
        if not key or host_of(key) == here:
            continue

        counts[key] += 1
        if key not in anchors:
            # The first mention's words. Later ones are usually "ibid", "op.
            # cit." or the bare "Archived" that follows an archive link.
            anchors[key] = _words(element)[:MAX_ANCHOR]

    return [
        Reference(url=key, host=host_of(key), anchor=anchors.get(key, ""), occurrences=n)
        for key, n in counts.most_common()
    ]
