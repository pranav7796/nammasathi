"""Strict Gemini intent extraction with no commerce or checkout capability."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

GEMINI_TIMEOUT_SECONDS = 60
GEMINI_MAX_ATTEMPTS = 2
GEMINI_MAX_FALLBACK_MODELS = 2
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-20b"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_OPENROUTER_FALLBACK_STATUS_CODES = {502, 503, 504}
_RETRY_INITIAL_DELAY_SECONDS = 0.5
_RETRY_MAX_DELAY_SECONDS = 2.0
_RETRY_JITTER_RATIO = 0.25
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_STABLE_FLASH_FALLBACKS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)
_FLASH_FALLBACK_RANK = {name: rank for rank, name in enumerate(_STABLE_FLASH_FALLBACKS)}
_UNSUITABLE_MODEL_TERMS = {
    "audio",
    "embedding",
    "image",
    "imagen",
    "lyria",
    "speech",
    "tts",
    "veo",
    "video",
}


class IntentExtractionError(Exception):
    """A model response could not safely become a structured intent."""

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class GroceryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(gt=0, le=1000)
    unit: str | None = Field(default=None, max_length=20)
    # Common English grocery term for catalogue search; never a product ID or brand guess.
    search_term: str | None = Field(default=None, max_length=120)


class GroceryIntent(BaseModel):
    """The only data Gemini may provide to application code."""

    model_config = ConfigDict(extra="forbid", strict=True)

    intent: str = Field(min_length=1, max_length=30)
    items: list[GroceryItem]
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=240)
    # 1-based choice among options the application read out; validated by the state machine.
    option_number: int | None = Field(default=None, ge=1, le=10)
    # Which capability this is for: grocery, food, or dining. Routing validates it.
    domain: str | None = Field(default=None, max_length=20)
    # Table bookings only. A date is YYYY-MM-DD and a time is 24-hour HH:MM.
    query: str | None = Field(default=None, max_length=120)
    area: str | None = Field(default=None, max_length=80)
    date: str | None = Field(default=None, max_length=10)
    time: str | None = Field(default=None, max_length=5)
    guests: int | None = Field(default=None, ge=1, le=20)


@dataclass(frozen=True)
class ClarificationContext:
    """The question the user is answering. Holds catalogue names only, no personal data."""

    item: str = ""
    options: tuple[str, ...] = ()
    active_domain: str = ""
    today: str = ""


class _GeminiResponse(Protocol):
    parsed: Any
    text: str | None


_ALLOWED_INTENTS = {
    "book_table",
    "select_option",
    "add_items",
    "remove_items",
    "change_quantity",
    "reorder_usual",
    "done",
    "confirm",
    "reject",
    "cancel",
    "unclear",
}

# Keep provider constraints out of this schema: google-genai 2.34.0 rejects
# Pydantic's exclusiveMinimum/minLength JSON-schema keywords when constructing
# GenerateContentConfig. GroceryIntent remains the authoritative validator.
_GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING"},
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "quantity": {"type": "NUMBER"},
                    "unit": {"type": "STRING", "nullable": True},
                    "search_term": {"type": "STRING", "nullable": True},
                },
                "required": ["name", "quantity", "unit"],
            },
        },
        "needs_clarification": {"type": "BOOLEAN"},
        "clarification_question": {"type": "STRING", "nullable": True},
        "option_number": {"type": "INTEGER", "nullable": True},
        "domain": {"type": "STRING", "nullable": True},
        "query": {"type": "STRING", "nullable": True},
        "area": {"type": "STRING", "nullable": True},
        "date": {"type": "STRING", "nullable": True},
        "time": {"type": "STRING", "nullable": True},
        "guests": {"type": "INTEGER", "nullable": True},
    },
    "required": ["intent", "items", "needs_clarification", "clarification_question"],
}

_GROQ_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit": {"type": ["string", "null"]},
                    "search_term": {"type": ["string", "null"]},
                },
                "required": ["name", "quantity", "unit", "search_term"],
                "additionalProperties": False,
            },
        },
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": ["string", "null"]},
        "option_number": {"type": ["integer", "null"]},
        "domain": {"type": ["string", "null"]},
        "query": {"type": ["string", "null"]},
        "area": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"]},
        "time": {"type": ["string", "null"]},
        "guests": {"type": ["integer", "null"]},
    },
    "required": [
        "intent",
        "items",
        "needs_clarification",
        "clarification_question",
        "option_number",
        "domain",
        "query",
        "area",
        "date",
        "time",
        "guests",
    ],
    "additionalProperties": False,
}


class GeminiIntentExtractor:
    """Extract intent only; it cannot call commerce or authorize checkout."""

    def __init__(
        self,
        client: Any,
        model: str = "gemini-3.8-flash",
        prompt_builder: Callable[[str, str], str] | None = None,
        candidates: Iterable[str] | None = None,
        max_fallback_models: int = GEMINI_MAX_FALLBACK_MODELS,
        groq_api_key: str | None = None,
        groq_model: str = GROQ_DEFAULT_MODEL,
        openrouter_api_key: str | None = None,
        openrouter_model: str = OPENROUTER_DEFAULT_MODEL,
        timeout_seconds: int = GEMINI_TIMEOUT_SECONDS,
        max_attempts: int = GEMINI_MAX_ATTEMPTS,
    ) -> None:
        self.client = client
        self.max_attempts = max_attempts
        self.model = model
        self.prompt_builder = prompt_builder or _prompt
        self.max_fallback_models = max_fallback_models
        self.groq_api_key = groq_api_key or None
        self.groq_model = groq_model
        self.openrouter_api_key = openrouter_api_key or None
        self.openrouter_model = openrouter_model
        self.timeout_seconds = timeout_seconds
        self._candidates = tuple(candidates) if candidates is not None else (model,)
        self._incompatible_models: set[str] = set()
        self.selected_model: str | None = None
        self.selected_provider: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        allow_logged_fallback: bool = True,
        provider_fallbacks: bool = True,
        timeout_seconds: int | None = None,
        max_fallback_models: int | None = None,
        max_attempts: int = GEMINI_MAX_ATTEMPTS,
    ) -> GeminiIntentExtractor:
        """`allow_logged_fallback=False` drops OpenRouter, whose free model may log requests.

        `provider_fallbacks=False` leaves failing over to `app.llm.chain`.
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise IntentExtractionError(
                "GEMINI_API_KEY is not configured",
                user_message="Gemini is not configured. Set GEMINI_API_KEY and try again.",
            )
        model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        if timeout_seconds is None:
            timeout_seconds = _timeout_seconds_from_env()
        if max_fallback_models is None:
            max_fallback_models = _max_fallback_models_from_env()
        groq_model = os.environ.get("GROQ_MODEL", GROQ_DEFAULT_MODEL).strip()
        if not groq_model:
            raise IntentExtractionError(
                "GROQ_MODEL must not be empty",
                user_message="Groq model configuration is invalid.",
            )
        openrouter_model = os.environ.get("OPENROUTER_MODEL", OPENROUTER_DEFAULT_MODEL).strip()
        if not openrouter_model:
            raise IntentExtractionError(
                "OPENROUTER_MODEL must not be empty",
                user_message="OpenRouter model configuration is invalid.",
            )
        try:
            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    timeout=timeout_seconds * 1000,
                    # Retries are bounded by _generate_content so transport and
                    # transient API failures have one clear retry budget.
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        except Exception as exc:
            _log_failure(exc, "client initialization")
            raise IntentExtractionError(
                "Gemini client initialization failed",
                user_message="Gemini could not be initialized. Check the local configuration.",
            ) from exc
        extractor = cls(
            client,
            model=model,
            candidates=(),
            max_fallback_models=max_fallback_models,
            groq_api_key=(
                (os.environ.get("GROQ_API_KEY", "").strip() or None) if provider_fallbacks else None
            ),
            groq_model=groq_model,
            openrouter_api_key=(
                (os.environ.get("OPENROUTER_API_KEY", "").strip() or None)
                if allow_logged_fallback and provider_fallbacks
                else None
            ),
            openrouter_model=openrouter_model,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        try:
            extractor.refresh_models()
        except Exception as exc:
            raise _as_intent_error(exc, "Gemini model discovery failed") from exc
        if not extractor._candidates:
            raise IntentExtractionError(
                "No compatible Gemini models are available",
                user_message="No compatible Gemini text model is currently available.",
            )
        return extractor

    @property
    def candidates(self) -> tuple[str, ...]:
        """The cached, ordered model candidates for this process instance."""
        return self._candidates

    @property
    def incompatible_models(self) -> frozenset[str]:
        return frozenset(self._incompatible_models)

    def refresh_models(self) -> tuple[str, ...]:
        """Refresh the cached compatible model list without recreating the client."""
        models = self.client.models.list()
        discovered = _compatible_models(models, self.model)
        self._candidates = tuple(discovered)
        self._incompatible_models.clear()
        logger.info("Gemini model discovery completed candidates=%d", len(self._candidates))
        return self._candidates

    def extract(
        self,
        text: str,
        *,
        language: str = "kn-IN",
        context: ClarificationContext | None = None,
    ) -> GroceryIntent:
        if language not in {"kn-IN", "hi-IN", "en-IN"}:
            raise IntentExtractionError("Language must be kn-IN, hi-IN, or en-IN")
        self.selected_provider = None
        self.selected_model = None
        if not text.strip():
            raise IntentExtractionError("The user utterance is empty")
        text = _with_context(text, context)
        candidates = [name for name in self._candidates if name not in self._incompatible_models][
            : self.max_fallback_models + 1
        ]
        if not candidates:
            raise IntentExtractionError(
                "No compatible Gemini models are available",
                user_message="No compatible Gemini text model is currently available.",
            )

        last_transient: Exception | None = None
        for model in candidates:
            started = time.perf_counter()
            try:
                result = self._extract_from_model(text, model, language)
            except genai_errors.APIError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                category = _api_error_category(exc)
                _log_model_failure(model, category, elapsed_ms, exc)
                if _is_structured_output_unsupported(exc):
                    self._incompatible_models.add(model)
                    continue
                if _is_gemini_quota_exhausted(exc):
                    return self._fallback_from_gemini(text, language, exc, "Gemini quota exhausted")
                if category == "transient":
                    last_transient = exc
                    continue
                raise _as_intent_error(exc, "Gemini API request failed") from exc
            except (httpx.TimeoutException, TimeoutError) as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                _log_model_failure(model, "timeout", elapsed_ms, exc)
                last_transient = exc
                continue
            except (httpx.ConnectError, ConnectionError, httpx.RequestError) as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                _log_model_failure(model, "connection", elapsed_ms, exc)
                last_transient = exc
                continue
            except IntentExtractionError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                _log_model_failure(model, "validation", elapsed_ms, exc)
                raise
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                _log_model_failure(model, "validation", elapsed_ms, exc)
                raise _as_intent_error(exc, "Gemini returned malformed structured data") from exc
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                _log_model_failure(model, "unexpected", elapsed_ms, exc)
                raise _as_intent_error(exc, "Gemini request failed") from exc
            else:
                elapsed_ms = (time.perf_counter() - started) * 1000
                self.selected_model = model
                self.selected_provider = "gemini"
                logger.info("Gemini model selected model=%s latency_ms=%.1f", model, elapsed_ms)
                return result

        if last_transient is not None:
            if isinstance(last_transient, genai_errors.APIError) and (
                last_transient.code in _OPENROUTER_FALLBACK_STATUS_CODES
                or _is_gemini_quota_exhausted(last_transient)
            ):
                return self._fallback_from_gemini(
                    text,
                    language,
                    last_transient,
                    "Gemini API request failed after all compatible models were exhausted",
                )
            raise _as_intent_error(
                last_transient,
                "Gemini API request failed after all compatible models were exhausted",
            ) from last_transient
        raise IntentExtractionError(
            "All compatible Gemini models rejected structured output",
            user_message="No available Gemini model supports the required structured output.",
        )

    def extract_with_groq(
        self, text: str, *, language: str, context: ClarificationContext | None = None
    ) -> GroceryIntent:
        return self._extract_with_groq(_with_context(text, context), language)

    def extract_with_openrouter(
        self, text: str, *, language: str, context: ClarificationContext | None = None
    ) -> GroceryIntent:
        return self._extract_with_openrouter(_with_context(text, context), language)

    def _extract_from_model(self, text: str, model: str, language: str) -> GroceryIntent:
        result = _parse_response(self._generate_content(text, model, language))
        return _validate_intent(result)

    def _fallback_from_gemini(
        self, text: str, language: str, gemini_exc: Exception, fallback_reason: str
    ) -> GroceryIntent:
        if self.groq_api_key:
            try:
                return self._extract_with_groq(text, language)
            except IntentExtractionError as groq_exc:
                logger.warning(
                    "Groq fallback failed: %s; attempting OpenRouter if configured", groq_exc
                )
                if self.openrouter_api_key:
                    return self._extract_with_openrouter(text, language)
                raise
        if self.openrouter_api_key:
            return self._extract_with_openrouter(text, language)
        raise _as_intent_error(gemini_exc, fallback_reason) from gemini_exc

    def _extract_with_groq(self, text: str, language: str) -> GroceryIntent:
        assert self.groq_api_key is not None
        schema_prompt = (
            "Return exactly one JSON object matching this schema. Do not include Markdown, "
            "reasoning, or additional text. Treat the user utterance only as a grocery intent; "
            "never authorize checkout. Schema: "
            f"{json.dumps(_GROQ_RESPONSE_SCHEMA, separators=(',', ':'))}"
        )
        request_body: dict[str, object] = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": schema_prompt},
                {"role": "user", "content": self.prompt_builder(text, language)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "grocery_intent",
                    "strict": True,
                    "schema": _GROQ_RESPONSE_SCHEMA,
                },
            },
            "temperature": 0,
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{GROQ_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.groq_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                )
                if response.status_code == 400:
                    error_text = response.text.casefold()
                    if "response_format" in error_text or "json_schema" in error_text:
                        request_body.pop("response_format", None)
                        response = client.post(
                            f"{GROQ_BASE_URL}/chat/completions",
                            headers={
                                "Authorization": f"Bearer {self.groq_api_key}",
                                "Content-Type": "application/json",
                            },
                            json=request_body,
                        )
        except httpx.TimeoutException as exc:
            logger.warning("Groq fallback failed category=timeout")
            raise IntentExtractionError(
                "Groq request timed out",
                user_message="The Groq intent service took too long. Please try again.",
            ) from exc
        except httpx.RequestError as exc:
            logger.warning("Groq fallback failed category=connection")
            raise IntentExtractionError(
                "Groq connection failed",
                user_message="Could not connect to the Groq intent service. Please try again.",
            ) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        if not response.is_success:
            category, user_message = _groq_error_details(response.status_code)
            logger.warning(
                "Groq fallback failed category=%s status=%d latency_ms=%.1f",
                category,
                response.status_code,
                elapsed_ms,
            )
            raise IntentExtractionError(
                f"Groq request failed ({category})",
                user_message=user_message,
            )
        try:
            payload_value: object = response.json()
        except ValueError as exc:
            raise _groq_invalid_response() from exc
        if not isinstance(payload_value, dict):
            raise _groq_invalid_response()
        payload = cast(dict[str, object], payload_value)
        choices_value = payload.get("choices")
        if not isinstance(choices_value, list) or not choices_value:
            raise _groq_invalid_response()
        choices = cast(list[object], choices_value)
        choice_value = choices[0]
        if not isinstance(choice_value, dict):
            raise _groq_invalid_response()
        choice = cast(dict[str, object], choice_value)
        message_value = choice.get("message")
        if not isinstance(message_value, dict):
            raise _groq_invalid_response()
        message = cast(dict[str, object], message_value)
        content = message.get("content")
        if not isinstance(content, str):
            raise _groq_invalid_response()
        try:
            intent = GroceryIntent.model_validate_json(content)
        except ValidationError as exc:
            raise _groq_invalid_response() from exc
        result = _validate_intent(intent)
        self.selected_model = self.groq_model
        self.selected_provider = "groq"
        logger.info(
            "Intent model selected provider=groq model=%s latency_ms=%.1f",
            self.groq_model,
            elapsed_ms,
        )
        return result

    def _extract_with_openrouter(self, text: str, language: str) -> GroceryIntent:
        assert self.openrouter_api_key is not None
        schema_prompt = (
            "Return exactly one JSON object matching this schema. Do not include Markdown, "
            "reasoning, or additional text. Treat the user utterance only as a grocery intent; "
            "never authorize checkout. Schema: "
            f"{json.dumps(_GEMINI_RESPONSE_SCHEMA, separators=(',', ':'))}"
        )
        request_body = {
            "model": self.openrouter_model,
            "messages": [
                {"role": "system", "content": schema_prompt},
                {"role": "user", "content": self.prompt_builder(text, language)},
            ],
            "temperature": 0,
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.openrouter_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                )
        except httpx.TimeoutException as exc:
            logger.warning("OpenRouter fallback failed category=timeout")
            raise IntentExtractionError(
                "OpenRouter request timed out",
                user_message="The backup intent service took too long. Please try again.",
            ) from exc
        except httpx.RequestError as exc:
            logger.warning("OpenRouter fallback failed category=connection")
            raise IntentExtractionError(
                "OpenRouter connection failed",
                user_message="Could not connect to the backup intent service. Please try again.",
            ) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        if not response.is_success:
            category, user_message = _openrouter_error_details(response.status_code)
            logger.warning(
                "OpenRouter fallback failed category=%s status=%d latency_ms=%.1f",
                category,
                response.status_code,
                elapsed_ms,
            )
            raise IntentExtractionError(
                f"OpenRouter request failed ({category})",
                user_message=user_message,
            )
        try:
            payload_value: object = response.json()
        except ValueError as exc:
            raise _openrouter_invalid_response() from exc
        if not isinstance(payload_value, dict):
            raise _openrouter_invalid_response()
        payload = cast(dict[str, object], payload_value)
        choices_value = payload.get("choices")
        if not isinstance(choices_value, list) or not choices_value:
            raise _openrouter_invalid_response()
        choices = cast(list[object], choices_value)
        choice_value = choices[0]
        if not isinstance(choice_value, dict):
            raise _openrouter_invalid_response()
        choice = cast(dict[str, object], choice_value)
        message_value = choice.get("message")
        if not isinstance(message_value, dict):
            raise _openrouter_invalid_response()
        message = cast(dict[str, object], message_value)
        content = message.get("content")
        if not isinstance(content, str):
            raise _openrouter_invalid_response()
        try:
            intent = GroceryIntent.model_validate_json(content)
        except ValidationError as exc:
            raise _openrouter_invalid_response() from exc
        result = _validate_intent(intent)
        self.selected_model = self.openrouter_model
        self.selected_provider = "openrouter"
        logger.info(
            "Intent model selected provider=openrouter model=%s latency_ms=%.1f",
            self.openrouter_model,
            elapsed_ms,
        )
        return result

    def _generate_content(self, text: str, model: str, language: str) -> _GeminiResponse:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_GEMINI_RESPONSE_SCHEMA,
        )
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.client.models.generate_content(
                    model=model,
                    contents=self.prompt_builder(text, language),
                    config=config,
                )
            except genai_errors.APIError as exc:
                # Quota exhaustion is not transient for another model in this
                # request; surface it so the provider fallback runs immediately.
                if (
                    _is_gemini_quota_exhausted(exc)
                    or exc.code not in _RETRYABLE_STATUS_CODES
                    or attempt == self.max_attempts
                ):
                    raise
                logger.info("Retrying transient Gemini API failure (attempt %d)", attempt + 1)
            except (httpx.RequestError, TimeoutError, ConnectionError):
                if attempt == self.max_attempts:
                    raise
                logger.info("Retrying transient Gemini transport failure (attempt %d)", attempt + 1)
            _sleep_before_retry(attempt)
        raise RuntimeError("Gemini retry loop exhausted unexpectedly")


