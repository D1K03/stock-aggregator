"""The models this project is willing to spend money on."""

from dataclasses import dataclass

from screener.ai.catalogue import allows


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Display metadata for one model.

    The prices are indicative and for humans reading a log or a settings page.
    They are never used to compute a charge: OpenRouter returns the real cost
    per call, and a second price list maintained here would drift the moment a
    provider changed a rate.

    It drifted, which is the point of `catalogue.py`. Every figure below was
    wrong by a factor of two or three within a few months of being typed —
    Solar had tripled and was still described here as the cheapest of the three
    — so the dashboard reads live prices and this stays as the fallback for
    when OpenRouter cannot be reached at all.
    """

    label: str
    note: str
    input_per_m: float
    output_per_m: float


FLASH = "deepseek/deepseek-v4-flash"
PRO = "deepseek/deepseek-v4-pro"
SOLAR = "upstage/solar-pro4"

MODELS: dict[str, ModelInfo] = {
    FLASH: ModelInfo(
        label="DeepSeek V4 Flash",
        note="Enough for summarising one filing section.",
        input_per_m=0.036,
        output_per_m=0.072,
    ),
    PRO: ModelInfo(
        label="DeepSeek V4 Pro",
        note="Roughly 12x Flash. Worth it for a whole transcript.",
        input_per_m=0.422,
        output_per_m=0.845,
    ),
    SOLAR: ModelInfo(
        label="Solar Pro 4",
        note="524k context. The bot's configured fallback.",
        input_per_m=0.090,
        output_per_m=0.360,
    ),
}

# Cheapest capable model first. Anything that needs more has to ask.
DEFAULT_MODEL = FLASH


def resolve_model(requested: str | None) -> str:
    """The model to use, falling back to the default for anything unknown.

    An allow-list rather than a pass-through: a typo in a model id would
    otherwise reach OpenRouter, match some other provider's model, and bill at
    a rate nobody chose.

    Two lists, consulted in this order for a reason. The table above is the
    fast path and needs no network, which is what the Discord bot and the
    ingest-side callers take: they run on a slug from configuration, and a
    process that posts one summary a night should not reach OpenRouter to
    confirm a name it already knows.

    The live catalogue is the second, and exists because the dashboard offers a
    real choice from four hundred models that cannot be typed out here. It is
    still an allow-list — the browser can only pick what the server itself
    fetched, and the eligibility rules in `catalogue.py` are what keep a click
    from routing to something that cannot call a tool or costs a day's cap in
    one message.
    """
    if requested in MODELS:
        # `requested` is narrowed to str by the membership test, but only
        # because MODELS is keyed on str — spelled out for the type checker.
        return str(requested)
    if requested is not None and allows(requested):
        return requested
    return DEFAULT_MODEL
