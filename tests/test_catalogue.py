"""The model catalogue: what is offered, what is refused, and what is recommended.

Every test here runs on a fixture rather than the network, because the ranking
is the part that has to be right and it is pure. The one test that does open a
client uses `httpx.MockTransport`, exactly as `test_ai.py` does.
"""

import httpx
import pytest

from screener.ai import catalogue as cat

MAX_OVER = cat.MAX_INPUT_PRICE + 0.01


def model(
    slug: str,
    *,
    prompt: float = 0.05,
    completion: float = 0.15,
    context: int = 262_144,
    tools: bool = True,
    text: bool = True,
    agentic: float | None = None,
    intelligence: float | None = None,
    coding: float | None = None,
) -> dict:
    """One row shaped the way OpenRouter shapes them."""
    analysis: dict[str, float] = {}
    if agentic is not None:
        analysis["agentic_index"] = agentic
    if intelligence is not None:
        analysis["intelligence_index"] = intelligence
    if coding is not None:
        analysis["coding_index"] = coding
    return {
        "id": slug,
        "name": f"Maker: {slug.split('/')[-1]}",
        "context_length": context,
        "architecture": {"output_modalities": ["text"] if text else ["image"]},
        "pricing": {
            # Dollars per token, as the API reports them.
            "prompt": str(prompt / 1e6),
            "completion": str(completion / 1e6),
            "input_cache_read": str(prompt / 4e6),
        },
        "top_provider": {"max_completion_tokens": 32768},
        "supported_parameters": ["max_tokens", "temperature"] + (["tools"] if tools else []),
        "benchmarks": {"artificial_analysis": analysis},
    }


def payload(*rows: dict) -> dict:
    return {"data": list(rows)}


def offered(*rows: dict) -> tuple[cat.Offered, ...]:
    return cat.eligible(cat.parse(payload(*rows)))


def slugs(ranked: tuple[cat.Ranked, ...]) -> list[str]:
    return [entry.model.slug for entry in ranked]


def test_prices_are_converted_to_dollars_per_million():
    # The API reports dollars per token, which renders as a wall of zeroes
    # everywhere a human reads it.
    (parsed,) = cat.parse(payload(model("a/b", prompt=0.04, completion=0.08)))
    assert parsed.input_per_m == pytest.approx(0.04)
    assert parsed.output_per_m == pytest.approx(0.08)


def test_the_author_prefix_is_stripped_from_the_label():
    # "inclusionAI: Ling 3.0 Flash" in a column that already has an author
    # column beside it.
    (parsed,) = cat.parse(payload(model("inclusionai/ling")))
    assert parsed.label == "ling"
    assert parsed.author == "inclusionai"


def test_a_model_without_tool_support_is_never_offered():
    # The agent is a tool loop. A model that cannot call a tool does not answer
    # worse, it answers wrongly — it invents the figure, which is the one thing
    # the system prompt forbids.
    assert offered(model("a/no-tools", tools=False)) == ()
    assert len(offered(model("a/tools", tools=True))) == 1


def test_a_model_over_the_price_ceiling_is_never_offered():
    # The daily cap is cents per person, so one click on an expensive model
    # would spend a day's allowance on a single message.
    over = cat.MAX_INPUT_PRICE + 0.01
    assert offered(model("a/dear", prompt=over)) == ()
    assert len(offered(model("a/cheap", prompt=cat.MAX_INPUT_PRICE))) == 1


def test_batch_endpoints_are_never_offered():
    # Answered whenever the provider gets to them, and cheaper than their own
    # interactive twin — so they would win a board they cannot serve.
    assert offered(model("a/b:batch")) == ()


def test_a_free_tier_is_never_offered():
    # Different rate limits and a different data policy, and this sends stored
    # screener rows. That is a decision to make deliberately, not by picking
    # the top of a list sorted by price.
    assert offered(model("a/b:free", prompt=0, completion=0)) == ()


def test_a_model_that_cannot_answer_in_text_is_never_offered():
    assert offered(model("a/images", text=False)) == ()


def test_a_small_context_is_never_offered():
    assert offered(model("a/tiny", context=cat.MIN_CONTEXT - 1)) == ()


def test_percentiles_are_taken_within_the_field_that_reports_the_benchmark():
    # The same rule as sector percentiles in the screener: a raw index means
    # nothing without the field it was measured against.
    ranked = cat.rank(
        offered(
            model("a/low", agentic=10, intelligence=10, coding=10),
            model("a/mid", agentic=50, intelligence=50, coding=50),
            model("a/high", agentic=90, intelligence=90, coding=90),
        )
    )
    placed = {entry.model.slug: entry.percentiles["agentic_index"] for entry in ranked}
    assert placed["a/low"] == 0
    assert placed["a/mid"] == 50
    assert placed["a/high"] == 100