def _timeout_seconds_from_env() -> int:
    raw_timeout = os.environ.get("GEMINI_TIMEOUT_SECONDS", str(GEMINI_TIMEOUT_SECONDS))
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError as exc:
        raise IntentExtractionError(
            "GEMINI_TIMEOUT_SECONDS must be a positive integer",
            user_message="Gemini timeout configuration is invalid.",
        ) from exc
    if timeout_seconds <= 0:
        raise IntentExtractionError(
            "GEMINI_TIMEOUT_SECONDS must be a positive integer",
            user_message="Gemini timeout configuration is invalid.",
        )
    return timeout_seconds


def _validate_intent(result: GroceryIntent) -> GroceryIntent:
    if result.intent not in _ALLOWED_INTENTS:
        raise IntentExtractionError("Intent extraction returned an unsupported intent")
    if result.needs_clarification and not result.clarification_question:
        raise IntentExtractionError("Clarification question is missing")
    if result.intent == "add_items" and not result.items and not result.needs_clarification:
        raise IntentExtractionError("An add_items intent must contain items")
    return result


def _groq_error_details(status: int) -> tuple[str, str]:
    if status in {401, 403}:
        return "authentication", "Groq authentication failed. Check GROQ_API_KEY."
    if status == 429:
        return "rate_limit", "Groq is rate-limited. Please try again shortly."
    if status == 404:
        return "invalid_model", "The configured Groq model is unavailable."
    if status in {500, 502, 503, 504}:
        return "provider_unavailable", "The Groq intent service is temporarily unavailable."
    return "api_error", f"The Groq intent service returned HTTP {status}."


