from __future__ import annotations

import base64
import io
import wave
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from app.commerce.mock import MockInstamart
from app.config import Settings, load_settings
from app.llm.extractor import GeminiIntentExtractor, GroceryIntent, GroceryItem
from app.orchestrator.state_machine import State
from app.voice.gnani import GnaniError, PrismaSTT, PrismaTranscript, TimbreTTS
from app.web import MAX_AUDIO_BYTES, create_app
from tests.test_gemini_extractor import FakeResponse


class FakeGeminiModels:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)

    def generate_content(self, **kwargs: object) -> FakeResponse:
        del kwargs
        return next(self.responses)


class FakeGeminiClient:
    def __init__(self, intents: list[GroceryIntent]) -> None:
        self.models = FakeGeminiModels([FakeResponse(parsed=intent) for intent in intents])


class FakeVoiceServices:
    def __init__(self, transcripts: list[str | GnaniError]) -> None:
        self.transcripts = iter(transcripts)
        self.stt_languages: list[str] = []
        self.tts_texts: list[str] = []
        self.tts_languages: list[str] = []
        self.tts_error: GnaniError | None = None

    async def transcribe(self, path: str | Path) -> PrismaTranscript:
        audio = Path(path).read_bytes()
        if not audio.startswith(b"RIFF"):
            raise GnaniError("invalid_audio", "WAV audio file is invalid")
        result = next(self.transcripts)
        if isinstance(result, GnaniError):
            raise result
        return PrismaTranscript(result, self.stt_languages[-1])

    async def synthesize(self, text: str, output_path: str | Path) -> Path:
        if self.tts_error is not None:
            raise self.tts_error
        self.tts_texts.append(text)
        destination = Path(output_path)
        destination.write_bytes(_wav_bytes())
        return destination


def _wav_bytes(seconds: int = 1) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000 * seconds)
    return stream.getvalue()


def _intent(name: str, quantity: float, unit: str = "kg") -> GroceryIntent:
    return GroceryIntent(
        intent="add_items",
        items=[GroceryItem(name=name, quantity=quantity, unit=unit)],
    )


def _settings() -> Settings:
    return load_settings(
        {
            "COMMERCE_BACKEND": "mock",
            "ALLOW_REAL_CHECKOUT": "false",
            "GNANI_API_KEY": "test-gnani-key",
            "GEMINI_API_KEY": "test-gemini-key",
        }
    )


def _extractor_factory(intents: list[GroceryIntent]):
    def make_extractor() -> GeminiIntentExtractor:
        return GeminiIntentExtractor(
            FakeGeminiClient(intents),
            candidates=("gemini-test",),
        )

    return make_extractor


def _speech_factories(services: FakeVoiceServices):
    def stt_factory(settings: Settings, language: str) -> PrismaSTT:
        del settings
        services.stt_languages.append(language)
        return cast(PrismaSTT, services)

    def tts_factory(settings: Settings, language: str) -> TimbreTTS:
        del settings
        services.tts_languages.append(language)
        return cast(TimbreTTS, services)

    return stt_factory, tts_factory