def test_an_unmeasured_benchmark_is_absent_rather_than_zero():
    # Absent, never imputed — `basis.py`'s rule. A model with no agentic score
    # is not a model that scored nothing at tool use, and averaging a zero in
    # would say exactly that.
    ranked = cat.rank(
        offered(
            model("a/full", agentic=90, intelligence=90, coding=90),
            model("a/partial", coding=90),
        )
    )
    partial = next(e for e in ranked if e.model.slug == "a/partial")
    assert "agentic_index" not in partial.percentiles
    assert partial.coverage == 1
    # Scored on what exists, so it is not dragged to the bottom by the absence.
    assert partial.capability == 50


def test_value_is_capability_over_a_blended_turn_cost():
    # Two models of identical capability, one twice the price: the cheap one
    # must rank first and by the ratio of their prices.
    ranked = cat.rank(
        offered(
            model("a/dear", prompt=0.10, completion=0.30, agentic=50, intelligence=50, coding=50),
            model("a/cheap", prompt=0.05, completion=0.15, agentic=50, intelligence=50, coding=50),
        )
    )
    assert slugs(ranked) == ["a/cheap", "a/dear"]
    assert ranked[0].value == pytest.approx(ranked[1].value * 2)


def test_the_cost_blend_is_weighted_towards_input():
    # The prompt and tool schemas are re-sent on every round while the reply is
    # capped at a few hundred tokens, so a turn is overwhelmingly input. A model
    # that is cheap in and dear out must beat one that is dear in and cheap out.
    ranked = cat.rank(
        offered(
            model("a/cheap-in", prompt=0.02, completion=0.30, agentic=50),
            model("a/cheap-out", prompt=0.08, completion=0.01, agentic=50),
        )
    )
    # Both are inside the price ceiling, so this is a comparison rather than a
    # pool of one: `a/cheap-out` is the cheaper of the two on a 50/50 blend and
    # must still lose on the blend the ranking actually uses.
    assert len(ranked) == 2
    assert slugs(ranked)[0] == "a/cheap-in"


def test_tool_use_outweighs_the_other_benchmarks():
    # The weights are the one opinionated part of the ranking, and this is what
    # they are for: the workload is a tool loop, so a model that is good at
    # tools and mediocre elsewhere beats its mirror image.
    ranked = cat.rank(
        offered(
            model("a/tools-first", agentic=90, intelligence=10, coding=10),
            model("a/code-first", agentic=10, intelligence=10, coding=90),
        )
    )
    assert slugs(ranked)[0] == "a/tools-first"


def field(*extra: dict) -> tuple[cat.Offered, ...]:
    """A plausible field, plus whatever the test is actually about.

    Percentiles are meaningless in a pool of two — every model is either 0 or
    100 — so a test about placement has to rank against something. These six
    are unremarkable on purpose: evenly spread, fully measured, and priced in
    the middle, so any result below is caused by the model under test rather
    than by the field.
    """
    return offered(
        *extra,
        *[
            model(f"pool/m{i}", prompt=0.05, completion=0.15, agentic=n, intelligence=n, coding=n)
            for i, n in enumerate((20, 35, 50, 65, 80, 95))
        ],
    )


def test_a_cheap_model_with_one_benchmark_is_offered_but_not_recommended():
    # This is the real case the coverage floor exists for, and the one the live
    # catalogue actually produces: on price alone a model with a single
    # benchmark tops the board while nobody has measured the thing it would be
    # doing all day.
    ranked = cat.rank(
        field(
            model("a/one-benchmark", prompt=0.02, completion=0.06, coding=99),
            model("a/measured", prompt=0.03, completion=0.09, agentic=90, intelligence=90, coding=90),
        )
    )
    assert slugs(ranked)[0] == "a/one-benchmark"
    # Still pickable — someone may know something the benchmarks do not.
    assert any(entry.model.slug == "a/one-benchmark" for entry in ranked)
    best = cat.recommend(ranked)
    assert best is not None
    assert best.model.slug == "a/measured"


def test_a_model_below_the_capability_floor_is_never_recommended():
    """Value per dollar alone would recommend whatever is cheapest and adequate.

    The price ceiling has already made the worst version of this impossible —
    nothing six times cheaper than the field can also be in its bottom decile,
    because it would have to be cheaper than the ceiling permits. What is left
    is the case here: a mid-table model at the cheap end that genuinely leads on
    value, and still should not be the thing a dropdown recommends to somebody
    who has not thought about it.
    """
    ranked = cat.rank(
        field(
            # Six times cheaper than `a/good`, which is exactly the ceiling.
            model("a/cheap-and-middling", prompt=0.02, completion=0.06,
                  agentic=55, intelligence=55, coding=55),
            model("a/good", prompt=0.12, completion=0.36,
                  agentic=90, intelligence=90, coding=90),
        )
    )
    # It does lead the board: this is a real trade-off, not a strawman.
    assert slugs(ranked)[0] == "a/cheap-and-middling"
    middling = next(e for e in ranked if e.model.slug == "a/cheap-and-middling")
    # Three benchmarks, so coverage is not what rules it out — the floor is.
    assert middling.coverage == 3
    assert middling.capability < cat.MIN_CAPABILITY

    # The property, not a particular slug: whatever is recommended is something
    # that cleared the floor, and it is not the thing that topped the board.
    # Naming a model here would be asserting the fixture's arithmetic rather
    # than the rule.
    best = cat.recommend(ranked)
    assert best is not None
    assert best.model.slug != "a/cheap-and-middling"
    assert best.capability >= cat.MIN_CAPABILITY


