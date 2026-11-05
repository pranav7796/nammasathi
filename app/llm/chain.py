"""Try intent providers in order, each with a short deadline.

Measured on 5 November 2026: Groq answered in about 1.2 s and Gemini in 5 to 13 s, and one
Gemini overload cost 89 s while it walked three models. So each provider gets one bounded try
and the next takes over. None of these can reach commerce code; they only return a validated
GroceryIntent.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from app.llm.extractor import (
    ClarificationContext,
    GeminiIntentExtractor,
    GroceryIntent,
    IntentExtractionError,
)
from app.llm.scripted import ScriptedExtractor
from app.observability import log_event
from app.timing import note_understood_by, stage

logger = logging.getLogger(__name__)
SCRIPTED = "scripted"

Extract = Callable[[str, str, ClarificationContext | None], GroceryIntent]


@dataclass(frozen=True)
class ProviderStep:
    name: str
    extract: Extract


COOLDOWN_SECONDS = 30.0


class ChainedExtractor:
    """A provider that just failed is skipped for a while, so one that is rate-limited or
    overloaded does not cost every turn its full timeout. The last provider is always tried."""

    def __init__(self, steps: list[ProviderStep]) -> None:
        if not steps:
            raise IntentExtractionError(
                "No intent provider is configured",
                user_message=(
                    "No language service is configured. Set GROQ_API_KEY or GEMINI_API_KEY, "
                    "or use DEMO_UNDERSTANDING=scripted in mock mode."
                ),
            )
        self.steps = steps
        self._resting_until: dict[str, float] = {}

    def extract(
        self,
        text: str,
        *,
        language: str = "kn-IN",
        context: ClarificationContext | None = None,
    ) -> GroceryIntent:
        last_error: IntentExtractionError | None = None
        with stage("understanding"):
            for index, step in enumerate(self.steps):
                last = index == len(self.steps) - 1
                if not last and time.monotonic() < self._resting_until.get(step.name, 0.0):
                    log_event("understanding", op=step.name, outcome="skipped_resting")
                    continue
                started = time.monotonic()
                try:
                    result = step.extract(text, language, context)
                except IntentExtractionError as exc:
                    last_error = exc
                except Exception as exc:  # A broken provider must not end the turn.
                    last_error = IntentExtractionError(f"{step.name} failed: {type(exc).__name__}")
                else:
                    self._resting_until.pop(step.name, None)
                    note_understood_by(step.name)
                    return result
                self._resting_until[step.name] = time.monotonic() + COOLDOWN_SECONDS
                log_event(
                    "understanding",
                    op=step.name,
                    outcome="failed",
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                logger.warning("Intent provider %s failed; trying the next", step.name)
        assert last_error is not None
        raise last_error


def _groq_step(settings: Settings) -> ProviderStep:
    extractor = GeminiIntentExtractor(
        client=None,
        candidates=(),
        groq_api_key=settings.groq_api_key,
        groq_model=settings.groq_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    return ProviderStep(
        "groq",
        lambda text, language, context: extractor.extract_with_groq(
            text, language=language, context=context
        ),
    )


def _openrouter_step(settings: Settings) -> ProviderStep:
    extractor = GeminiIntentExtractor(
        client=None,
        candidates=(),
        openrouter_api_key=settings.openrouter_api_key,
        openrouter_model=settings.openrouter_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    return ProviderStep(
        "openrouter",
        lambda text, language, context: extractor.extract_with_openrouter(
            text, language=language, context=context
        ),
    )


def _gemini_step(settings: Settings) -> ProviderStep:
    """Discovers models on first use, so a Gemini outage cannot stop a session from starting."""
    cached: list[GeminiIntentExtractor] = []

    def extract(text: str, language: str, context: ClarificationContext | None) -> GroceryIntent:
        if not cached:
            cached.append(
                GeminiIntentExtractor.from_env(
                    allow_logged_fallback=False,
                    provider_fallbacks=False,
                    timeout_seconds=settings.llm_timeout_seconds,
                    max_fallback_models=0,
                    max_attempts=1,
                )
            )
        return cached[0].extract(text, language=language, context=context)

    return ProviderStep("gemini", extract)


def build_extractor(settings: Settings) -> ChainedExtractor:
    """Scripted understanding exists only in mock mode; config refuses anything else."""
    scripted = ProviderStep(
        SCRIPTED,
        lambda text, language, context: ScriptedExtractor().extract(
            text, language=language, context=context
        ),
    )
    if settings.demo_understanding == SCRIPTED:
        return ChainedExtractor([scripted])

    groq = _groq_step(settings) if settings.groq_api_key else None
    gemini = _gemini_step(settings) if os.environ.get("GEMINI_API_KEY") else None
    ordered = [groq, gemini] if settings.llm_primary == "groq" else [gemini, groq]
    steps = [step for step in ordered if step is not None]
    # OpenRouter's free model may log requests, so it never sees a real account or the public.
    if (
        settings.openrouter_api_key
        and settings.commerce_backend == "mock"
        and not settings.public_deployment
    ):
        steps.append(_openrouter_step(settings))
    if settings.demo_understanding == "fallback":
        steps.append(scripted)
    # Names only, never keys: tells an operator from the host's log what this server will use.
    log_event(
        "providers",
        op=",".join(step.name for step in steps) or "none",
        kind="gnani" if settings.gnani_api_key else "no_gnani",
    )
    return ChainedExtractor(steps)