@asynccontextmanager
async def _client_for(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        yield client


def _session_headers(session_id: str) -> dict[str, str]:
    return {"X-Session-ID": session_id}


async def _create_session(client: httpx.AsyncClient, language: str = "kn-IN") -> dict[str, object]:
    response = await client.post("/api/sessions", json={"language": language})
    assert response.status_code == 200, response.text
    return response.json()


async def _upload(
    client: httpx.AsyncClient, session_id: str, language: str | None = None
) -> httpx.Response:
    return await client.post(
        "/api/session/turn" if language is None else f"/api/session/turn?language={language}",
        headers={**_session_headers(session_id), "Content-Type": "audio/wav"},
        content=_wav_bytes(),
    )


@pytest.mark.asyncio
async def test_browser_session_creation_and_isolation() -> None:
    services = FakeVoiceServices([])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        first = await _create_session(client)
        second = await _create_session(client, language="hi-IN")

        first_id = str(first["session_id"])
        second_id = str(second["session_id"])
        assert first_id != second_id
        assert first["assistant_text"] == "ನಮಸ್ಕಾರ. ನಿಮಗೆ ಯಾವ ಸಾಮಾನುಗಳು ಬೇಕು?"
        assert first["state"] == State.COLLECTING.value
        assert first["language"] == "kn-IN"
        assert first["address_label"] == "Home"
        assert first["payment_method"] == "COD"
        assert first["audio_base64"] == base64.b64encode(_wav_bytes()).decode("ascii")
        first_runner = app.state.sessions[first_id].runner
        second_runner = app.state.sessions[second_id].runner
        assert isinstance(first_runner.adapter, MockInstamart)
        assert isinstance(second_runner.adapter, MockInstamart)
        assert first_runner.adapter is not second_runner.adapter
        assert first_runner.session.cart == ()
        assert second_runner.session.cart == ()
        assert len(services.tts_texts) == 2
        assert services.tts_languages == ["kn-IN", "hi-IN"]
        assert second["assistant_text"] == "नमस्ते। आपको कौन-सा सामान चाहिए?"


@pytest.mark.asyncio
async def test_kannada_audio_turns_preserve_cart_and_confirm_latest_readback() -> None:
    services = FakeVoiceServices(["ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ", "ಒಂದು ಕೆಜಿ ಆಟ್ಟ"])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("rice", 2), _intent("atta", 1)]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client)
        session_id = str(created["session_id"])
        first = await _upload(client, session_id)
        assert first.status_code == 200, first.text
        first_data = first.json()
        assert first_data["transcript"] == "ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ"
        assert first_data["state"] == State.READBACK.value
        assert first_data["cart"][0]["pack_size"] == "1 kg"
        first_version = first_data["cart_version"]

        second = await _upload(client, session_id)
        assert second.status_code == 200, second.text
        second_data = second.json()
        assert second_data["transcript"] == "ಒಂದು ಕೆಜಿ ಆಟ್ಟ"
        assert second_data["state"] == State.READBACK.value
        assert len(second_data["cart"]) == 2
        assert second_data["cart_version"] > first_version
        assert second_data["can_confirm"] is True
        assert services.stt_languages == ["kn-IN", "kn-IN"]
        assert services.tts_languages == ["kn-IN", "kn-IN", "kn-IN"]

        runner = app.state.sessions[session_id].runner
        assert runner.adapter.checkout_calls == 0
        confirmation = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={
                "action": "confirm",
                "cart_version": second_data["cart_version"],
                "cart_hash": second_data["cart_hash"],
            },
        )
        assert confirmation.status_code == 200, confirmation.text
        assert confirmation.json()["state"] == State.DONE.value
        assert runner.adapter.checkout_calls == 1


@pytest.mark.parametrize("language", ["kn-IN", "hi-IN", "en-IN"])
@pytest.mark.asyncio
async def test_supported_languages_reach_prisma(language: str) -> None:
    services = FakeVoiceServices(["2 kilo rice"])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("rice", 2)]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client, language)
        response = await _upload(client, str(created["session_id"]))

    assert response.status_code == 200, response.text
    assert services.stt_languages == [language]
    assert services.tts_languages == [language, language]
    assert response.json()["language"] == language
    assert response.json()["tts_language"] == language


@pytest.mark.parametrize("language", ["kn-IN", "hi-IN", "en-IN"])
@pytest.mark.asyncio
async def test_language_persists_across_multiple_turns_and_actions(language: str) -> None:
    services = FakeVoiceServices(["item 1", "item 2"])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("rice", 2), _intent("atta", 1)]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    expected_snippets = {
        "kn-IN": "ನಿಮ್ಮ ಬುಟ್ಟಿಯಲ್ಲಿ",
        "hi-IN": "आपकी टोकरी में",
        "en-IN": "Your basket has",
    }

    async with _client_for(app) as client:
        created = await _create_session(client, language)
        session_id = str(created["session_id"])
        first = await _upload(client, session_id)
        second = await _upload(client, session_id)

    assert first.status_code == second.status_code == 200
    assert expected_snippets[language] in first.json()["assistant_text"]
    assert expected_snippets[language] in second.json()["assistant_text"]
    assert first.json()["language"] == language
    assert second.json()["language"] == language
    assert services.stt_languages == [language, language]
    assert services.tts_languages == [language, language, language]


@pytest.mark.asyncio
async def test_language_switching_between_conversations() -> None:
    services = FakeVoiceServices([])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        hindi_session = await _create_session(client, "hi-IN")
        english_session = await _create_session(client, "en-IN")
        kannada_session = await _create_session(client, "kn-IN")

        h_id = str(hindi_session["session_id"])
        e_id = str(english_session["session_id"])
        k_id = str(kannada_session["session_id"])

        h_get = await client.get("/api/session", headers=_session_headers(h_id))
        e_get = await client.get("/api/session", headers=_session_headers(e_id))
        k_get = await client.get("/api/session", headers=_session_headers(k_id))

    assert h_get.json()["language"] == "hi-IN"
    assert e_get.json()["language"] == "en-IN"
    assert k_get.json()["language"] == "kn-IN"
    assert services.tts_languages == ["hi-IN", "en-IN", "kn-IN"]


