from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from google.genai.errors import ClientError, ServerError

from app.llm.extractor import (
    GEMINI_MAX_ATTEMPTS,
    GEMINI_TIMEOUT_SECONDS,
    GeminiIntentExtractor,
    GroceryIntent,
    GroceryItem,
    IntentExtractionError,
)


class FakeResponse:
    def __init__(self, parsed: object = None, text: str | None = None) -> None:
        self.parsed = parsed
        self.text = text


class FakeModels:
    def __init__(self, responses: Iterator[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.list_calls = 0

    def list(self) -> list[object]:
        self.list_calls += 1
        return [
            SimpleNamespace(
                name="models/gemini-3.8-flash",
                display_name="Gemini 3.8 Flash",
                supported_actions=["generateContent"],
            )
        ]

    def generate_content(self, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        return next(self.responses)


class FakeClient:
    def __init__(self, responses: Iterator[FakeResponse]) -> None:
        self.models = FakeModels(responses)


class FailingModels:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def generate_content(self, **kwargs: object) -> FakeResponse:
        self.calls += 1
        raise self.error


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.models = FailingModels(error)


class ScriptedModels:
    def __init__(self, outcomes: Iterator[object], models: list[object] | None = None) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []
        self.available_models = models or []
        self.list_calls = 0

    def list(self) -> list[object]:
        self.list_calls += 1
        return self.available_models

    def generate_content(self, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if not isinstance(outcome, FakeResponse):
            raise TypeError("invalid scripted response")
        return outcome


class ScriptedClient:
    def __init__(self, outcomes: Iterator[object], models: list[object] | None = None) -> None:
        self.models = ScriptedModels(outcomes, models)


def _no_sleep(delay: float) -> None:
    del delay


def _no_jitter(low: float, high: float) -> float:
    del low, high
    return 0.0


def test_gemini_response_is_validated_without_network() -> None:
    client = FakeClient(
        iter(
            [
                FakeResponse(
                    parsed=GroceryIntent(
                        intent="add_items",
                        items=[GroceryItem(name="rice", quantity=2, unit="kg")],
                    )
                )
            ]
        )
    )
    extractor = GeminiIntentExtractor(client)

    result = extractor.extract("2 kg rice")

    assert result.items[0].name == "rice"
    assert client.models.calls[0]["model"] == "gemini-3.8-flash"


def test_malformed_gemini_response_is_rejected() -> None:
    client = FakeClient(iter([FakeResponse(text='{"intent":"add_items","items":[]}')]))
    extractor = GeminiIntentExtractor(client)

    with pytest.raises(IntentExtractionError):
        extractor.extract("something")


def test_unknown_fields_are_rejected() -> None:
    client = FakeClient(
        iter([FakeResponse(text='{"intent":"add_items","items":[],"checkout":true}')])
    )

    with pytest.raises(IntentExtractionError):
        GeminiIntentExtractor(client).extract("rice")


def test_pydantic_validation_error_is_rejected() -> None:
    client = FakeClient(
        iter(
            [
                FakeResponse(
                    text='{"intent":"add_items","items":[{"name":"rice","quantity":0,"unit":"kg"}],"needs_clarification":false,"clarification_question":null}'
                )
            ]
        )
    )

    with pytest.raises(IntentExtractionError, match="malformed"):
        GeminiIntentExtractor(client).extract("rice")


def test_authentication_failure_is_classified() -> None:
    client = FakeClient(iter([]))

    def fail(**kwargs: object) -> FakeResponse:
        raise ClientError(401, {"error": {"message": "invalid API key"}})

    client.models.generate_content = fail
    with pytest.raises(IntentExtractionError, match="authentication") as caught:
        GeminiIntentExtractor(client).extract("rice")
    assert "GEMINI_API_KEY" in caught.value.user_message
    assert "invalid API key" not in caught.value.user_message


def test_rate_limit_is_classified() -> None:
    client = FakeClient(iter([]))

    def fail(**kwargs: object) -> FakeResponse:
        raise ClientError(429, {"error": {"message": "rate limited"}})

    client.models.generate_content = fail
    with pytest.raises(IntentExtractionError, match="rate limit"):
        GeminiIntentExtractor(client).extract("rice")


def test_server_api_failure_is_classified() -> None:
    client = FakeClient(iter([]))

    def fail(**kwargs: object) -> FakeResponse:
        raise ServerError(503, {"error": {"message": "temporarily unavailable"}})

    client.models.generate_content = fail
    with pytest.raises(IntentExtractionError, match="API request"):
        GeminiIntentExtractor(client).extract("rice")


def test_unavailable_model_is_classified() -> None:
    client = FakeClient(iter([]))

    def fail(**kwargs: object) -> FakeResponse:
        raise ClientError(404, {"error": {"message": "model unavailable"}})

    client.models.generate_content = fail
    with pytest.raises(IntentExtractionError, match="model") as caught:
        GeminiIntentExtractor(client).extract("rice")
    assert "GEMINI_MODEL" in caught.value.user_message


def test_missing_api_key_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(IntentExtractionError, match="not configured"):
        GeminiIntentExtractor.from_env()


def test_timeout_is_bounded_and_user_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.extractor.time.sleep", _no_sleep)
    client = FailingClient(httpx.ReadTimeout("timed out"))

    with pytest.raises(IntentExtractionError) as caught:
        GeminiIntentExtractor(client).extract("rice")

    assert client.models.calls == GEMINI_MAX_ATTEMPTS
    assert "too long" in caught.value.user_message


def test_connection_failure_is_bounded_and_user_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.extractor.time.sleep", _no_sleep)
    client = FailingClient(httpx.ConnectError("connection failed"))

    with pytest.raises(IntentExtractionError) as caught:
        GeminiIntentExtractor(client).extract("rice")

    assert client.models.calls == GEMINI_MAX_ATTEMPTS
    assert "connect" in caught.value.user_message.lower()


def test_retry_exhaustion_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.llm.extractor.time.sleep", sleeps.append)
    monkeypatch.setattr("app.llm.extractor.random.uniform", _no_jitter)
    client = FailingClient(ServerError(503, {"error": {"message": "busy"}}))

    with pytest.raises(IntentExtractionError) as caught:
        GeminiIntentExtractor(client).extract("rice")

    assert client.models.calls == GEMINI_MAX_ATTEMPTS
    assert "temporarily unavailable" in caught.value.user_message
    assert sleeps == [0.5]


def test_from_env_configures_timeout_and_single_sdk_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    fake_client = FakeClient(iter([]))

    def make_client(**kwargs: object) -> FakeClient:
        captured.update(kwargs)
        return fake_client

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr("app.llm.extractor.genai.Client", make_client)

    extractor = GeminiIntentExtractor.from_env()

    assert extractor.model == "gemini-3.8-flash"
    assert fake_client.models.list_calls == 1
    options = cast(Any, captured["http_options"])
    assert options.timeout == GEMINI_TIMEOUT_SECONDS * 1000
    assert options.retry_options.attempts == 1


def _valid_response() -> FakeResponse:
    return FakeResponse(
        parsed=GroceryIntent(
            intent="add_items",
            items=[GroceryItem(name="rice", quantity=2, unit="kg")],
        )
    )


def _model(name: str, actions: list[str] | None = None, display: str = "") -> object:
    return SimpleNamespace(
        name=name,
        display_name=display or name,
        supported_actions=actions or ["generateContent"],
    )


def test_preferred_model_succeeds_first() -> None:
    client = ScriptedClient(iter([_valid_response()]))
    extractor = GeminiIntentExtractor(
        client,
        model="models/preferred-flash",
        candidates=("models/preferred-flash", "models/fallback-flash"),
    )

    result = extractor.extract("rice")

    assert result.items[0].name == "rice"
    assert client.models.calls[0]["model"] == "models/preferred-flash"
    assert extractor.selected_model == "models/preferred-flash"


def test_503_falls_back_to_next_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.extractor.time.sleep", _no_sleep)
    first = ServerError(503, {"error": {"message": "busy"}})
    client = ScriptedClient(iter([first, first, _valid_response()]))
    extractor = GeminiIntentExtractor(
        client,
        candidates=("models/first", "models/second"),
    )

    extractor.extract("rice")

    assert [call["model"] for call in client.models.calls] == [
        "models/first",
        "models/first",
        "models/second",
    ]


def test_429_falls_back_to_next_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.extractor.time.sleep", _no_sleep)
    first = ClientError(429, {"error": {"message": "busy"}})
    client = ScriptedClient(iter([first, first, _valid_response()]))
    extractor = GeminiIntentExtractor(client, candidates=("models/first", "models/second"))

    extractor.extract("rice")

    assert client.models.calls[-1]["model"] == "models/second"


def test_structured_output_incompatibility_marks_model_and_falls_back() -> None:
    incompatible = ClientError(
        400, {"error": {"message": "response schema is unsupported for this model"}}
    )
    client = ScriptedClient(iter([incompatible, _valid_response()]))
    extractor = GeminiIntentExtractor(client, candidates=("models/first", "models/second"))

    extractor.extract("rice")

    assert extractor.candidates == ("models/first", "models/second")
    assert extractor.selected_model == "models/second"
    assert "models/first" in extractor.incompatible_models


def test_invalid_api_key_does_not_fallback() -> None:
    client = ScriptedClient(iter([ClientError(401, {"error": {"message": "invalid API key"}})]))
    extractor = GeminiIntentExtractor(client, candidates=("models/first", "models/second"))

    with pytest.raises(IntentExtractionError, match="authentication"):
        extractor.extract("rice")

    assert len(client.models.calls) == 1


def test_no_available_compatible_models() -> None:
    extractor = GeminiIntentExtractor(ScriptedClient(iter([])), candidates=())

    with pytest.raises(IntentExtractionError, match="No compatible"):
        extractor.extract("rice")


def test_all_models_fail_after_bounded_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.extractor.time.sleep", _no_sleep)
    failure = ServerError(503, {"error": {"message": "busy"}})
    client = ScriptedClient(iter([failure, failure, failure, failure]))
    extractor = GeminiIntentExtractor(client, candidates=("models/first", "models/second"))

    with pytest.raises(IntentExtractionError, match="API request"):
        extractor.extract("rice")

    assert len(client.models.calls) == 4


def test_malformed_output_does_not_fallback() -> None:
    client = ScriptedClient(
        iter([FakeResponse(text='{"intent":"add_items","items":[]}'), _valid_response()])
    )
    extractor = GeminiIntentExtractor(client, candidates=("models/first", "models/second"))

    with pytest.raises(IntentExtractionError, match="items"):
        extractor.extract("rice")

    assert len(client.models.calls) == 1


def test_discovery_is_cached_and_refreshable() -> None:
    models = [
        _model("models/gemini-3.8-flash", display="Gemini 3.8 Flash"),
        _model("models/gemini-3.6-flash", display="Gemini 3.6 Flash"),
        _model("models/gemini-2.5-flash", display="Gemini 2.5 Flash"),
        _model("models/text-embedding", display="Text Embedding"),
        _model("models/gemini-no-content", actions=["countTokens"]),
    ]
    client = ScriptedClient(iter([_valid_response()]), models=models)
    extractor = GeminiIntentExtractor(
        client,
        model="gemini-3.8-flash",
        candidates=(),
    )

    assert extractor.refresh_models() == ("models/gemini-3.8-flash", "models/gemini-3.6-flash")
    extractor.extract("rice")
    assert client.models.list_calls == 1
    extractor.refresh_models()
    assert client.models.list_calls == 2


def test_discovery_prioritizes_stable_flash_models_and_excludes_legacy() -> None:
    models = [
        _model("models/gemini-2.5-flash"),
        _model("models/gemini-3.5-flash-lite"),
        _model("models/gemini-3.6-flash"),
        _model("models/gemini-3.8-flash"),
        _model("models/gemini-3.7-flash"),
        _model("models/gemini-3.5-flash"),
    ]
    extractor = GeminiIntentExtractor(
        ScriptedClient(iter([]), models=models),
        model="gemini-3.8-flash",
        candidates=(),
    )

    assert extractor.refresh_models() == (
        "models/gemini-3.8-flash",
        "models/gemini-3.7-flash",
        "models/gemini-3.6-flash",
        "models/gemini-3.5-flash",
        "models/gemini-3.5-flash-lite",
    )
    assert "models/gemini-2.5-flash" not in extractor.candidates


def test_36_flash_is_tried_and_legacy_25_flash_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.extractor.time.sleep", _no_sleep)
    unavailable = ServerError(503, {"error": {"message": "busy"}})
    models = [
        _model("models/gemini-3.8-flash"),
        _model("models/gemini-3.6-flash"),
        _model("models/gemini-2.5-flash"),
    ]
    client = ScriptedClient(iter([unavailable, unavailable, _valid_response()]), models=models)
    extractor = GeminiIntentExtractor(
        client,
        model="gemini-3.8-flash",
        candidates=(),
    )
    extractor.refresh_models()

    extractor.extract("rice")

    assert [call["model"] for call in client.models.calls] == [
        "models/gemini-3.8-flash",
        "models/gemini-3.8-flash",
        "models/gemini-3.6-flash",
    ]


def test_preferred_model_is_first_and_not_duplicated() -> None:
    models = [
        _model("gemini-3.6-flash"),
        _model("models/gemini-3.8-flash"),
        _model("models/gemini-3.6-flash"),
        _model("models/gemini-2.5-flash"),
    ]
    extractor = GeminiIntentExtractor(
        ScriptedClient(iter([]), models=models),
        model="models/gemini-3.6-flash",
        candidates=(),
    )

    assert extractor.refresh_models() == (
        "gemini-3.6-flash",
        "models/gemini-3.8-flash",
    )


@pytest.mark.parametrize(
    ("language", "expected_language_name"),
    [("kn-IN", "Kannada"), ("hi-IN", "Hindi"), ("en-IN", "English")],
)
def test_language_passed_to_prompt_builder(language: str, expected_language_name: str) -> None:
    client = FakeClient(iter([_valid_response()]))
    extractor = GeminiIntentExtractor(client)
    intent = extractor.extract("दो किलो चावल", language=language)
    assert intent.intent == "add_items"
    assert intent.items[0].name == "rice"
    assert client.models.calls
    prompt = str(client.models.calls[-1].get("contents", ""))
    assert f"in {expected_language_name}" in prompt
