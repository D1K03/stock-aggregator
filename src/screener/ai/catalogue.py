"""What OpenRouter is currently selling, and which of it is worth buying.

`models.py` is the hand-written list of models this project is willing to spend
money on. It is three entries, and every price in it was out of date within a
month of being typed. This is the other half: the live catalogue, fetched from
OpenRouter's public models endpoint, so the dashboard can offer a real choice
and say what each one costs today rather than what it cost when someone wrote
it down.

Two things make that safe rather than reckless.

**The catalogue is the allow-list.** `resolve_model` refuses an unknown slug on
purpose: a typo would otherwise reach OpenRouter, match some other provider's
model, and bill at a rate nobody chose. A dropdown of four hundred models
cannot be maintained by hand, so the list the browser picks from and the list
the server accepts are the same fetched object rather than two lists that
drift.

**Eligibility is a filter, not a preference.** A model without tool support does
not answer worse, it answers *wrongly* — Steven's answer to "what is in the
database" is a tool call, and a model that cannot make one invents the figure
instead, which is the single thing the system prompt forbids. And the daily cap
is cents per person, so one click on a model at forty dollars a million tokens
would spend a day's allowance on one message. The cap is not what makes the
dropdown safe to open; the price ceiling is.

The ranking deliberately mirrors `screener.scoring`: normalise each benchmark
to a **percentile within the pool that reports it**, average the percentiles
that exist, and never impute one that does not. It is the same argument as
sector percentiles — a raw index means nothing without the field it was
measured against — and the same rule as `basis.py`, that an absent input is
absent rather than zero or a guess.

Everything here except `ranked_models()` is pure and at module scope, so CI checks
the ranking without a network, the way `sentiment.plan_chunks` is checked
without the model.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"

# Long, because this is a price list rather than a price. Models are added and
# rates change over days, and a browser opening the dashboard should not cost a
# round trip to OpenRouter to draw a dropdown.
TTL_SECONDS = 6 * 60 * 60

# Short on purpose: this sits in front of a page render, and a catalogue that
# hangs costs the dropdown rather than the conversation.
TIMEOUT = 8.0

# The three indices Artificial Analysis publishes through OpenRouter, in the
# order they are weighted below.
INDICES = ("agentic_index", "intelligence_index", "coding_index")

# What this workload actually is. Steven is a tool-calling loop over a screener
# database — up to six rounds of "call a tool, read the result, decide" — so
# agentic ability is the thing being bought. Intelligence second, because the
# question behind a tool call still has to be understood. Coding last and not
# zero: the `sql` tool means he does write a SELECT, but a short one against a
# schema he is handed.
#
# This is the one place the ranking is opinionated, and it is opinionated on
# purpose: OpenRouter's own value board is workload-neutral because it does not
# know what you are going to run. This does.
WEIGHTS: dict[str, float] = {
    "agentic_index": 0.55,
    "intelligence_index": 0.30,
    "coding_index": 0.15,
}

# How a turn's bill actually splits. The system prompt, the tool schemas and the
# remembered exchanges are re-sent on every round of the loop, while the reply
# itself is capped at a few hundred tokens — so a turn is overwhelmingly input.
# A naive 50/50 blend would rank models on the half of the bill that barely
# moves.
INPUT_SHARE = 0.9

# The ceiling, as a multiple of the cheapest eligible model's turn cost.
#
# Relative rather than absolute, because the thing being bounded is "far outside
# what this project spends", and that is a statement about the market rather
# than about a number. A fixed dollar figure written down today is either
# strangling or meaningless within a year of the rates moving; a multiple of the
# cheapest thing on offer means the same thing in both worlds.
#
# Six, measured rather than chosen. It is set by the dearest model this project
# actually uses: `upstage/solar-pro4`, what the bot answers on today, is 5.8x
# the cheapest eligible turn, and a ceiling that excluded the model in
# production would be describing somebody else's budget. It also keeps the
# highest-capability option on the board at 5.5x, so the tight end of the list
# is still a real choice rather than only cheap ones.
#
# What it cuts is the tail: three quarters of the eligible field, and every
# frontier model by an order of magnitude. Anything dearer is not a trade-off
# anyone should be able to make by clicking a row in a dropdown.
PRICE_MULTIPLE = 6.0

# And an absolute backstop, in dollars per million input tokens, for the case
# the relative rule cannot cover: if the cheapest model on OpenRouter were ever
# dear, six times it would still be dear.
#
# Both live in code and not in the environment, deliberately. This is the line
# between what anyone signed in may spend money on and what they may not, and it
# should move in a diff somebody reads — the same argument as the universe CSV
# being committed so a sector reclassification shows up before it moves a score.
MAX_INPUT_PRICE = 0.5

# Never offered, whatever they cost.
#
# The price rules above already exclude every one of these — the cheapest
# Anthropic model on OpenRouter today is seventy times the ceiling — so this is
# belt and braces rather than the mechanism. It is here because "never Anthropic"
# is a standing instruction rather than an inference from today's prices, and a
# rule that holds only while a price holds is not the rule that was asked for.
EXCLUDED_AUTHORS = frozenset({"anthropic"})

# Below this there is no room for the loop: the prompt and the tool schemas
# alone are several thousand tokens before anyone has asked anything.
MIN_CONTEXT = 32_000

# The router: a choice that is not a model.
#
# Picking a model pins it, and a pinned model is wrong the week after the
# ranking moves — which it does, because the prices and the benchmarks behind it
# are somebody else's and they change. This is the other option: choose the
# *rule* instead of the answer, and get whatever tops the board at the moment
# the question is asked.
#
# It is the default, and it is what a lapsed choice returns to, so the two paths
# that end in "nobody has expressed a preference" end in the same place rather
# than in a slug written down somewhere.
#
# Deliberately not in `author/name` form. Every real OpenRouter id has a slash
# in it, so a bare word cannot collide with one, and there is no version of this
# where the sentinel is accidentally sent to a provider as a model name.
ROUTER = "router"

# What it takes to be *recommended*, as opposed to merely offered.
#
# Coverage first: value per dollar rewards being cheap, and a model with one
# benchmark to its name can top the board on price alone while nobody has
# measured the thing it would actually be doing. Two of three is the line
# between a ranking and a rumour.
MIN_INDICES = 2
# And a floor, because capability-per-dollar with no floor recommends whatever
# is cheapest and barely works. A model in the bottom half of its field is not
# a bargain for a tool that must not invent a number.
MIN_CAPABILITY = 50.0


@dataclass(frozen=True, slots=True)
class Offered:
    """One model as OpenRouter currently describes it.

    Prices are per million tokens, converted here rather than at the point of
    display: the API reports dollars per token, which renders as a wall of
    zeroes in every place a human reads it.
    """

    slug: str
    label: str
    author: str
    context: int
    max_output: int
    input_per_m: float
    output_per_m: float
    cache_read_per_m: float
    # Raw Artificial Analysis indices, keyed by the names in `INDICES`. A key is
    # absent when the benchmark has not been run, never zero.
    indices: dict[str, float]
    # Whether the model accepts a `tools` array, and whether it answers in
    # text. Both are eligibility facts rather than display ones, but they are
    # properties of the model, and carrying them here is what lets `eligible`
    # be a filter over records instead of a function that needs the raw payload
    # handed to it a second time.
    takes_tools: bool = False
    answers_in_text: bool = False

    @property
    def turn_cost(self) -> float:
        """Blended price of one tool-heavy turn, per million tokens."""
        return INPUT_SHARE * self.input_per_m + (1 - INPUT_SHARE) * self.output_per_m


@dataclass(frozen=True, slots=True)
class Ranked:
    """One model placed against the rest of the eligible field."""

    model: Offered
    # Percentile per index, same keys as `Offered.indices` and the same
    # absences. This is what the interface shows: the raw index is meaningless
    # without the field, which is the whole argument for sector percentiles in
    # the screener.
    percentiles: dict[str, float]
    # Weighted mean of the percentiles that exist, on the weights above.
    capability: float
    # Capability per dollar of a blended turn. The number the board sorts on.
    value: float

    @property
    def coverage(self) -> int:
        """How many of the three benchmarks this model has actually been run on."""
        return len(self.percentiles)

    @property
    def recommendable(self) -> bool:
        """Whether there is enough evidence to call this one best.

        Being pickable and being recommendable are different questions, and
        conflating them is how a dropdown starts making claims. Anything
        eligible can be chosen; only this can be *offered* as the choice.
        """
        return self.coverage >= MIN_INDICES and self.capability >= MIN_CAPABILITY


def _number(raw: Any) -> float:
    """A price or an index as a float, treating anything unreadable as absent."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def parse(payload: Any) -> tuple[Offered, ...]:
    """OpenRouter's models response into `Offered` records.

    Defensive about shape rather than strict: this is somebody else's payload
    and a new field or a null where a number was expected should cost one model
    from the list, not the whole dropdown.
    """
    rows = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return ()

    out: list[Offered] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("id") or "")
        if not slug:
            continue
        pricing = row.get("pricing") or {}
        provider = row.get("top_provider") or {}
        analysis = ((row.get("benchmarks") or {}).get("artificial_analysis")) or {}

        indices = {
            key: float(analysis[key])
            for key in INDICES
            if isinstance(analysis.get(key), (int, float))
        }
        # The API gives "inclusionAI: Ling 3.0 Flash". The author is already a
        # column of its own, so the prefix is furniture in every row.
        name = str(row.get("name") or slug)
        label = name.split(": ", 1)[1] if ": " in name else name

        out.append(
            Offered(
                slug=slug,
                label=label,
                author=slug.split("/", 1)[0],
                context=int(row.get("context_length") or 0),
                max_output=int(provider.get("max_completion_tokens") or 0),
                input_per_m=_number(pricing.get("prompt")) * 1e6,
                output_per_m=_number(pricing.get("completion")) * 1e6,
                cache_read_per_m=_number(pricing.get("input_cache_read")) * 1e6,
                indices=indices,
                takes_tools="tools" in (row.get("supported_parameters") or []),
                answers_in_text="text"
                in ((row.get("architecture") or {}).get("output_modalities") or []),
            )
        )
    return tuple(out)


