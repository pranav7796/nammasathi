from __future__ import annotations

import base64
import io
import wave
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from app.commerce.base import Cart, CartLine, CommerceRejected, RejectionKind
from app.commerce.mock import MockInstamart
from app.commerce.order_journal import FileOrderJournal, new_attempt
from app.commerce.runtime import dining_adapter, food_adapter
from app.config import Settings, load_settings
from app.dining.mock import MockDineout
from app.food.mock import MockFood
from app.llm.extractor import GeminiIntentExtractor, GroceryIntent, GroceryItem
from app.orchestrator.checkout import CheckoutPolicy
from app.orchestrator.state_machine import State
from app.voice.gnani import GnaniError, PrismaSTT, PrismaTranscript, TimbreTTS
from app.web import (
    MAX_AUDIO_BYTES,
    _default_tts_factory,  # pyright: ignore[reportPrivateUsage]
    create_app,
)
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
        assert "ಸಾಥಿ" in str(first["assistant_text"])
        assert first["state"] == State.COLLECTING.value
        assert first["language"] == "kn-IN"
        assert first["address_label"] == "Home"
        assert first["payment_method"] == "COD"
        assert first["audio_base64"] == base64.b64encode(_wav_bytes()).decode("ascii")
        await app.state.sessions[first_id].assistant.workflow("grocery")
        await app.state.sessions[second_id].assistant.workflow("grocery")
        first_runner = app.state.sessions[first_id].runner
        second_runner = app.state.sessions[second_id].runner
        assert isinstance(first_runner.adapter, MockInstamart)
        assert isinstance(second_runner.adapter, MockInstamart)
        assert first_runner.adapter is not second_runner.adapter
        assert first_runner.session.cart == ()
        assert second_runner.session.cart == ()
        assert len(services.tts_texts) == 2
        assert services.tts_languages == ["kn-IN", "hi-IN"]
        assert "साथी" in str(second["assistant_text"])


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
        ("en-IN", "Saathi could not make an audio reply"),
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
        extractor_factory=_extractor_factory([_intent("rice", 2)]),
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
        # Neither failure reached a capability, so no cart was even opened.
        assert app.state.sessions[session_id].assistant.workflows == {}


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


class LiveLikeMock(MockInstamart):
    """A non-mock backend for the web layer; still entirely in memory."""


def _real_settings(tmp_path: Path, **overrides: str) -> Settings:
    values = {
        "COMMERCE_BACKEND": "swiggy",
        "SWIGGY_LIVE_ACCESS": "true",
        "ALLOW_SWIGGY_CART_WRITES": "true",
        "ALLOW_REAL_CHECKOUT": "true",
        "MAX_REAL_ORDER_INR": "5000",
        "ORDER_JOURNAL_PATH": str(tmp_path / "attempts.jsonl"),
        "GNANI_API_KEY": "test-gnani-key",
        "GEMINI_API_KEY": "test-gemini-key",
        **overrides,
    }
    return load_settings(values)


def _real_app(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    intents: list[GroceryIntent],
    services: FakeVoiceServices,
) -> tuple[FastAPI, LiveLikeMock]:
    shop = LiveLikeMock()

    def fake_adapter(_: Settings) -> LiveLikeMock:
        return shop

    def fast_policy(_: Settings) -> CheckoutPolicy:
        return CheckoutPolicy(max_orders=1, max_total_inr=Decimal(5000))

    monkeypatch.setattr("app.web.commerce_adapter", fake_adapter)
    monkeypatch.setattr("app.web.checkout_policy", fast_policy)
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=settings,
        extractor_factory=_extractor_factory(intents),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )
    return app, shop


@asynccontextmanager
async def _local_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
            base_url="http://localhost:8000",
        ) as client,
    ):
        yield client


async def test_real_mode_rejects_other_machines_and_foreign_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app, _ = _real_app(monkeypatch, _real_settings(tmp_path), [], FakeVoiceServices([]))
    async with app.router.lifespan_context(app):
        for client_host, base_url, origin in (
            ("10.0.0.5", "http://localhost:8000", None),
            ("127.0.0.1", "http://evil.example", None),
            ("127.0.0.1", "http://localhost:8000", "http://evil.example"),
        ):
            transport = httpx.ASGITransport(app=app, client=(client_host, 5000))
            headers = {"Origin": origin} if origin else {}
            async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
                response = await client.post(
                    "/api/sessions", json={"language": "en-IN"}, headers=headers
                )
            assert response.status_code == 403
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
            base_url="http://localhost:8000",
        ) as client:
            assert (await client.get("/api/addresses")).status_code == 200


