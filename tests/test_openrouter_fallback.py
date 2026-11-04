from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from google.genai.errors import ClientError, ServerError

from app.llm.extractor import (
    OPENROUTER_BASE_URL,
    OPENROUTER_DEFAULT_MODEL,
    GeminiIntentExtractor,
    GroceryIntent,
    GroceryItem,
    IntentExtractionError,
)


class FailingGeminiModels:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def generate_content(self, **kwargs: object) -> object:
        del kwargs
        self.calls += 1
        raise self.error


class FailingGeminiClient:
    def __init__(self, error: Exception) -> None:
        self.models = FailingGeminiModels(error)


class FakeOpenRouterClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> FakeOpenRouterClient:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
    ) -> httpx.Response:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def _openrouter_response(status: int, content: str = "") -> httpx.Response:
    payload: dict[str, object] = (
        {"choices": [{"message": {"content": content}}]} if status < 400 else {"error": {}}
    )
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", f"{OPENROUTER_BASE_URL}/chat/completions"),
    )


def _patch_openrouter(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> FakeOpenRouterClient:
    fake = FakeOpenRouterClient(response)

    def make_client(*, timeout: float) -> FakeOpenRouterClient:
        assert timeout == 60
        return fake

    monkeypatch.setattr("app.llm.extractor.httpx.Client", make_client)

    def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("app.llm.extractor.time.sleep", no_sleep)
    return fake


def _valid_content() -> str:
    return json.dumps(
        {
            "intent": "add_items",
            "items": [{"name": "rice", "quantity": 2, "unit": "kg"}],
            "needs_clarification": False,
            "clarification_question": None,
        }
    )


def _extractor(error: Exception) -> tuple[GeminiIntentExtractor, FailingGeminiClient]:
    client = FailingGeminiClient(error)
    extractor = GeminiIntentExtractor(
        client,
        model="gemini-3.8-flash",
        candidates=("gemini-3.8-flash",),
        openrouter_api_key="test-openrouter-secret",
    )
    return extractor, client


@pytest.mark.parametrize("gemini_status", [502, 503, 504])
def test_openrouter_fallback_extracts_and_validates_intent(
    monkeypatch: pytest.MonkeyPatch, gemini_status: int
) -> None:
    fake = _patch_openrouter(monkeypatch, _openrouter_response(200, _valid_content()))
    extractor, gemini = _extractor(ServerError(gemini_status, {"error": {"message": "busy"}}))

    result = extractor.extract("2 kg rice")

    assert isinstance(result, GroceryIntent)
    assert result.items == [GroceryItem(name="rice", quantity=2, unit="kg")]
    assert gemini.models.calls == 2
    assert extractor.selected_provider == "openrouter"
    assert extractor.selected_model == OPENROUTER_DEFAULT_MODEL
    call = fake.calls[0]
    assert call["url"] == f"{OPENROUTER_BASE_URL}/chat/completions"
    assert call["headers"] == {
        "Authorization": "Bearer test-openrouter-secret",
        "Content-Type": "application/json",
    }
    body = call["json"]
    assert isinstance(body, dict)
    assert body["model"] == OPENROUTER_DEFAULT_MODEL
    assert "response_format" not in body
    assert "tools" not in body


@pytest.mark.parametrize("content", ["not JSON", "{}"])
def test_malformed_openrouter_output_is_rejected(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _patch_openrouter(monkeypatch, _openrouter_response(200, content))
    extractor, _ = _extractor(ServerError(503, {"error": {"message": "busy"}}))

    with pytest.raises(IntentExtractionError, match="OpenRouter returned malformed"):
        extractor.extract("rice")


def test_openrouter_authentication_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "test-openrouter-secret"
    _patch_openrouter(monkeypatch, _openrouter_response(401))
    extractor, _ = _extractor(ServerError(503, {"error": {"message": "busy"}}))

    with pytest.raises(IntentExtractionError, match="authentication") as error:
        extractor.extract("rice")

    assert "OPENROUTER_API_KEY" in error.value.user_message
    assert secret not in error.value.user_message
    assert secret not in caplog.text


def test_openrouter_rate_limit_is_reported_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_openrouter(monkeypatch, _openrouter_response(429))
    extractor, _ = _extractor(ServerError(503, {"error": {"message": "busy"}}))

    with pytest.raises(IntentExtractionError, match="rate_limit"):
        extractor.extract("rice")

    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", [502, 503, 504])
def test_openrouter_provider_outage_is_reported(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    fake = _patch_openrouter(monkeypatch, _openrouter_response(status))
    extractor, _ = _extractor(ServerError(503, {"error": {"message": "busy"}}))

    with pytest.raises(IntentExtractionError, match="provider_unavailable"):
        extractor.extract("rice")

    assert len(fake.calls) == 1


def test_gemini_rate_limit_does_not_trigger_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("app.llm.extractor.time.sleep", no_sleep)

    def unexpected_openrouter(*args: Any, **kwargs: Any) -> None:
        pytest.fail("OpenRouter should not be called for Gemini HTTP 429")

    monkeypatch.setattr("app.llm.extractor.httpx.Client", unexpected_openrouter)
    extractor, gemini = _extractor(ClientError(429, {"error": {"message": "limited"}}))

    with pytest.raises(IntentExtractionError, match="rate limit"):
        extractor.extract("rice")

    assert gemini.models.calls == 2


def test_gemini_outage_without_openrouter_key_returns_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_sleep(delay: float) -> None:
        del delay

    def unexpected_openrouter(*args: Any, **kwargs: Any) -> None:
        pytest.fail("OpenRouter should not be called without its API key")

    monkeypatch.setattr("app.llm.extractor.time.sleep", no_sleep)
    monkeypatch.setattr("app.llm.extractor.httpx.Client", unexpected_openrouter)
    client = FailingGeminiClient(ServerError(503, {"error": {"message": "busy"}}))
    extractor = GeminiIntentExtractor(client, candidates=("gemini-3.8-flash",))

    with pytest.raises(IntentExtractionError, match="API request"):
        extractor.extract("rice")

    assert client.models.calls == 2


def test_gemini_other_transient_status_does_not_trigger_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_sleep(delay: float) -> None:
        del delay

    def unexpected_openrouter(*args: Any, **kwargs: Any) -> None:
        pytest.fail("OpenRouter should only follow Gemini HTTP 502/503/504")

    monkeypatch.setattr("app.llm.extractor.time.sleep", no_sleep)
    monkeypatch.setattr("app.llm.extractor.httpx.Client", unexpected_openrouter)
    extractor, gemini = _extractor(ServerError(500, {"error": {"message": "server error"}}))

    with pytest.raises(IntentExtractionError, match="API request"):
        extractor.extract("rice")

    assert gemini.models.calls == 2
