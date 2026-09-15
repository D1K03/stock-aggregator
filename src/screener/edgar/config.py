"""EDGAR ingest settings, owned by the ingest layer."""

from dataclasses import dataclass

from screener.config import env

# The archive, not `data.sec.gov`. The daily index and the complete submissions
# it names both live under here, and neither needs a key or a session.
DEFAULT_HOST = "https://www.sec.gov/Archives/edgar"

# Twice a day. A Form 4 is due within two business days of the trade, and SEC
# publishes the day's index once the feed closes, so there is nothing to gain
# from asking more often and one missed wake should not cost a whole day.
DEFAULT_REFRESH_HOURS = 12

# How many unwalked days one pass takes. The bound exists so a long backfill
# drains steadily rather than as one twelve-hour pass that a deploy interrupts
# and starts over: at ~174 filings a day this is about two and a half minutes.
DEFAULT_DAYS_PER_PASS = 5

# How far back to consider. Widening this **is** the backfill -- there is no
# subcommand, because the frontier is the set of days already walked and a
# wider window simply offers more of them. EDGAR's daily indexes go back to
# 1994; thirty days is a month of catching up on a fresh deployment.
DEFAULT_BACKFILL_DAYS = 30

# Between filings, in seconds. SEC publishes a limit of 10 requests a second
# and enforces it by blocking the address, so this is compliance rather than
# politeness -- unlike Arctic Shift, where the same constant is a courtesy.
# 0.15s is about 6.7/s, which leaves room for the universe refresh to be
# running at the same time without the two together crossing the line.
DEFAULT_DELAY = 0.15

# The same disqualifying substring `screener.universe.sources.sec` records, for
# the same measured reason: SEC answers 403 to an address at github.com as
# firmly as to a User-Agent naming nobody at all.
BANNED = "github.com"


@dataclass(frozen=True)
class EdgarConfig:
    """Where to fetch from, how far back, how often, and who we say we are.

    The contact address is the one field with no working default, and that is
    deliberate: SEC answers 403 to a User-Agent that names nobody, so there is
    nothing here to turn on without one. It is the credential and the switch at
    once, which is the shape `PLAYGROUND_DB_PASSWORD` already has.
    """

    host: str = DEFAULT_HOST
    contact_email: str = ""
    backfill_days: int = DEFAULT_BACKFILL_DAYS
    days_per_pass: int = DEFAULT_DAYS_PER_PASS
    refresh_hours: int = DEFAULT_REFRESH_HOURS
    delay: float = DEFAULT_DELAY

    @classmethod
    def from_env(cls) -> "EdgarConfig":
        return cls(
            host=env.text("EDGAR_ARCHIVE_URL", DEFAULT_HOST).rstrip("/"),
            # **Deliberately not SEC_CONTACT_EMAIL**, which already exists and
            # drives the quarterly, hand-run universe refresh. Sharing the
            # variable would mean that setting a contact address for a command
            # somebody runs four times a year silently starts a daily crawler
            # against the same government service. `__main__` says so out loud
            # when one is set and the other is not, so the separation is
            # discoverable rather than mysterious.
            contact_email=env.text("EDGAR_CONTACT_EMAIL", "").strip(),
            backfill_days=env.integer("EDGAR_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS),
            days_per_pass=env.integer("EDGAR_DAYS_PER_PASS", DEFAULT_DAYS_PER_PASS),
            refresh_hours=env.integer("EDGAR_REFRESH_HOURS", DEFAULT_REFRESH_HOURS),
            # Milliseconds as an integer so `env.integer` does the validating,
            # then divided -- the shape `REDDIT_DELAY_MS` already uses.
            delay=float(env.integer("EDGAR_DELAY_MS", int(DEFAULT_DELAY * 1000))) / 1000,
        )

    @property
    def user_agent(self) -> str:
        """The header SEC requires, built from the deployer's contact address.

        Duplicated from `screener.universe.sources.sec` rather than imported:
        `screener.universe` does not re-export it, CLAUDE.md's rule is that
        nothing outside a package imports its submodules, and the two have
        genuinely different failure modes. A hand-run command should raise on a
        missing address; a long-running container should report itself off.
        """
        return f"stock-aggregator/0.1 ({self.contact_email})"

    @property
    def enabled(self) -> bool:
        """False without a usable contact address, which is how this is off.

        An address at the banned domain counts as unusable rather than absent,
        and `__main__` tells those two apart: one is a deliberate off switch and
        the other is a typo that would otherwise present as a 403 nobody can
        explain.
        """
        return bool(self.contact_email) and BANNED not in self.contact_email.lower()