async def test_real_spoken_yes_needs_the_readback_this_browser_received(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    services = FakeVoiceServices(["rice", "yes", "yes"])
    app, shop = _real_app(monkeypatch, _real_settings(tmp_path), [_intent("rice", 2)], services)

    async with _local_client(app) as client:
        created = await _create_session(client, "en-IN")
        session_id = str(created["session_id"])
        turn = (await _upload(client, session_id)).json()
        assert turn["can_confirm"] is True

        # No echo of what was heard: refused, and the cart is read out again.
        blind = await _upload(client, session_id)
        assert blind.status_code == 200
        assert shop.checkout_calls == 0
        assert "Your basket has" in blind.json()["assistant_text"]

        heard = await client.post(
            "/api/session/turn",
            headers={
                **_session_headers(session_id),
                "Content-Type": "audio/wav",
                "X-Heard-Cart-Version": str(turn["cart_version"]),
                "X-Heard-Cart-Hash": str(turn["cart_hash"]),
            },
            content=_wav_bytes(),
        )

    data = heard.json()
    assert shop.checkout_calls == 1
    assert data["state"] == State.DONE.value
    assert data["order_ids"] == ["IM000001"]
    assert data["order_verified"] is True
    assert "demo" not in data["assistant_text"].casefold()
    assert "mock" not in data["assistant_text"].casefold()


async def test_read_only_mode_cannot_confirm_or_change_the_cart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _real_settings(
        tmp_path,
        ALLOW_REAL_CHECKOUT="false",
        ALLOW_SWIGGY_CART_WRITES="false",
        MAX_REAL_ORDER_INR="",
    )
    services = FakeVoiceServices(["rice"])
    app, shop = _real_app(monkeypatch, settings, [_intent("rice", 2)], services)

    async with _local_client(app) as client:
        created = await _create_session(client, "en-IN")
        assert created["checkout_enabled"] is False
        assert created["can_confirm"] is False
        assert shop.checkout_calls == 0


async def test_unresolved_attempt_blocks_the_browser_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _real_settings(tmp_path)
    FileOrderJournal(Path(settings.order_journal_path)).begin(
        new_attempt("h", "100", (), ("Rice",)), lambda _: None
    )
    services = FakeVoiceServices(["rice"])
    app, shop = _real_app(monkeypatch, settings, [_intent("rice", 2)], services)

    async with _local_client(app) as client:
        created = await _create_session(client, "en-IN")

    assert created["state"] == State.ORDER_UNKNOWN.value
    assert created["can_confirm"] is False
    assert "do not order again" in str(created["assistant_text"])
    assert shop.checkout_calls == 0


async def test_existing_cart_for_another_address_is_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    services = FakeVoiceServices(["rice"])
    app, shop = _real_app(monkeypatch, _real_settings(tmp_path), [_intent("rice", 2)], services)
    await shop.update_cart(
        "addr_home", [CartLine(spin_id="atta_5kg", sku_id="sku_atta_5kg", quantity=1)]
    )
    shop._cart_address = "addr_elsewhere"  # pyright: ignore[reportPrivateUsage]

    async with _local_client(app) as client:
        created = await _create_session(client, "en-IN")
        turn = (await _upload(client, str(created["session_id"]))).json()

    assert "won't change it" in turn["assistant_text"]
    assert len(shop._cart) == 1  # pyright: ignore[reportPrivateUsage]
    assert shop._cart[0].sku_id == "sku_atta_5kg"  # pyright: ignore[reportPrivateUsage]


async def test_refused_basket_read_is_explained_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.orchestrator.turn_runner.START_CART_RETRY_SECONDS", 0)
    services = FakeVoiceServices(["rice"])
    app, shop = _real_app(monkeypatch, _real_settings(tmp_path), [_intent("rice", 2)], services)

    async def refused() -> Cart:
        raise CommerceRejected("refused", kind=RejectionKind.NOT_SERVICEABLE)

    monkeypatch.setattr(shop, "get_cart", refused)

    async with _local_client(app) as client:
        created = await _create_session(client, "en-IN")
        turn = (await _upload(client, str(created["session_id"]))).json()

    assert "could not reach the shop" in turn["assistant_text"]
    assert shop.checkout_calls == 0


async def test_choosing_an_option_by_tap_uses_the_same_rules() -> None:
    services = FakeVoiceServices(["toor dal"])
    stt_factory, tts_factory = _speech_factories(services)
    intent = GroceryIntent(
        intent="add_items", items=[GroceryItem(name="rice", quantity=1, unit=None)]
    )
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([intent]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client, "en-IN")
        session_id = str(created["session_id"])
        asked = (await _upload(client, session_id)).json()
        assert asked["state"] == State.CLARIFYING.value
        assert [option["pack_size"] for option in asked["options"]] == ["1 kg", "5 kg", "10 kg"]
        assert "5 kg" in asked["assistant_text"]

        bad = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={"action": "select_option", "option_index": 7},
        )
        assert bad.json()["state"] == State.CLARIFYING.value

        chosen = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={"action": "select_option", "option_index": 1},
        )

    data = chosen.json()
    assert data["state"] == State.READBACK.value
    assert data["cart"][0]["pack_size"] == "5 kg"
    assert data["options"] == []


