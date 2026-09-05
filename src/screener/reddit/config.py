"""Reddit ingest settings, owned by the ingest layer."""

from dataclasses import dataclass

from screener.config import env

# Arctic Shift, a public mirror of Reddit's own data. Reddit's API is not used:
# it answers an unauthenticated request with 403 whatever User-Agent is sent,
# its robots.txt is `Disallow: /` for every agent, and an OAuth client now needs
# manual approval. Scraping reddit.com anyway would be against this project's
# own rule that scrapers respect robots.txt and ToS.
DEFAULT_HOST = "https://arctic-shift.photon-reddit.com/api"

DEFAULT_SUBREDDITS = "wallstreetbets,stocks"
DEFAULT_BACKFILL_DAYS = 7
DEFAULT_REFRESH_HOURS = 6

# Between pages, in seconds. Arctic Shift publishes no rate limit and is run by
# volunteers, so this is politeness rather than compliance: fast enough that a
# week of the busiest subreddit backfills in half an hour, slow enough that we
# are not the reason it falls over.
DEFAULT_DELAY = 1.0


@dataclass(frozen=True)
class RedditConfig:
    """Where to fetch from, how far back, and how often.

    No credentials, so unlike every other config object here there is nothing to
    keep out of a repr and nothing that has to be in Infisical. Every field has a
    working default: the layer runs with none of these set.
    """

    host: str = DEFAULT_HOST
    subreddits: tuple[str, ...] = ("wallstreetbets", "stocks")
    backfill_days: int = DEFAULT_BACKFILL_DAYS
    refresh_hours: int = DEFAULT_REFRESH_HOURS
    delay: float = DEFAULT_DELAY

    @classmethod
    def from_env(cls) -> "RedditConfig":
        raw = env.text("REDDIT_SUBREDDITS", DEFAULT_SUBREDDITS)
        return cls(
            host=env.text("ARCTIC_SHIFT_URL", DEFAULT_HOST).rstrip("/"),
            subreddits=tuple(
                # Tolerant of spacing, because this is typed into Infisical by
                # hand and "wallstreetbets, stocks" is what a person writes.
                part.strip().lstrip("r/").strip()
                for part in raw.split(",")
                if part.strip()
            ),
            backfill_days=env.integer("REDDIT_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS),
            refresh_hours=env.integer("REDDIT_REFRESH_HOURS", DEFAULT_REFRESH_HOURS),
            # Tunable without a deploy, because the right number is a property
            # of how busy the mirror is rather than of this code.
            delay=float(env.integer("REDDIT_DELAY_MS", int(DEFAULT_DELAY * 1000))) / 1000,
        )

    @property
    def enabled(self) -> bool:
        """False when the subreddit list is empty, which is how this is switched off."""
        return bool(self.subreddits)
