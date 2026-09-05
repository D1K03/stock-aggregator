"""Social ingest: posts and comments, for the Sentiment pillar.

Two halves that share nothing but a dataclass. `source` talks to the network and
never opens a database connection; `store` writes rows and never opens a socket.
That is the split `screener.universe` already draws between `refresh` and
`load`, and it is what lets either half be tested without the other.

**Not Reddit's own API**, and not a scraper either. Reddit answers an
unauthenticated request with 403 whatever User-Agent is sent, its robots.txt is
`Disallow: /` for every agent, and an OAuth client needs manual approval that
can take a month and can be refused. `CLAUDE.md` says scrapers respect
robots.txt, so this reads a public mirror instead — which also has the
date-range search that Reddit's own thousand-item listing cap does not, and
without which a week of r/wallstreetbets is unreachable.

Ingest only. Nothing here connects an item to a security or scores it: a post
mentions zero, one or many tickers, and deciding which is its own piece of work.
"""

from screener.reddit.config import RedditConfig
from screener.reddit.ingest import Report, once
from screener.reddit.source import Item, SourceError

__all__ = ["Item", "RedditConfig", "Report", "SourceError", "once"]