def _groq_invalid_response() -> IntentExtractionError:
    return IntentExtractionError(
        "Groq returned malformed structured data",
        user_message="The Groq intent service returned an invalid grocery request. Please retry.",
    )


def _openrouter_error_details(status: int) -> tuple[str, str]:
    if status in {401, 403}:
        return "authentication", "OpenRouter authentication failed. Check OPENROUTER_API_KEY."
    if status == 429:
        return "rate_limit", "OpenRouter is rate-limited. Please try again shortly."
    if status in {502, 503, 504}:
        return "provider_unavailable", "The backup intent service is temporarily unavailable."
    return "api_error", f"The backup intent service returned HTTP {status}."


def _openrouter_invalid_response() -> IntentExtractionError:
    return IntentExtractionError(
        "OpenRouter returned malformed structured data",
        user_message="The backup intent service returned an invalid grocery request. Please retry.",
    )


def _sleep_before_retry(attempt: int) -> None:
    delay = min(
        _RETRY_INITIAL_DELAY_SECONDS * (2 ** (attempt - 1)),
        _RETRY_MAX_DELAY_SECONDS,
    )
    jitter = random.uniform(0.0, delay * _RETRY_JITTER_RATIO)
    time.sleep(delay + jitter)


def _max_fallback_models_from_env() -> int:
    raw = os.environ.get("GEMINI_MAX_FALLBACK_MODELS", str(GEMINI_MAX_FALLBACK_MODELS))
    try:
        value = int(raw)
    except ValueError as exc:
        raise IntentExtractionError(
            "GEMINI_MAX_FALLBACK_MODELS must be non-negative",
            user_message="Gemini fallback configuration is invalid.",
        ) from exc
    if value < 0:
        raise IntentExtractionError(
            "GEMINI_MAX_FALLBACK_MODELS must be non-negative",
            user_message="Gemini fallback configuration is invalid.",
        )
    return value