def eligible(offered: tuple[Offered, ...]) -> tuple[Offered, ...]:
    """The models this project will let someone pick.

    Every clause here is a refusal with a reason, and none of them is a
    preference: a preference belongs in `rank`, where it is visible as a weight
    and can be argued with. These are the models that would be wrong to offer
    at all.
    """
    keep: list[Offered] = []
    for model in offered:
        # A tool loop with a model that cannot call tools is the failure this
        # project least wants: a fluent answer with an invented number in it.
        if not model.takes_tools:
            continue
        if not model.answers_in_text:
            continue
        if model.author in EXCLUDED_AUTHORS:
            continue
        # `:batch` endpoints are answered whenever the provider gets to them.
        # That is a fine way to summarise a filing overnight and no way at all
        # to hold a conversation, and they undercut their own interactive twin
        # on price, so they would otherwise win a board they cannot serve.
        if ":batch" in model.slug:
            continue
        # A free tier is a different product with different rate limits and a
        # different data policy, and this sends stored screener rows. Opting
        # into that is a decision someone should make deliberately, not by
        # picking the top of a list sorted by price.
        if model.input_per_m <= 0 and model.output_per_m <= 0:
            continue
        if model.input_per_m > MAX_INPUT_PRICE:
            continue
        if model.context < MIN_CONTEXT:
            continue
        keep.append(model)

    # The relative ceiling, which needs the field before it can be applied: how
    # dear is too dear is a question about what else is on offer. Run last, over
    # what survived the absolute rules, so the cheapest it measures against is
    # itself a model this project would actually use — otherwise some free or
    # toolless thing would set the baseline for everything else.
    if not keep:
        return ()
    floor = min(model.turn_cost for model in keep)
    return tuple(model for model in keep if model.turn_cost <= floor * PRICE_MULTIPLE)


