"""The models this project is willing to spend money on."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Display metadata for one model.

    The prices are indicative and for humans reading a log or a settings page.
    They are never used to compute a charge: OpenRouter returns the real cost
    per call, and a second price list maintained here would drift the moment a
    provider changed a rate.
    """

    label: str
    note: str
    input_per_m: float
    output_per_m: float


FLASH = "deepseek/deepseek-v4-flash"
PRO = "deepseek/deepseek-v4-pro"

MODELS: dict[str, ModelInfo] = {
    FLASH: ModelInfo(
        label="DeepSeek V4 Flash",
        note="Cheapest. Enough for summarising one filing section.",
        input_per_m=0.14,
        output_per_m=0.28,
    ),
    PRO: ModelInfo(
        label="DeepSeek V4 Pro",
        note="Roughly 3x the cost. Worth it for a whole transcript.",
        input_per_m=0.435,
        output_per_m=0.87,
    ),
}

# Cheapest capable model first. Anything that needs more has to ask.
DEFAULT_MODEL = FLASH


def resolve_model(requested: str | None) -> str:
    """The model to use, falling back to the default for anything unknown.

    An allow-list rather than a pass-through: a typo in a model id would
    otherwise reach OpenRouter, match some other provider's model, and bill at
    a rate nobody chose.
    """
    if requested in MODELS:
        # `requested` is narrowed to str by the membership test, but only
        # because MODELS is keyed on str — spelled out for the type checker.
        return str(requested)
    return DEFAULT_MODEL