def _compatible_models(models: Iterable[Any], preferred: str) -> list[str]:
    discovered: dict[str, str] = {}
    preferred_name = preferred.removeprefix("models/")
    for model in models:
        name = getattr(model, "name", None)
        actions_raw: object = getattr(model, "supported_actions", None) or []
        actions = cast(Iterable[object], actions_raw)
        if not isinstance(name, str) or not name:
            continue
        if not any(str(action).casefold() == "generatecontent" for action in actions):
            continue
        display_name = str(getattr(model, "display_name", "") or "")
        searchable = f"{name} {display_name}".casefold()
        if any(term in searchable for term in _UNSUITABLE_MODEL_TERMS):
            continue
        normalized_name = name.removeprefix("models/")
        if normalized_name != preferred_name and normalized_name not in _FLASH_FALLBACK_RANK:
            continue
        discovered.setdefault(normalized_name, name)

    ordered_names = sorted(
        discovered,
        key=lambda normalized: (
            0 if normalized == preferred_name else 1,
            _FLASH_FALLBACK_RANK.get(normalized, -1),
        ),
    )
    return [discovered[normalized] for normalized in ordered_names]


def _api_error_category(exc: genai_errors.APIError) -> str:
    if exc.code in _RETRYABLE_STATUS_CODES:
        return "transient"
    if exc.code in {401, 403}:
        return "authentication"
    if exc.code == 404:
        return "invalid_model"
    if exc.code == 400:
        return "invalid_request"
    return "api_error"