def _percentiles(values: list[float]) -> dict[float, float]:
    """Percentile per distinct value, midpoint-ranked.

    Deliberately not `screener.scoring.percentile`, which is the same
    arithmetic: importing it would pull the scoring package's surface — and
    through it psycopg — into the bot and status containers, which is exactly
    the coupling `screener.config` was split up to avoid. Floats rather than
    Decimals because these are benchmark indices for display, not money.
    """
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        # One model reporting a benchmark has no field to be placed in, and 50
        # is the only answer that does not claim a rank it cannot know.
        return {ordered[0]: 50.0}
    out: dict[float, float] = {}
    for value in set(ordered):
        below = sum(1 for other in ordered if other < value)
        equal = sum(1 for other in ordered if other == value)
        rank = below + (equal - 1) / 2
        out[value] = rank / (n - 1) * 100
    return out


def rank(offered: tuple[Offered, ...]) -> tuple[Ranked, ...]:
    """Place every model against the field, best value first.

    Pure, so the whole ranking is testable from a fixture. The field is
    whatever was passed in: percentiles are relative by definition, so ranking
    a filtered pool against itself is the point rather than an approximation.
    """
    scales = {
        index: _percentiles([m.indices[index] for m in offered if index in m.indices])
        for index in INDICES
    }

    out: list[Ranked] = []
    for model in offered:
        placed = {
            index: scales[index][value]
            for index, value in model.indices.items()
            if value in scales.get(index, {})
        }
        if not placed:
            # Nothing has been measured. It stays pickable — someone may know
            # something the benchmarks do not — but it cannot be placed, and
            # inventing a middling capability for it would let it rank on price
            # alone.
            out.append(Ranked(model=model, percentiles={}, capability=0.0, value=0.0))
            continue

        # Weighted over the indices that exist, renormalised by their weights.
        # An absent benchmark is absent, not zero: scoring counts a missing
        # pillar as nothing because a weighted pillar that produced no metrics
        # is a real gap in a blended score, but here the model is being
        # compared to others measured on the same subset, and zeroing would
        # punish a model for a benchmark nobody has run yet.
        share = sum(WEIGHTS[index] for index in placed)
        capability = sum(WEIGHTS[index] * value for index, value in placed.items()) / share
        cost = model.turn_cost
        out.append(
            Ranked(
                model=model,
                percentiles=placed,
                capability=capability,
                value=capability / cost if cost > 0 else 0.0,
            )
        )

    return tuple(sorted(out, key=lambda r: r.value, reverse=True))


