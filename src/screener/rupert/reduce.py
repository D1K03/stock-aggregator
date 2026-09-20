"""Many readings into one number per security per night. Pure arithmetic.

The third of the three verbs, and the one that does not talk to anything. Every
constant here changes what a metric *means* rather than how much it counts, so
each one bumps `scoring_logic_version` and none of them belongs in the database
-- the line `screener.scoring.basis` draws, for the reason it draws it.

**Nothing consumes this yet**, deliberately and on the same terms as everything
else in this tree: a new input moves a pillar for every ticker on the night it
lands, and the diff step reads a universe-wide shift as a universe-wide set of
crossings. It goes in behind a weight-version bump, not beside one.

Two numbers come out, not one, and the second is the one the evidence actually
favours. Tone is what a sentiment pillar is assumed to want; **attention** --
how much a security is being talked about against its own recent normal -- is
what the literature keeps finding predicts better, and `CLAUDE.md` already
half-says so: "weight Sentiment low, treat it as a crowding warning". Both are
emitted so a backtest can choose between them rather than an assumption.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean, median

# Below this, a night's tone is one person's opinion rather than a reading.
#
# **Measured, not chosen.** Over three days of the live corpus, 291 securities
# were mentioned at all, 57 reached ten mentions and 27 reached thirty. Ten is
# the floor that keeps roughly a fifth of what is mentioned and discards the
# long tail of securities named once by one person -- which would otherwise be
# the loudest column in the table, because a single comment has no variance to
# pull it toward the middle.
MIN_MENTIONS = 10

# What fraction of each end to discard before averaging.
#
# A plain mean is the obvious answer and the wrong one: one viral post moves it,
# and the corpus this reads is a subreddit where exactly that is the point.
# Trimming a tenth off each end costs almost nothing when a security is being
# discussed normally and removes the single screenshot that would otherwise
# decide the night.
TRIM = 0.1

# How many days of a security's own history the attention figure is measured
# against. Its *own*, never the universe's: a security that is always discussed
# and a security that has never been discussed before both read as unremarkable
# against a market-wide average, and the second one is the entire signal.
BASELINE_DAYS = 30

# Below this many baseline days, there is nothing to be unusual against.
MIN_BASELINE_DAYS = 14


@dataclass(frozen=True, slots=True)
class Mood:
    """One security, one night: how it read and how much of it there was.

    `mentions` travels beside `tone` rather than being divided out of it,
    because a score computed from three comments and one computed from three
    hundred are different evidence and the number alone cannot say which it is.
    Same rule the pillars follow in keeping a raw metric beside its percentile.
    """

    tone: Decimal
    mentions: int
    trimmed: int


def mood(tones: Sequence[float]) -> Mood | None:
    """The night's tone for one security, or None when there is too little.

    `tones` is one `positive - negative` per resolved mention, as
    `screener.sentiment.Sentiment.score` derives it. The derivation stays there
    and is not repeated here: one definition, computed where the inputs are.

    None rather than zero for a thin night, because zero is a real reading --
    it is what a genuinely balanced day looks like -- and a security nobody
    mentioned is not having a balanced day. The caller turns this into an
    absence with a reason.
    """
    if len(tones) < MIN_MENTIONS:
        return None

    ordered = sorted(tones)
    cut = int(len(ordered) * TRIM)
    kept = ordered[cut : len(ordered) - cut] if cut else ordered
    # Trimming can only empty the list if TRIM ever reached a half, which would
    # be a different statistic. Guarded anyway, because the failure would be a
    # crash in the middle of a night rather than a wrong number.
    middle = fmean(kept) if kept else median(ordered)

    return Mood(
        # Quantised on the way out. The pillars are decimal arithmetic
        # throughout, and a float's binary-repr tail rendered into a
        # traceability panel reads as a bug in the panel.
        tone=Decimal(f"{middle:.6f}"),
        mentions=len(tones),
        trimmed=len(ordered) - len(kept),
    )


def attention(today: int, baseline: Sequence[int]) -> Decimal | None:
    """How unusual tonight's volume is against this security's own recent days.

    A robust z-score: median and median absolute deviation rather than mean and
    standard deviation, because the baseline of a meme stock contains the spike
    this is trying to detect, and a mean baseline is moved by the very event it
    is supposed to be the background for.

    `baseline` is one count per day, the days before tonight, and is passed as
    data so this stays pure -- the history query belongs to `store`.

    None when there is not enough history to say what normal is. A security
    ingested last week has no normal, and inventing one would make its first
    ordinary day read as an event.
    """
    if len(baseline) < MIN_BASELINE_DAYS:
        return None

    typical = median(baseline)
    spread = median([abs(day - typical) for day in baseline])
    if spread == 0:
        # A security discussed exactly the same amount every day -- in practice
        # one discussed zero times every day, which is most of the universe.
        # Any mention at all is then unusual, but *how* unusual is not a
        # question this data can answer, so it declines to invent a magnitude.
        return None if today == typical else Decimal(1) if today > typical else Decimal(-1)

    # 1.4826 makes the median absolute deviation an estimator of the standard
    # deviation for normally distributed data, so this number is readable on the
    # scale people already expect a z-score to be on.
    return Decimal(f"{(today - typical) / (1.4826 * spread):.6f}")
