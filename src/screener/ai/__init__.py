"""Narrative extraction through OpenRouter.

Condensing transcripts, pulling guidance changes out of filings, turning risk
sections into flags — the unstructured work a classifier cannot do. An LLM
never emits a sentiment number here; that is FinBERT's job, and a model asked
for a score will produce a confident one with nothing behind it.
"""

from screener.ai.config import RouterConfig
from screener.ai.models import DEFAULT_MODEL, MODELS, ModelInfo, resolve_model
from screener.ai.openrouter import AiError, Completion, complete

__all__ = [
    "AiError",
    "Completion",
    "DEFAULT_MODEL",
    "MODELS",
    "RouterConfig",
    "ModelInfo",
    "complete",
    "resolve_model",
]