def test_nothing_is_recommended_when_nothing_has_enough_evidence():
    # A real outcome the interface has to handle: with no benchmarks published
    # for anything affordable, the honest answer is no recommendation rather
    # than whatever is cheapest.
    ranked = cat.rank(offered(model("a/unmeasured"), model("a/also-unmeasured")))
    assert cat.recommend(ranked) is None


def test_the_reason_names_the_evidence_it_rests_on():
    # A recommendation the interface cannot explain is the same thing as an
    # alert that says STRONG BUY.
    ranked = cat.rank(
        offered(
            model("a/best", prompt=0.04, completion=0.12, agentic=90, intelligence=90, coding=90),
            model("a/other", prompt=0.10, completion=0.30, agentic=10, intelligence=10, coding=10),
        )
    )
    best = cat.recommend(ranked)
    assert best is not None
    reason = cat.why(best)
    assert "tool use" in reason
    assert "$0.04" in reason
    assert "3 of 3" in reason


def test_the_payload_always_includes_the_current_model():
    # A picker that cannot draw its own selection is worse than one with an
    # extra row.
    # Priced in a narrow band so the ceiling keeps all forty: this test is about
    # the slice the interface is sent, not about eligibility.
    ranked = cat.rank(
        offered(
            *[
                model(f"a/m{i}", prompt=0.02 + i * 0.001, completion=0.06, agentic=50)
                for i in range(40)
            ]
        )
    )
    shaped = cat.payload(ranked, current="a/m39")
    assert len(shaped["models"]) == cat.OFFERED_TO_THE_INTERFACE + 1
    assert any(row["slug"] == "a/m39" for row in shaped["models"])


def test_the_payload_keeps_unmeasured_benchmarks_as_nulls():
    # A missing key would render the same as a zero.
    ranked = cat.rank(offered(model("a/partial", coding=50)))
    (row,) = cat.payload(ranked, current="a/partial")["models"]
    assert row["percentiles"]["agentic_index"] is None
    assert row["percentiles"]["coding_index"] == 50


def test_a_malformed_row_costs_one_model_rather_than_the_catalogue():
    # Somebody else's payload. A new field or a null where a number was
    # expected should not empty the dropdown.
    parsed = cat.parse({"data": [model("a/good"), "not a dict", {"no": "id"}]})
    assert [m.slug for m in parsed] == ["a/good"]


def test_an_unreadable_catalogue_leaves_the_conversation_alone(monkeypatch):
    # Fails soft: a dropdown that cannot be drawn should not break the page,
    # and the conversation stays on the model it was already using.
    monkeypatch.setattr(cat, "_cached", ())
    monkeypatch.setattr(cat, "_cached_at", 0.0)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    assert cat.ranked_models(transport=httpx.MockTransport(refuse), force=True) == ()


def test_a_fetched_catalogue_is_ranked_and_cached(monkeypatch):
    monkeypatch.setattr(cat, "_cached", ())
    monkeypatch.setattr(cat, "_cached_at", 0.0)
    calls = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(
            200,
            json=payload(
                model("a/cheap", prompt=0.02, completion=0.06, agentic=80, intelligence=80, coding=80),
                model("a/dear", prompt=0.10, completion=0.30, agentic=80, intelligence=80, coding=80),
            ),
        )

    transport = httpx.MockTransport(answer)
    first = cat.ranked_models(transport=transport, force=True)
    assert slugs(first) == ["a/cheap", "a/dear"]

    # Second read is the cache, not a second request.
    second = cat.ranked_models(transport=transport)
    assert second == first
    assert len(calls) == 1


def test_an_anthropic_model_is_never_offered():
    # A standing instruction rather than an inference from today's prices. The
    # ceiling already excludes them by a wide margin, and would stop doing so
    # the day a cheap one shipped.
    assert offered(model("anthropic/claude-whatever", prompt=0.001, completion=0.001)) == ()