@pytest.mark.parametrize(
    ("language", "localized_error"),
    [
        ("kn-IN", "ಧ್ವನಿ ಉತ್ತರ ಸಿದ್ಧವಾಗಲಿಲ್ಲ"),
        ("hi-IN", "आवाज़ में जवाब तैयार नहीं हुआ"),
        ("en-IN", "Ajji could not make an audio reply"),
    ],
)
@pytest.mark.asyncio
async def test_tts_failure_does_not_fall_back_to_another_language(
    language: str, localized_error: str
) -> None:
    services = FakeVoiceServices([])
    services.tts_error = GnaniError("service", "Timbre is temporarily unavailable")
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client, language)

    assert created["language"] == language
    assert created["tts_language"] == language
    assert created["audio_base64"] is None
    assert localized_error in str(created["audio_error"])
    assert services.tts_languages == [language]


@pytest.mark.asyncio
async def test_audio_turn_cannot_override_the_session_language() -> None:
    services = FakeVoiceServices(["namaste"])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("rice", 1)]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client, "hi-IN")
        response = await _upload(client, str(created["session_id"]), "kn-IN")

    assert response.status_code == 409
    assert services.stt_languages == []
    assert services.tts_languages == ["hi-IN"]


@pytest.mark.asyncio
async def test_stale_readback_confirmation_is_rejected() -> None:
    services = FakeVoiceServices(["rice"])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("rice", 1)]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client)
        session_id = str(created["session_id"])
        turn = await _upload(client, session_id)
        assert turn.status_code == 200
        readback = turn.json()
        runner = app.state.sessions[session_id].runner
        stale = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={
                "action": "confirm",
                "cart_version": readback["cart_version"] - 1,
                "cart_hash": "stale",
            },
        )
        assert stale.status_code == 409
        stale_data = stale.json()
        assert stale_data["can_confirm"] is True
        assert "1 kg Sona Masoori Rice" in stale_data["assistant_text"]
        assert stale_data["audio_base64"] is not None
        assert runner.adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_cancellation_clears_cart_without_checkout() -> None:
    services = FakeVoiceServices(["atta"])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("atta", 1)]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client)
        session_id = str(created["session_id"])
        assert (await _upload(client, session_id)).status_code == 200
        runner = app.state.sessions[session_id].runner
        cancelled = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={"action": "cancel"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cart"] == []
        assert runner.adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_invalid_audio_and_stt_failure_leave_cart_unchanged() -> None:
    services = FakeVoiceServices([GnaniError("service", "Prisma is temporarily unavailable")])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("rice", 1)]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client)
        session_id = str(created["session_id"])
        invalid = await client.post(
            "/api/session/turn?language=kn-IN",
            headers={**_session_headers(session_id), "Content-Type": "audio/wav"},
            content=b"not a wav file",
        )
        assert invalid.status_code == 422

        failed = await _upload(client, session_id)
        assert failed.status_code == 502
        assert "temporarily unavailable" in failed.json()["detail"]
        runner = app.state.sessions[session_id].runner
        assert runner.session.cart == ()
        assert runner.adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_oversized_audio_and_expired_session_are_rejected() -> None:
    services = FakeVoiceServices([])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client)
        session_id = str(created["session_id"])
        oversized = await client.post(
            "/api/session/turn?language=kn-IN",
            headers={
                **_session_headers(session_id),
                "Content-Type": "audio/wav",
                "Content-Length": str(MAX_AUDIO_BYTES + 1),
            },
            content=b"too big",
        )
        assert oversized.status_code == 413

        app.state.sessions[session_id].last_activity = 0
        expired = await client.get("/api/session", headers=_session_headers(session_id))
        assert expired.status_code == 410


@pytest.mark.parametrize(("backend", "allow_checkout"), [("swiggy", False), ("mock", True)])
@pytest.mark.asyncio
async def test_browser_app_refuses_real_commerce_configuration(
    backend: str, allow_checkout: bool
) -> None:
    unsafe = Settings(commerce_backend=backend, allow_real_checkout=allow_checkout)
    app = create_app(settings=unsafe)
    with pytest.raises(RuntimeError, match="COMMERCE_BACKEND=mock"):
        async with app.router.lifespan_context(app):
            pytest.fail("unsafe configuration entered the application lifespan")
