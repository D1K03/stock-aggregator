import httpx
import pytest

from screener.ai import DEFAULT_MODEL, MODELS, AiError, complete, resolve_model
from screener.ai.models import PRO


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("AI_MODEL", raising=False)


def transport_returning(body, status=200):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


def completion_body(**overrides):
    body = {
        "model": DEFAULT_MODEL,
        "choices": [{"message": {"content": "a summary"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "cost": 0.000042},
    }
    body.update(overrides)
    return body


def test_an_unknown_model_id_falls_back_to_the_cheap_default():
    # A typo would otherwise reach OpenRouter, match some other provider's
    # model, and bill at a rate nobody chose.
    assert resolve_model("deepseek/deepseek-v4-flashh") == DEFAULT_MODEL
    assert resolve_model(None) == DEFAULT_MODEL
    assert resolve_model(PRO) == PRO
    assert DEFAULT_MODEL in MODELS


def test_the_cost_is_read_from_the_response_rather_than_computed():
    # A local price table is wrong the first time a provider changes a rate,
    # and silently wrong thereafter.
    result = complete(
        system="s", user="u", transport=transport_returning(completion_body())
    )
    assert result.cost_usd == 0.000042
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 30
    assert result.text == "a summary"


def test_a_response_cut_short_by_the_token_budget_is_flagged_as_truncated():
    # A `length` stop cuts structured output mid-object, which then fails to
    # parse for a reason that looks nothing like "the budget was too small".
    body = completion_body(
        choices=[{"message": {"content": "{\"a\":"}, "finish_reason": "length"}]
    )
    assert complete(system="s", user="u", transport=transport_returning(body)).truncated


def test_an_error_object_in_a_two_hundred_is_still_an_error():
    # OpenRouter reports an upstream provider failure this way, so the status
    # code alone is not enough to know the call worked.
    body = {"error": {"message": "upstream is down"}}
    with pytest.raises(AiError, match="upstream is down"):
        complete(system="s", user="u", transport=transport_returning(body))


def test_a_response_with_no_choices_is_reported_rather_than_returned_empty():
    with pytest.raises(AiError, match="no choices"):
        complete(
            system="s", user="u", transport=transport_returning(completion_body(choices=[]))
        )


def test_the_layer_is_inert_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(AiError, match="OPENROUTER_API_KEY is not set"):
        complete(system="s", user="u")