def recommend(ranked: tuple[Ranked, ...]) -> Ranked | None:
    """The best value with enough evidence behind it, or nothing.

    Returning None is a real outcome and the interface has to handle it: if
    OpenRouter has published no benchmarks for anything affordable, the honest
    answer is to leave the button off rather than point at whatever is
    cheapest.
    """
    for candidate in ranked:
        if candidate.recommendable:
            return candidate
    return None


def why(candidate: Ranked) -> str:
    """One line saying what the recommendation is resting on.

    A recommendation the interface cannot explain is the same thing as an alert
    that says STRONG BUY. This is the sentence under the button.
    """
    agentic = candidate.percentiles.get("agentic_index")
    # Phrased as "beats N% of the field" rather than "Nth percentile" because
    # the ordinal is a separate piece of arithmetic to get wrong for no gain,
    # and because the comparison is the thing being claimed.
    lead = (
        f"beats {agentic:.0f}% of the field at tool use"
        if agentic is not None
        else f"beats {candidate.capability:.0f}% of the field overall"
    )
    return (
        f"{lead}, at ${candidate.model.input_per_m:.3g}/M in — "
        f"best capability per dollar, on {candidate.coverage} of {len(INDICES)} benchmarks"
    )


# The cache. One process, one catalogue, refetched when it goes stale. Module
# state rather than a class because there is exactly one OpenRouter and the
# alternative is threading a handle through every caller that wants to draw a
# dropdown.
_cached: tuple[Ranked, ...] = ()
_cached_at: float = 0.0


def ranked_models(
    *, transport: httpx.BaseTransport | None = None, force: bool = False
) -> tuple[Ranked, ...]:
    """The ranked, eligible catalogue, from cache when it is fresh.

    Fails soft and says so in the log: a dropdown that cannot be drawn should
    leave the conversation on its configured model, not break the page. The
    caller distinguishes "empty because OpenRouter is down" from "empty because
    nothing qualified" by the fact that the second cannot happen — the static
    `MODELS` are always offered alongside this.

    `transport` is for tests, exactly as in `openrouter.py`; production never
    passes it.
    """
    global _cached, _cached_at
    fresh = _cached and (time.monotonic() - _cached_at) < TTL_SECONDS
    if fresh and not force:
        return _cached

    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            response = client.get(CATALOGUE_URL)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("could not read the OpenRouter catalogue: %s", exc)
        # The stale copy, if there is one. A price list from six hours ago is
        # a better answer than no dropdown, and the alternative — refusing to
        # render — turns somebody else's outage into ours.
        return _cached

    ranked = rank(eligible(parse(payload)))
    _cached, _cached_at = ranked, time.monotonic()
    logger.info("catalogue: %d models eligible", len(ranked))
    return ranked


