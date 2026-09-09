"""The tool that fetches an article and keeps it.

Storage is the point. A tool result is 400 characters and is re-sent as context
on every following round, so an article cannot come back through one — what
comes back is what the document *is*: the headline, where it is from, when it
was published, how long it is, and the id to read it by. The text goes to
`magpie.document`, which Steven can read with `sql` and which is on `/playground`
for anyone who wants the whole thing.

Asked over HTTP rather than done here, so the bot image never carries a
parser: the container that scrapes is the only process holding lxml, and the
only one holding the proxy credentials that let a fetch cost anything.

This is the first tool here that can spend money, and the only one. The billed
rung is reached only when both free routes have failed, it is metered against a
daily count of its own, and the cost lands on the audit trail — but deliberately
not on the chat spend cap, because a scrape is forty times a reply and putting
them on one counter would let scraping silence the assistant.
"""

from screener.bot.tools.registry import actor, tool
from screener.magpie.client import scrape as ask_magpie

# What is left of the tool budget once the metadata is written. Enough to tell
# whether the right page was fetched, and nowhere near enough to be the article.
LEAD = 180

_WHY = {
    "robots": "the site's robots.txt says no, so I did not fetch it — that is final",
    "paywall": "the publisher marks it as not free to read, so I did not pay a proxy to get past that",
    "not_a_page": "that address is a file rather than a web page",
    "too_short": "the page came back but there was no article in it",
    "all_strategies_failed": "every route to it failed",
    "restarted": "the scraper restarted before it could fetch that, so it is worth another go",
    "not_stored": "I reached the page but could not keep it, which is a fault our end rather than the site's",
    "unlocker_capped": "every free route failed and the paid one is over its daily limit",
    "unavailable": "the store is not reachable",
}


@tool("scrape", "Fetch an article by URL and keep it. Returns what it is, not the text.")
def scrape(url: str) -> str:
    """Fetch, extract and store one page.

    Who asked comes from `actor()` rather than from an argument: the model does
    not know who it is talking to and must not be told, because a name in the
    arguments is a name it could invent.
    """
    who, _ = actor()
    result = ask_magpie(url, requested_by=who)

    if not result.ok:
        reason = result.reason or "failed"
        return f"not stored: {_WHY.get(reason, reason)}"

    document = result.document
    assert document is not None
    when = document.get("published") or "undated"
    verb = "stored" if document.get("stored") else "already had it, unchanged"
    head = (
        f"#{document['id']} {verb}: {document['title']!r} — {document['host']}, {when}, "
        f"{document['word_count']} words, via {document['strategy']}."
    )
    return f"{head} {(document.get('lead') or '')[:LEAD]}…"
