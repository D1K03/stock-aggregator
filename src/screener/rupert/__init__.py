"""Which security a text is about, and what kind of thing it says.

The join `screener.reddit` and `screener.magpie` both stopped short of. Both
said so in their own migrations -- "connecting an item to the tickers it
mentions is its own piece of work", "a nullable column here would invite a
half-done join" -- and this is that piece of work, as a table rather than a
column for exactly the reason 022 gave: a resolution is a decision somebody made
with a model, at a moment, with a confidence and a set of alternatives it was
chosen between, and a column throws all of that away.

Three verbs, in order. **Resolve** shortlists candidate symbols with a regex and
then asks a model which one -- if any -- the sentence is actually about.
**Read** scores what resolved, with FinBERT. **Reduce** turns many readings into
one number per security per night, and is pure arithmetic.

Two halves that share nothing but a dataclass, as `screener.reddit` and
`screener.edgar` do. `decide` talks to the model and never opens a database
connection; `store` writes rows and never opens a socket. `candidates`,
`questions` and `reduce` are pure and open neither.

**The deterministic half deliberately refuses to decide.** Measured on the live
corpus over 22 days and 424,178 items: `$TICKER` reaches 0.6% of comments, so
bare uppercase tokens have to be candidates too -- and doing that reaches 5.6%
of items while pulling in ordinary English at roughly a quarter of the top
matches (YOU, ON, IT, ARE, ALL, AM, NOW, PM, beside real traffic in MU, SNDK,
AMD and NVDA). A blacklist is lossy in both directions: dropping ALL, IT and ON
discards Allstate, Gartner and ON Semiconductor for good, because the difference
between "I put it ALL on calls" and "ALL reported a combined ratio of 91" is the
sentence and not the word. So the regex shortlists and something that can read
the sentence chooses.

**`DESIGN.md`'s rule stands unchanged: an LLM never emits a score.** The
decision model is asked `choice` and `noul` questions only -- which company,
what kind of claim, and three gates. Tone is FinBERT's and only FinBERT's. Its
own `score` primitive is the worst-calibrated of the three out of distribution
and is deliberately unused.

Ingest only, on the same terms as everything else in this tree. `reduce` is pure
and nothing consumes it: a new input moves a pillar for every ticker on the
night it lands, so wiring it into the Sentiment pillar needs a weight-version
bump and a backfill with alerting disabled, not a table.
"""

from screener.rupert.config import RupertConfig
from screener.rupert.decide import Choice, DecideError, Decision, Throttled
from screener.rupert.reduce import Mood, attention, mood
from screener.rupert.run import Report, once
from screener.rupert.store import Text
from screener.rupert.version import CHANGELOG, VERSION, described, released

__all__ = [
    "CHANGELOG",
    "Choice",
    "DecideError",
    "Decision",
    "Mood",
    "Report",
    "RupertConfig",
    "Text",
    "Throttled",
    "VERSION",
    "attention",
    "described",
    "mood",
    "once",
    "released",
]