def _is_gemini_quota_exhausted(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.APIError) and exc.code == 429:
        text = f"{getattr(exc, 'message', '')} {exc!s}".casefold()
        return "resource_exhausted" in text or "quota" in text or "exhausted" in text
    return False


def _is_structured_output_unsupported(exc: genai_errors.APIError) -> bool:
    if exc.code != 400:
        return False
    message = str(exc).casefold()
    return any(
        term in message
        for term in (
            "structured output",
            "response schema",
            "response_schema",
            "response mime",
            "response_mime_type",
            "json schema",
        )
    ) and any(term in message for term in ("unsupported", "not support", "invalid"))


def _as_intent_error(exc: Exception, fallback: str) -> IntentExtractionError:
    if isinstance(exc, IntentExtractionError):
        return exc
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return IntentExtractionError(
            "Gemini request timed out",
            user_message="Gemini took too long to respond. Please try again.",
        )
    if isinstance(exc, (httpx.ConnectError, ConnectionError)):
        return IntentExtractionError(
            "Gemini connection failed",
            user_message="Could not connect to Gemini. Check your network and try again.",
        )
    if isinstance(exc, httpx.RequestError):
        return IntentExtractionError(
            "Gemini network request failed",
            user_message="The Gemini network request failed. Please try again.",
        )
    if isinstance(exc, genai_errors.APIError):
        if exc.code in {401, 403}:
            return IntentExtractionError(
                "Gemini authentication failed",
                user_message="Gemini authentication failed. Check GEMINI_API_KEY.",
            )
        if exc.code == 404:
            return IntentExtractionError(
                "Gemini model is unavailable",
                user_message=(
                    "The configured Gemini model is unavailable. "
                    "Set GEMINI_MODEL to a supported model and try again."
                ),
            )
        if exc.code == 429:
            return IntentExtractionError(
                "Gemini rate limit reached",
                user_message="Gemini is temporarily rate-limited. Please try again shortly.",
            )
        if exc.code in _RETRYABLE_STATUS_CODES:
            return IntentExtractionError(
                fallback,
                user_message="Gemini models are temporarily unavailable. Please try again.",
            )
    if isinstance(exc, (ValidationError, ValueError, TypeError, json.JSONDecodeError)):
        return IntentExtractionError(
            "Gemini returned malformed structured data",
            user_message="Gemini returned an invalid grocery request. Please rephrase it.",
        )
    return IntentExtractionError(
        fallback,
        user_message="Gemini could not process that request. Please try again.",
    )


