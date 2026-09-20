"""Rupert settings, owned by the resolver."""

from dataclasses import dataclass

from screener.config import env

# How many paid decisions one day may buy. **This is the off switch and the cap
# at once**, which is the shape `EDGAR_CONTACT_EMAIL` already has, and it
# defaults to zero: this layer spends money, so it starts off and turning it on
# is a deliberate act with a number attached.
#
# Zero means nobody may spend anything, never "no limit". The permissive reading
# of an unset cap is the one `screener.bot.budget` already rejected, and for the
# same reason -- the first anybody knows of it is the invoice.
#
# **Its own counter rather than `DAILY_SPEND_CAP_USD`**, on `screener.magpie`'s
# precedent. A decision costs about $0.00002 against a chat reply's $0.00005, so
# the two are close in unit price but not in volume: a night's resolving is
# thousands of calls and the assistant's whole daily allowance is $0.10. One
# counter for both would let a busy night in the corpus silence Steven, which is
# exactly the failure magpie's separate unlocker cap exists to prevent.
DEFAULT_DAILY_MAX_CALLS = 0

# A hard ceiling in dollars, checked against what was actually billed.
#
# **Belt as well as braces, and the two are not redundant.** The call cap above
# bounds spend only if the price per call is what we think it is — it is a count,
# and it assumes $0.000021. A provider repricing, a state that grew, or a
# question set that doubled would each push the bill past the intent while the
# counter still read as fine. This one reads `rupert.mention.cost_usd`, which is
# what OpenRouter actually charged, so it holds whatever the unit price does.
#
# Measured: a full night is about $0.02, so 25 cents is roughly ten normal
# nights. It is a backstop for something going wrong, not a working limit — if
# this is what stops a pass, something else is already broken.
DEFAULT_DAILY_MAX_USD = 0.25

# How many candidate-bearing items one pass resolves. At ~1,080 a day on the
# measured corpus, a pass every six hours has about 270 to do and this is a
# ceiling rather than a target -- it bounds a first run against a backlog, so a
# fresh deployment drains steadily instead of spending its whole cap at once.
DEFAULT_BATCH = 400

DEFAULT_REFRESH_HOURS = 6

# How far back to start reading on a corpus never read before.
#
# **Zero, which means "start now and never look back".** The first pass sets the
# frontier to the moment it runs, so nothing already sitting in `social_item` is
# ever decided and the first night costs only what arrives during it.
#
# Measured, and the reason this is not 7: the corpus takes ~19,300 items a day,
# so a week's backfill is ~135,000 items examined and ~7,600 paid decisions on
# the first wake — which blows through a day's call budget several times over
# and spends it on comments from before anybody was watching. A backfill is a
# thing to ask for deliberately, on a day you are watching it, not something a
# deploy does to you.
#
# Widening this **is** the backfill, on `EDGAR_BACKFILL_DAYS`'s terms, and it
# only applies to a corpus with no `rupert.progress` row — once the frontier
# exists, this is ignored and the walk carries on from where it got to.
DEFAULT_BACKFILL_DAYS = 0

# How peaked the choice has to be before the mention counts as resolved.
#
# The model's own guidance is above 0.9 to act without review. That is the floor
# here rather than a comfortable setting, because an independent calibration
# test measured it **overconfident on `choice` out of distribution** -- the
# temperature needed to make the probabilities honest was 3.29. A decision below
# this is still stored, with its whole distribution, as `unsure`: the threshold
# can then be re-cut against real data without re-deciding, which is the only
# reason keeping the distribution is worth the column.
DEFAULT_CONFIDENCE_FLOOR = 0.9

# Above this, the text is treated as trying to steer whatever reads it and the
# mention is refused. Low, and asymmetric with the floor above on purpose: the
# same calibration test found `noul` **under**confident out of distribution
# (refit 0.66), so a yes/no that gets as far as 0.5 is saying more than the
# number looks like it is saying.
DEFAULT_INJECTION_CEILING = 0.5

# How much of one text is sent. The state is capped far below the endpoint's
# 32k because the published guidance is that accuracy falls as the state fills
# with content unrelated to the decision, and the decision is "which of these
# companies is this about" -- which the first paragraph answers if anything does.
DEFAULT_MAX_CHARS = 6_000


@dataclass(frozen=True)
class RupertConfig:
    """What to read, how much of it, how sure to be, and how much to spend.

    No credentials of its own: the key is `OPENROUTER_API_KEY`, owned by
    `screener.ai.RouterConfig` and shared with the assistant, because it is one
    account and one bill. What is *not* shared is the budget -- see
    `DEFAULT_DAILY_MAX_CALLS`.
    """

    daily_max_calls: int = DEFAULT_DAILY_MAX_CALLS
    daily_max_usd: float = DEFAULT_DAILY_MAX_USD
    batch: int = DEFAULT_BATCH
    refresh_hours: int = DEFAULT_REFRESH_HOURS
    backfill_days: int = DEFAULT_BACKFILL_DAYS
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR
    injection_ceiling: float = DEFAULT_INJECTION_CEILING
    max_chars: int = DEFAULT_MAX_CHARS

    @classmethod
    def from_env(cls) -> "RupertConfig":
        return cls(
            daily_max_calls=env.integer("RUPERT_DAILY_MAX_CALLS", DEFAULT_DAILY_MAX_CALLS),
            # Cents as an integer, so `env.integer` validates it and "0.25"
            # typed into Infisical cannot silently read as nothing.
            daily_max_usd=env.integer(
                "RUPERT_DAILY_MAX_CENTS", int(DEFAULT_DAILY_MAX_USD * 100)
            ) / 100,
            batch=env.integer("RUPERT_BATCH", DEFAULT_BATCH),
            refresh_hours=env.integer("RUPERT_REFRESH_HOURS", DEFAULT_REFRESH_HOURS),
            backfill_days=env.integer("RUPERT_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS),
            # Thresholds as integer percent so `env.integer` does the
            # validating, then divided -- the shape `REDDIT_DELAY_MS` uses for
            # the same reason. A threshold typed as "0.9" into Infisical and
            # read as text is a class of bug worth not having.
            confidence_floor=env.integer(
                "RUPERT_CONFIDENCE_PCT", int(DEFAULT_CONFIDENCE_FLOOR * 100)
            ) / 100,
            injection_ceiling=env.integer(
                "RUPERT_INJECTION_PCT", int(DEFAULT_INJECTION_CEILING * 100)
            ) / 100,
            max_chars=env.integer("RUPERT_MAX_CHARS", DEFAULT_MAX_CHARS),
        )

    @property
    def enabled(self) -> bool:
        """False while the daily call budget is zero, which is how this is off."""
        return self.daily_max_calls > 0