def test_a_model_far_dearer_than_the_cheapest_is_never_offered():
    # The ceiling is relative, because "too expensive" is a statement about
    # what else is on offer rather than about a number somebody typed.
    pool = offered(
        model("a/floor", prompt=0.02, completion=0.02),
        model("a/near", prompt=0.05, completion=0.05),
        model("a/far", prompt=2.0, completion=2.0),
    )
    kept = {m.slug for m in pool}
    assert kept == {"a/floor", "a/near"}


def test_the_ceiling_is_measured_against_models_that_survived_the_other_rules():
    # A free or toolless model must not set the baseline the rest are judged
    # against: it would drag the ceiling down to something nothing clears.
    pool = offered(
        model("a/free", prompt=0, completion=0),
        model("a/no-tools", prompt=0.001, completion=0.001, tools=False),
        model("a/real", prompt=0.05, completion=0.05),
        model("a/dearer", prompt=0.20, completion=0.20),
    )
    # `a/real` is the cheapest thing actually usable, so the ceiling is a
    # multiple of it and `a/dearer` at 4x survives.
    assert {m.slug for m in pool} == {"a/real", "a/dearer"}


def test_the_absolute_backstop_holds_when_everything_is_dear():
    # If the cheapest model on offer were itself expensive, a multiple of it
    # would still be expensive.
    assert offered(model("a/dear", prompt=MAX_OVER, completion=MAX_OVER)) == ()


# -- the router --------------------------------------------------------------


def test_the_router_is_choosable_but_is_never_a_model():
    # Two different questions. `allows` answers "may this be sent to
    # OpenRouter", which the sentinel never may; `offers` answers "may a person
    # select this", which it may.
    assert cat.offers(cat.ROUTER)
    assert not cat.allows(cat.ROUTER)


def test_the_router_sentinel_cannot_collide_with_a_real_model_id():
    # Every OpenRouter id is author/name. A bare word cannot be one, so there is
    # no version of this where the sentinel reaches a provider as a model name.
    assert "/" not in cat.ROUTER


def test_the_router_resolves_to_whatever_currently_tops_the_ranking(monkeypatch):
    # The point of choosing it: the rule is stored, so the answer follows the
    # ranking rather than a slug captured when the button was pressed.
    def catalogue_of(*rows):
        return cat.rank(offered(*rows))

    ranked = catalogue_of(
        model("a/good", prompt=0.02, completion=0.06, agentic=90, intelligence=90, coding=90),
        model("a/other", prompt=0.10, completion=0.30, agentic=10, intelligence=10, coding=10),
    )
    monkeypatch.setattr(cat, "ranked_models", lambda **_: ranked)
    assert cat.routed(cat.ROUTER) == "a/good"

    # The board moves; so does the router, with nothing re-chosen.
    moved = catalogue_of(
        model("a/newcomer", prompt=0.01, completion=0.03, agentic=95, intelligence=95, coding=95),
        model("a/good", prompt=0.02, completion=0.06, agentic=90, intelligence=90, coding=90),
    )
    monkeypatch.setattr(cat, "ranked_models", lambda **_: moved)
    assert cat.routed(cat.ROUTER) == "a/newcomer"


def test_a_pinned_model_is_returned_as_itself(monkeypatch):
    ranked = cat.rank(offered(model("a/pinned", agentic=60, intelligence=60, coding=60)))
    monkeypatch.setattr(cat, "ranked_models", lambda **_: ranked)
    assert cat.routed("a/pinned") == "a/pinned"


def test_a_pinned_model_that_has_gone_away_resolves_to_nothing(monkeypatch):
    # The caller's cue to fall back. A slug stored yesterday is not evidence
    # that the catalogue still offers it today.
    monkeypatch.setattr(cat, "ranked_models", lambda **_: ())
    assert cat.routed("a/withdrawn") is None


def test_the_payload_names_the_model_the_router_lands_on():
    # "Automatic" with nothing under it is how somebody ends up not knowing what
    # they are paying for, so the interface is handed the resolved model rather
    # than left to work it out.
    ranked = cat.rank(
        field(model("a/best", prompt=0.02, completion=0.06, agentic=95, intelligence=95, coding=95))
    )
    shaped = cat.payload(ranked, current=cat.ROUTER)
    assert shaped["current"] == cat.ROUTER
    assert shaped["router"]["slug"] == cat.ROUTER
    assert shaped["router"]["resolves"] == "a/best"
    assert shaped["router"]["label"] == "best"
    assert shaped["router"]["why"]


def test_the_router_reports_no_model_when_nothing_qualifies():
    # A real state the interface has to draw: with nothing measured, the router
    # has nothing to point at and says so rather than naming whatever is
    # cheapest.
    ranked = cat.rank(offered(model("a/unmeasured"), model("a/also-unmeasured")))
    shaped = cat.payload(ranked, current=cat.ROUTER)
    assert shaped["router"]["resolves"] is None