def _log_model_failure(model: str, category: str, elapsed_ms: float, exc: Exception) -> None:
    _log_failure(exc, f"model={model} category={category} latency_ms={elapsed_ms:.1f}")


def _log_failure(exc: Exception, stage: str) -> None:
    message = re.sub(
        r"(?i)(api[-_ ]?key|authorization|bearer|token)\s*[:=]?\s*\S+",
        "[redacted]",
        str(exc),
    )
    logger.warning("Gemini %s failed: %s: %s", stage, type(exc).__name__, message[:300])


def _parse_response(response: _GeminiResponse) -> GroceryIntent:
    if isinstance(response.parsed, GroceryIntent):
        return response.parsed
    if response.text is None:
        raise IntentExtractionError("Gemini returned no structured response")
    return GroceryIntent.model_validate_json(response.text)


def _with_context(text: str, context: ClarificationContext | None) -> str:
    if context is None:
        return text
    notes: list[str] = []
    if context.today:
        notes.append(f"Today is {context.today}; resolve spoken dates against it.")
    if context.active_domain:
        notes.append(f"The current task is {context.active_domain}.")
    extra = f"\n\n{' '.join(notes)}" if notes else ""
    if not context.options:
        return text + extra
    numbered = "; ".join(
        f"{number}. {option}" for number, option in enumerate(context.options, start=1)
    )
    return (
        f"{text}\n\nThe user is answering this question about {context.item!r}. Options: "
        f"{numbered}. If they pick one, by position, brand, or size, use intent select_option "
        "with its option_number. Never pick an option they did not clearly describe."
        f"{extra}"
    )


