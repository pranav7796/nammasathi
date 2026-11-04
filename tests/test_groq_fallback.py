from __future__ import annotations

import json
from typing import cast

import httpx
import pytest
from google.genai.errors import ClientError, ServerError

from app.llm.extractor import (
    GROQ_BASE_URL,
    GROQ_DEFAULT_MODEL,
    OPENROUTER_BASE_URL,
    OPENROUTER_DEFAULT_MODEL,
    GeminiIntentExtractor,
    IntentExtractionError,
)


class FailingGeminiModels:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        raise self.error


class FakeClient:
    def __init__(self, responses: dict[str, list[httpx.Response]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> FakeClient:
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
        return self.responses[url].pop(0)


def _response(url: str, status: int, content: str | None = None) -> httpx.Response:
    payload: dict[str, object] = (
        {"choices": [{"message": {"content": content}}]}
        if content is not None
        else {"error": {"message": "provider error"}}
    )
    return httpx.Response(status, json=payload, request=httpx.Request("POST", url))


def _valid_content() -> str:
    return json.dumps(
        {
            "intent": "add_items",
            "items": [{"name": "rice", "quantity": 2, "unit": "kg"}],
            "needs_clarification": False,
            "clarification_question": None,
        }
    )


def _quota_error() -> ClientError:
    return ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})


def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gemini_error: Exception | None = None,
    groq_responses: list[httpx.Response] | None = None,
    openrouter_responses: list[httpx.Response] | None = None,
) -> tuple[GeminiIntentExtractor, FailingGeminiModels, FakeClient]:
    groq_url = f"{GROQ_BASE_URL}/chat/completions"
    openrouter_url = f"{OPENROUTER_BASE_URL}/chat/completions"
    responses = {
        groq_url: groq_responses or [],
        openrouter_url: openrouter_responses or [],
    }
    fake_http = FakeClient(responses)

    def make_http_client(**kwargs: object) -> FakeClient:
        del kwargs
        return fake_http

    def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("app.llm.extractor.httpx.Client", make_http_client)
    monkeypatch.setattr("app.llm.extractor.time.sleep", no_sleep)
    gemini = FailingGeminiModels(gemini_error or _quota_error())
    extractor = GeminiIntentExtractor(
        type("Client", (), {"models": gemini})(),
        candidates=("gemini-primary", "gemini-secondary"),
        groq_api_key="groq-test-secret",
        openrouter_api_key="openrouter-test-secret",
    )
    return extractor, gemini, fake_http


def test_quota_exhaustion_immediately_uses_groq_and_preserves_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groq_url = f"{GROQ_BASE_URL}/chat/completions"
    extractor, gemini, http = _build(
        monkeypatch,
        groq_responses=[_response(groq_url, 200, _valid_content())],
    )

    result = extractor.extract("ಎರಡು ಕಿಲೋ ಅಕ್ಕಿ", language="kn-IN")

    assert result.items[0].name == "rice"
    assert len(gemini.calls) == 1
    assert extractor.selected_provider == "groq"
    assert extractor.selected_model == GROQ_DEFAULT_MODEL
    call = http.calls[0]
    assert call["url"] == groq_url
    assert call["headers"] == {
        "Authorization": "Bearer groq-test-secret",
        "Content-Type": "application/json",
    }
    body = cast(dict[str, object], call["json"])
    assert body["model"] == GROQ_DEFAULT_MODEL
    assert "in Kannada" in str(body["messages"])
    response_format = cast(dict[str, object], body["response_format"])
    assert response_format["type"] == "json_schema"
    assert len(http.calls) == 1


@pytest.mark.parametrize("bad_content", ["not JSON", "{}"])
def test_malformed_groq_response_falls_back_to_openrouter(
    monkeypatch: pytest.MonkeyPatch, bad_content: str
) -> None:
    groq_url = f"{GROQ_BASE_URL}/chat/completions"
    openrouter_url = f"{OPENROUTER_BASE_URL}/chat/completions"
    extractor, _, http = _build(
        monkeypatch,
        groq_responses=[_response(groq_url, 200, bad_content)],
        openrouter_responses=[_response(openrouter_url, 200, _valid_content())],
    )

    result = extractor.extract("two kilos of rice", language="en-IN")

    assert result.items[0].quantity == 2
    assert extractor.selected_provider == "openrouter"
    assert extractor.selected_model == OPENROUTER_DEFAULT_MODEL
    assert [call["url"] for call in http.calls] == [groq_url, openrouter_url]
    assert "in English" in str(http.calls[0]["json"]["messages"])  # type: ignore[index]


def test_groq_authentication_failure_falls_back_without_exposing_keys(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    groq_url = f"{GROQ_BASE_URL}/chat/completions"
    openrouter_url = f"{OPENROUTER_BASE_URL}/chat/completions"
    extractor, _, http = _build(
        monkeypatch,
        groq_responses=[_response(groq_url, 401)],
        openrouter_responses=[_response(openrouter_url, 200, _valid_content())],
    )

    extractor.extract("दो किलो चावल", language="hi-IN")

    assert extractor.selected_provider == "openrouter"
    assert "in Hindi" in str(http.calls[0]["json"]["messages"])  # type: ignore[index]
    assert "groq-test-secret" not in caplog.text
    assert "openrouter-test-secret" not in caplog.text


def test_if_groq_and_openrouter_both_fail_returns_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groq_url = f"{GROQ_BASE_URL}/chat/completions"
    openrouter_url = f"{OPENROUTER_BASE_URL}/chat/completions"
    extractor, gemini, _ = _build(
        monkeypatch,
        groq_responses=[_response(groq_url, 401)],
        openrouter_responses=[_response(openrouter_url, 503)],
    )

    with pytest.raises(IntentExtractionError, match="OpenRouter request failed") as caught:
        extractor.extract("rice")

    assert len(gemini.calls) == 1
    assert "groq-test-secret" not in str(caught.value)
    assert "openrouter-test-secret" not in str(caught.value)


def test_gemini_server_error_uses_groq_before_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groq_url = f"{GROQ_BASE_URL}/chat/completions"
    extractor, gemini, http = _build(
        monkeypatch,
        gemini_error=ServerError(503, {"error": {"message": "busy"}}),
        groq_responses=[_response(groq_url, 200, _valid_content())],
    )

    extractor.extract("rice")

    assert len(gemini.calls) == 4
    assert len(http.calls) == 1
    assert http.calls[0]["url"] == groq_url