def allows(slug: str) -> bool:
    """Whether this slug is one the catalogue currently offers.

    The check that makes the dropdown an allow-list rather than a pass-through.
    Reads the cache and fetches if it is cold, because the alternative — trust
    whatever the cache happens to hold after a restart — is precisely the
    silent degradation this codebase keeps finding in itself: a valid pick
    would fall back to the default and look like it worked.
    """
    return any(entry.model.slug == slug for entry in ranked_models())


def find(slug: str) -> Ranked | None:
    """One ranked entry by slug, or None if the catalogue does not offer it."""
    for entry in ranked_models():
        if entry.model.slug == slug:
            return entry
    return None


def offers(slug: str) -> bool:
    """Whether this is something a person may choose — a model, or the router.

    The router is a choice the interface offers and the catalogue does not
    contain, so `allows` alone would refuse it. Kept apart from `allows` rather
    than folded into it, because `allows` answers "may this be sent to
    OpenRouter" and the router may never be.
    """
    return slug == ROUTER or allows(slug)


def routed(choice: str) -> str | None:
    """A stored choice as the concrete model to send, or None if there is none.

    The one place the sentinel is turned back into a model, so nothing
    downstream has to know the router exists. Re-resolved on every question
    rather than at the moment of choosing, which is the entire point: choosing
    the router is choosing to follow the ranking, and a value captured when the
    button was pressed would just be a pinned model with extra steps.
    """
    if choice != ROUTER:
        return choice if allows(choice) else None
    best = recommend(ranked_models())
    return best.model.slug if best is not None else None


# How many models the dashboard is sent. The full eligible field is a couple of
# hundred, which is a scroll bar rather than a choice; this is the part of it
# anyone would actually pick from, and the ranking has already decided which
# part that is.
OFFERED_TO_THE_INTERFACE = 24


def payload(ranked: tuple[Ranked, ...], *, current: str) -> dict[str, Any]:
    """The catalogue as the dashboard reads it.

    Shaped here rather than in the status service so the numbers the interface
    prints and the numbers the ranking used are the same ones, and every figure
    the picker shows comes with what produced it — the same rule as the
    screen's explain panel, where a score that cannot show its inputs is not
    worth displaying.

    `current` is always included even when it has fallen out of the top slice
    or off the catalogue entirely, because a picker that cannot draw its own
    selection is worse than one with an extra row.
    """
    best = recommend(ranked)
    shown = list(ranked[:OFFERED_TO_THE_INTERFACE])
    if current and not any(entry.model.slug == current for entry in shown):
        shown.extend(entry for entry in ranked if entry.model.slug == current)

    return {
        "current": current,
        # The router as the interface draws it: a row you can select, which is
        # not a model and shows the model it currently comes out as. `resolves`
        # is a fact about right now and is deliberately not stored anywhere —
        # the whole value of choosing it is that tomorrow's answer may differ.
        "router": {
            "slug": ROUTER,
            "resolves": best.model.slug if best is not None else None,
            "label": best.model.label if best is not None else None,
            "why": why(best) if best is not None else None,
        },
        "recommended": (
            {"slug": best.model.slug, "why": why(best)} if best is not None else None
        ),
        "weights": dict(WEIGHTS),
        "models": [
            {
                "slug": entry.model.slug,
                "label": entry.model.label,
                "author": entry.model.author,
                "context": entry.model.context,
                "input_per_m": round(entry.model.input_per_m, 4),
                "output_per_m": round(entry.model.output_per_m, 4),
                "capability": round(entry.capability),
                "value": round(entry.value),
                "coverage": entry.coverage,
                "recommendable": entry.recommendable,
                # Nulls kept rather than dropped: "not measured" is a fact the
                # interface shows as a dash, and a missing key would render the
                # same as a zero.
                "percentiles": {
                    index: (
                        round(entry.percentiles[index])
                        if index in entry.percentiles
                        else None
                    )
                    for index in INDICES
                },
            }
            for entry in shown
        ],
    }