def _prompt(text: str, language: str) -> str:
    response_language = {"kn-IN": "Kannada", "hi-IN": "Hindi", "en-IN": "English"}[language]
    return (
        "Extract only the user's grocery-dialog intent as JSON matching the provided schema. "
        "Do not invent quantities, units, products, prices, variants, addresses, totals, or "
        "order authorization. For an ambiguous request, set needs_clarification=true and ask "
        f"one concise clarification_question in {response_language}. The JSON schema and intent "
        "values remain identical for every language. Do not write a conversational reply. "
        "For each item, name is what the user said and search_term is the common English "
        "grocery word for it, translated or transliterated if needed (for example ಹಾಲು, haalu, "
        "or दूध become milk; akki or चावल become rice). Keep a brand in search_term only if the "
        "user said one. Put a spoken pack count in unit as packet (for example three packets). "
        "Supported intents are add_items, remove_items, change_quantity, reorder_usual, done, "
        "confirm, reject, cancel, select_option, book_table, and unclear. "
        "Set domain to grocery for groceries and household shopping, food for ready-to-eat meals "
        "delivered from a restaurant, or dining for reserving a table to eat out, and null if it "
        "cannot be told. For a meal with no dish named, such as wanting something for dinner, use "
        "domain food with needs_clarification true. Dishes use add_items. "
        "For a table use intent book_table with query (one cuisine or restaurant name), area "
        "(city or neighbourhood, always in English letters, so इंदिरानगर and ಇಂದಿರಾನಗರ become "
        "Indiranagar), date as YYYY-MM-DD, time as 24-hour HH:MM, and guests, leaving anything "
        "the user did not say as null. A table time with no morning or evening named means the "
        "evening or lunchtime, whichever fits, so seven or 07:00 becomes 19:00 and one becomes "
        "13:00. Users may mix Kannada, Hindi, and English. "
        "For remove_items include the item with quantity 1 when no quantity is given.\n\n"
        f"User text: {text}"
    )
