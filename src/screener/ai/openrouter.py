"""The OpenRouter client."""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from screener.ai.config import RouterConfig
from screener.ai.models import resolve_model

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Generous, because the work is a single long document rather than a chat turn.
DEFAULT_TIMEOUT = 120.0


class AiError(RuntimeError):
    """The call could not be made, or came back unusable."""


@dataclass(slots=True)
class Completion:
    """One model response, with what it cost."""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    finish_reason: str

    @property
    def truncated(self) -> bool:
        """Whether the model stopped because it ran out of room.

        Worth checking before parsing: a `length` stop cuts structured output
        mid-object, which then fails to parse for a reason that looks nothing
        like "the budget was too small".
        """
        return self.finish_reason == "length"


def complete(
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = 900,
    temperature: float = 0.4,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> Completion:
    """Send one prompt and return the response with its real cost.

    `transport` exists so tests can pass `httpx.MockTransport` and exercise
    this code without a key or a network. Production never passes it.
    """
    config = RouterConfig.from_env()
    if config.api_key is None:
        raise AiError("OPENROUTER_API_KEY is not set")

    chosen = resolve_model(model or config.model)
    payload: dict[str, Any] = {
        "model": chosen,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Ask OpenRouter to report what it charged. Without this the only way
        # to know the cost is a local price table, which is wrong the first
        # time a provider changes a rate and silently wrong thereafter.
        "usage": {"include": True},
    }

    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.post(
                API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    # Attribution on the OpenRouter dashboard, so spend can be
                    # traced back to this project rather than a bare key.
                    "HTTP-Referer": config.app_base_url,
                    "X-Title": "stock-aggregator",
                },
            )
    except httpx.HTTPError as exc:
        raise AiError(f"OpenRouter request failed: {exc}") from exc

    if response.status_code >= 400:
        raise AiError(f"OpenRouter returned HTTP {response.status_code}")

    body = response.json()
    if "error" in body:
        # A 200 carrying an error object is OpenRouter's way of reporting an
        # upstream provider failure, so status alone is not enough.
        raise AiError(f"OpenRouter reported an error: {body['error']}")

    choices = body.get("choices") or []
    if not choices:
        raise AiError("OpenRouter returned no choices")

    usage = body.get("usage") or {}
    completion = Completion(
        text=choices[0].get("message", {}).get("content") or "",
        model=body.get("model") or chosen,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cost_usd=float(usage.get("cost") or 0.0),
        finish_reason=choices[0].get("finish_reason") or "",
    )
    logger.info(
        "openrouter %s: %d+%d tokens, $%.5f",
        completion.model,
        completion.prompt_tokens,
        completion.completion_tokens,
        completion.cost_usd,
    )
    return completion
