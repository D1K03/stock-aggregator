"""Finance-tuned sentiment: text in, three probabilities out.

FinBERT (`ProsusAI/finbert`) exported to ONNX and run under `onnxruntime` on
CPU, in a container of its own. `screener.sentiment.server` is that container;
everything else in this repository asks it for a reading through the client
here, which is `httpx` and nothing more.

Only the client is re-exported. `serve`, `build_server` and `load_model`
deliberately are not: importing them is one import away from `onnxruntime`,
`numpy` and a 438 MB graph, and a process that wants a reading must not pay for
that to make one HTTP call. That is the same line `screener.transcribe` draws,
and `screener.skybird` draws around yt-dlp.

**A model, not a language model, and the distinction is the point.** DESIGN.md's
rule is that an LLM never emits a number: they are inconsistent at numeric
scoring and cost money for something a free classifier does better. This is the
classifier that rule assumes. `screener.ai` does narrative extraction and
answers questions; it does not do this, and this does not do that.

The three probabilities are what travel. `Sentiment.score` -- positive minus
negative -- is derived from them in the client, so there is one definition of it
and a reading always carries the raw inputs behind it. That is the pillar rule
applied one level down: keep the metric beside the number that ranks it, because
a reading that moved and a corpus that moved are different facts.

**Scoring only. Nothing here reads or writes a database, connects a text to a
security, or feeds a pillar.** `screener.reddit` stores the posts and comments
and deliberately does not score them; this scores text and deliberately does not
know where it came from. Wiring the Sentiment pillar is its own piece of work
and it is not small: DESIGN.md's own procedure for onboarding a source into a
pillar is to bump the weight version, backfill with alerting disabled and only
then resume, because a new input moves every ticker on the night it lands.
"""

from screener.sentiment.client import (
    LABELS,
    MAX_CHARS,
    MAX_TEXTS,
    Sentiment,
    score,
    scorer_url,
)

__all__ = [
    "LABELS",
    "MAX_CHARS",
    "MAX_TEXTS",
    "Sentiment",
    "score",
    "scorer_url",
]
