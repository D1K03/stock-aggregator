"""HTML into an article. Never opens a socket.

trafilatura does the boilerplate removal, which is the part worth having a
library for: a headline, a byline, a date and the body without the navigation,
the cookie banner and the related-articles rail. It is called only on a string
that has already been fetched — `bare_extraction` does no network I/O at all.

**The library's own downloader is never imported here, and a test enforces it.**
`trafilatura.fetch_url` would work perfectly well and would silently bypass both
the proxy ladder and the spend cap, which is the kind of failure nothing else
would notice: the article would arrive, the cost would not be recorded, and the
strategy column would be a lie.
"""

import logging
from datetime import date
from typing import cast

from screener.magpie.models import Extracted

logger = logging.getLogger(__name__)

# Below this, a page is not an article. A cookie wall, a challenge interstitial
# and a "page not found" all extract to a sentence or two, and storing one as a
# document is worse than storing nothing: it looks like a successful scrape.
MIN_WORDS = 40


class NotAnArticle(RuntimeError):
    """The HTML parsed, but nothing article-shaped came out of it."""


def _as_date(raw: object) -> date | None:
    """trafilatura returns `YYYY-MM-DD` or nothing. Anything else is dropped."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def extract(html: str, *, url: str | None = None) -> Extracted:
    """Pull the article out of one page. Raises `NotAnArticle` if there is none.

    `url` is passed through only so trafilatura can resolve a relative canonical
    link; it is not fetched.
    """
    # Imported here rather than at module scope so the import cost lands on the
    # one process that extracts, and so `screener.magpie.client` stays httpx.
    # Installed by the `scrape` extra, which only this container takes.
    from trafilatura import bare_extraction  # pyright: ignore[reportMissingImports]

    try:
        parsed = bare_extraction(html, url=url, with_metadata=True)
    except Exception as exc:
        # Broad because trafilatura raises whatever lxml raises, and a page that
        # will not parse is an ordinary outcome rather than an incident.
        raise NotAnArticle(f"could not parse: {type(exc).__name__}") from exc

    if parsed is None:
        raise NotAnArticle("nothing article-shaped in the page")

    # `as_dict` has existed since 1.x and is what the library documents; the
    # cast is because trafilatura ships no type information, so pyright infers a
    # `dict[bytes, ...]` and every lookup below becomes an error about a key
    # type nobody wrote.
    # `bare_extraction` is declared as returning `Document | dict`, and the
    # dict arm is the one pyright resolves against. Both shapes are handled,
    # and the cast is because trafilatura ships no type information — without
    # it every lookup below is an error about a key type nobody wrote.
    raw = parsed if isinstance(parsed, dict) else parsed.as_dict()
    fields = cast(dict[str, object], raw)
    text = _text(fields.get("text")) or ""
    if len(text.split()) < MIN_WORDS:
        raise NotAnArticle(f"only {len(text.split())} words; not an article")

    return Extracted(
        title=_text(fields.get("title")) or "",
        text=text,
        author=_text(fields.get("author")),
        published=_as_date(fields.get("date")),
        language=_text(fields.get("language")),
        canonical_url=_text(fields.get("url")),
    )