async def test_reply_is_recoverable_after_a_lost_response() -> None:
    services = FakeVoiceServices(["rice"])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("rice", 2)]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client, "en-IN")
        session_id = str(created["session_id"])
        turn = (await _upload(client, session_id)).json()
        await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={
                "action": "confirm",
                "cart_version": turn["cart_version"],
                "cart_hash": turn["cart_hash"],
            },
        )
        # The browser lost that response and reloads.
        restored = (await client.get("/api/session", headers=_session_headers(session_id))).json()

    assert restored["state"] == State.DONE.value
    assert restored["order_ids"] == ["IM000001"]
    assert "demo" in restored["last_reply"].casefold()


async def test_broken_speech_cannot_hide_a_placed_order() -> None:
    class ExplodingTts:
        async def synthesize(self, text: str, output_path: str | Path) -> Path:
            raise RuntimeError("voice engine bug")

    services = FakeVoiceServices(["rice"])
    stt_factory, _ = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_intent("rice", 2)]),
        stt_factory=stt_factory,
        tts_factory=lambda settings, language: cast(TimbreTTS, ExplodingTts()),
    )

    async with _client_for(app) as client:
        created = await _create_session(client, "en-IN")
        session_id = str(created["session_id"])
        turn = (await _upload(client, session_id)).json()
        placed = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={
                "action": "confirm",
                "cart_version": turn["cart_version"],
                "cart_hash": turn["cart_hash"],
            },
        )

    assert placed.status_code == 200
    assert placed.json()["state"] == State.DONE.value
    assert placed.json()["audio_error"]


async def test_paise_reach_the_browser_as_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal as D

    from app.web import _money_json  # pyright: ignore[reportPrivateUsage]

    del monkeypatch
    assert _money_json(D("86.50")) == 86.5
    assert _money_json(D(87)) == 87
    assert isinstance(_money_json(D(87)), int)


def test_voices_match_each_language_preference() -> None:
    assert _default_voice_for("kn-IN") == "Saanvi"
    assert _default_voice_for("hi-IN") == "Nalini"
    assert _default_voice_for("en-IN") == "Kaveri"


def _default_voice_for(language: str) -> str:
    settings = load_settings({"GNANI_API_KEY": "k"})
    return _default_tts_factory(settings, language).voice


def _dining_intent() -> GroceryIntent:
    return GroceryIntent(
        intent="book_table",
        domain="dining",
        items=[],
        query="Italian",
        area="Bengaluru",
        date="2030-01-02",
        time="19:00",
        guests=2,
    )


async def test_one_conversation_can_book_a_table_through_the_api() -> None:
    services = FakeVoiceServices(["a table for two"])
    stt_factory, tts_factory = _speech_factories(services)
    app = create_app(
        settings=_settings(),
        extractor_factory=_extractor_factory([_dining_intent()]),
        stt_factory=stt_factory,
        tts_factory=tts_factory,
    )

    async with _client_for(app) as client:
        created = await _create_session(client, "en-IN")
        session_id = str(created["session_id"])
        assert created["capability"] == ""
        asked = (await _upload(client, session_id)).json()
        assert asked["capability"] == "dining"
        assert asked["state"] == "CHOOSING_RESTAURANT"
        assert asked["options"][0]["name"] == "Toscano"

        readback = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={"action": "select_option", "option_index": 0},
        )
        data = readback.json()
        assert data["state"] == "READBACK"
        assert data["can_confirm"] is True
        assert data["payment_method"] == "NONE"
        assert "free reservation" in data["assistant_text"]

        booked = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={
                "action": "confirm",
                "cart_version": data["cart_version"],
                "cart_hash": data["cart_hash"],
            },
        )

    assert booked.status_code == 200
    assert booked.json()["state"] == "DONE"
    assert booked.json()["order_ids"] == ["DN000001"]
    assert "demo" in booked.json()["assistant_text"].casefold()


async def test_real_booking_stays_off_until_its_own_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _real_settings(tmp_path)
    assert settings.allow_real_checkout is True
    assert settings.allow_real_booking is False
    services = FakeVoiceServices(["table"])
    app, _ = _real_app(monkeypatch, settings, [_dining_intent()], services)

    def fake_dining(_: Settings) -> MockDineout:
        return MockDineout()

    monkeypatch.setattr("app.web.dining_adapter", fake_dining)

    async with _local_client(app) as client:
        created = await _create_session(client, "en-IN")
        session_id = str(created["session_id"])
        await _upload(client, session_id)
        readback = await client.post(
            "/api/session/action",
            headers=_session_headers(session_id),
            json={"action": "select_option", "option_index": 0},
        )

    assert readback.json()["state"] == "READBACK"
    assert readback.json()["checkout_enabled"] is False
    assert readback.json()["can_confirm"] is False


async def test_mock_mode_uses_only_mock_providers_for_every_capability() -> None:
    settings = _settings()
    assert isinstance(food_adapter(settings, "addr"), MockFood)
    assert isinstance(dining_adapter(settings), MockDineout)
