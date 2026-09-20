"""Narrative extraction through OpenRouter.

Condensing transcripts, pulling guidance changes out of filings, turning risk
sections into flags — the unstructured work a classifier cannot do. An LLM
never emits a sentiment number here; that is FinBERT's job, and a model asked
for a score will produce a confident one with nothing behind it.
"""

from screener.ai.catalogue import (
    ROUTER, Offered, Ranked, allows, find, offers, ranked_models, recommend,
    routed, why,
)
from screener.ai.catalogue import payload as catalogue_payload
from screener.ai.config import RouterConfig
from screener.ai.models import DEFAULT_MODEL, MODELS, ModelInfo, resolve_model
from screener.ai.openrouter import AiError, Completion, ToolCall, complete, converse

__all__ = [
    "AiError",
    "Completion",
    "DEFAULT_MODEL",
    "MODELS",
    "ROUTER",
    "Offered",
    "Ranked",
    "RouterConfig",
    "ModelInfo",
    "ToolCall",
    "allows",
    "catalogue_payload",
    "complete",
    "converse",
    "find",
    "offers",
    "ranked_models",
    "recommend",
    "resolve_model",
    "routed",
    "why",
]
