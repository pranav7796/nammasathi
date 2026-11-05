from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.commerce.mock import MockInstamart
from app.llm.extractor import GeminiIntentExtractor, GroceryIntent, GroceryItem
from app.orchestrator.state_machine import CallStarted, State
from app.orchestrator.turn_runner import TextTurnRunner
from app.voice.gnani import GnaniError, PrismaSTT, TimbreTTS
from tests.test_gemini_extractor import FakeResponse


class ResponseQueueClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = iter(responses)
        self.requests: list[httpx.Request] = []

    async def __aenter__(self) -> ResponseQueueClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        self.requests.append(request)
        response = next(self.responses)
        response.request = request
        return response


def wav_bytes(seconds: int = 1) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * (8000 * seconds))
    return stream.getvalue()


def response(
    status: int, *, json_body: object | None = None, content: bytes = b""
) -> httpx.Response:
    if json_body is not None:
        return httpx.Response(status, json=json_body)
    return httpx.Response(status, content=content, headers={"content-type": "audio/wav"})


def patch_httpx(
    monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]
) -> ResponseQueueClient:
    client = ResponseQueueClient(responses)

    def fake_async_client(**kwargs: Any) -> ResponseQueueClient:
        del kwargs
        return client

    monkeypatch.setattr("app.voice.gnani.httpx.AsyncClient", fake_async_client)
    return client


@pytest.mark.asyncio
async def test_prisma_success_and_multipart_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio_path = tmp_path / "turn.wav"
    audio_path.write_bytes(wav_bytes())
    client = patch_httpx(
        monkeypatch,
        [response(200, json_body={"success": True, "transcript": "2 kg akki"})],
    )
    transcript = await PrismaSTT("secret", language="kn-IN").transcribe(audio_path)
    assert transcript.text == "2 kg akki"
    assert client.requests[0].url.path == "/stt/v3"


@pytest.mark.asyncio
async def test_prisma_invalid_audio_rejected_before_network(tmp_path: Path) -> None:
    with pytest.raises(GnaniError, match="does not exist"):
        await PrismaSTT("secret", language="kn-IN").transcribe(tmp_path / "missing.wav")


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "authentication"), (429, "rate_limit"), (503, "service")],
)
@pytest.mark.asyncio
async def test_prisma_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int, category: str
) -> None:
    path = tmp_path / "turn.wav"
    path.write_bytes(wav_bytes())
    patch_httpx(monkeypatch, [response(status), response(status)])
    with pytest.raises(GnaniError) as error:
        await PrismaSTT("secret", language="kn-IN").transcribe(path)
    assert error.value.category == category


@pytest.mark.asyncio
async def test_prisma_retries_once_after_a_brief_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "turn.wav"
    path.write_bytes(wav_bytes())
    ok = response(200, json_body={"success": True, "transcript": "ಹಾಲು"})
    patch_httpx(monkeypatch, [response(503), ok])
    result = await PrismaSTT("secret", language="kn-IN").transcribe(path)
    assert result.text == "ಹಾಲು"


@pytest.mark.asyncio
async def test_prisma_does_not_retry_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "turn.wav"
    path.write_bytes(wav_bytes())
    ok = response(200, json_body={"success": True, "transcript": "ಹಾಲು"})
    patch_httpx(monkeypatch, [response(401), ok])
    with pytest.raises(GnaniError):
        await PrismaSTT("secret", language="kn-IN").transcribe(path)


@pytest.mark.asyncio
async def test_prisma_rejects_wav_over_documented_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "long.wav"
    path.write_bytes(wav_bytes(31))
    with pytest.raises(GnaniError, match="30 seconds"):
        await PrismaSTT("secret", language="kn-IN").transcribe(path)


@pytest.mark.asyncio
async def test_timbre_valid_wav_written_and_format_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = patch_httpx(monkeypatch, [response(200, content=wav_bytes())])
    path = await TimbreTTS("secret", voice="Saanvi", language="kn-IN").synthesize(
        "Hello", tmp_path / "reply.wav"
    )
    assert path.read_bytes().startswith(b"RIFF")
    request = client.requests[0]
    assert request.url.path.endswith("/tts/inference")


@pytest.mark.asyncio
async def test_timbre_invalid_audio_response_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_httpx(monkeypatch, [response(200, content=b"not a wav")])
    with pytest.raises(GnaniError, match="valid WAV"):
        await TimbreTTS("secret", voice="Saanvi", language="kn-IN").synthesize(
            "Hello", tmp_path / "bad.wav"
        )


@pytest.mark.parametrize(("status", "category"), [(403, "authentication"), (503, "service")])
@pytest.mark.asyncio
async def test_timbre_api_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int, category: str
) -> None:
    patch_httpx(monkeypatch, [response(status)])
    with pytest.raises(GnaniError) as error:
        await TimbreTTS("secret", voice="Saanvi", language="kn-IN").synthesize(
            "Hello", tmp_path / "x.wav"
        )
    assert error.value.category == category


@pytest.mark.parametrize("language", ["kn-IN", "hi-IN", "en-IN"])
@pytest.mark.asyncio
async def test_timbre_language_passed_in_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, language: str
) -> None:
    client = patch_httpx(monkeypatch, [response(200, content=wav_bytes())])
    await TimbreTTS("secret", voice="Saanvi", language=language).synthesize(
        "Test speech", tmp_path / "out.wav"
    )
    assert client.requests
    assert client.requests[0].url.path.endswith("/tts/inference")


@pytest.mark.parametrize("language", ["kn-IN", "hi-IN", "en-IN"])
@pytest.mark.asyncio
async def test_prisma_language_passed_in_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, language: str
) -> None:
    audio_path = tmp_path / "input.wav"
    audio_path.write_bytes(wav_bytes())
    client = patch_httpx(
        monkeypatch,
        [response(200, json_body={"success": True, "transcript": "test transcript"})],
    )
    transcript = await PrismaSTT("secret", language=language).transcribe(audio_path)
    assert transcript.language == language
    assert transcript.text == "test transcript"
    assert client.requests[0].url.path.endswith("/stt/v3")


class FakeGeminiModels:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)

    def generate_content(self, **kwargs: object) -> FakeResponse:
        return next(self.responses)


class FakeGeminiClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.models = FakeGeminiModels(responses)


@pytest.mark.asyncio
async def test_voice_turns_preserve_cart_and_require_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio_path = tmp_path / "request.wav"
    audio_path.write_bytes(wav_bytes())
    transcripts = [
        response(200, json_body={"success": True, "transcript": "add 5 kg atta"}),
        response(200, content=wav_bytes()),
        response(200, json_body={"success": True, "transcript": "add 1 kg rice"}),
        response(200, content=wav_bytes()),
        response(200, json_body={"success": True, "transcript": "yes confirm"}),
        response(200, content=wav_bytes()),
    ]
    patch_httpx(monkeypatch, transcripts)
    gemini = GeminiIntentExtractor(
        FakeGeminiClient(
            [
                FakeResponse(
                    parsed=GroceryIntent(
                        intent="add_items", items=[GroceryItem(name="atta", quantity=5, unit="kg")]
                    )
                ),
                FakeResponse(
                    parsed=GroceryIntent(
                        intent="add_items", items=[GroceryItem(name="rice", quantity=1, unit="kg")]
                    )
                ),
                FakeResponse(parsed=GroceryIntent(intent="confirm", items=[])),
            ]
        )
    )
    mock = MockInstamart()
    runner = await TextTurnRunner.create(mock, extractor=gemini)
    await runner.dispatch(CallStarted(user_known=True))
    stt = PrismaSTT("secret", language="kn-IN")
    tts = TimbreTTS("secret", voice="Saanvi", language="kn-IN")
    checkout_before_confirm = mock.checkout_calls

    for turn in range(2):
        text = (await stt.transcribe(audio_path)).text
        effects = await runner.dispatch_text(text)
        del effects
        await tts.synthesize("response", tmp_path / f"reply-{turn}.wav")
        assert runner.session.state is State.READBACK
    assert mock.checkout_calls == checkout_before_confirm == 0

    text = (await stt.transcribe(audio_path)).text
    await runner.dispatch_text(text)
    await tts.synthesize("order placed", tmp_path / "reply-final.wav")
    assert runner.session.state is State.DONE
    assert mock.checkout_calls == 1


@pytest.mark.asyncio
async def test_cart_edit_invalidates_confirmation_and_no_live_commerce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "turn.wav"
    path.write_bytes(wav_bytes())
    patch_httpx(monkeypatch, [response(200, json_body={"success": True, "transcript": "add rice"})])
    extractor = GeminiIntentExtractor(
        FakeGeminiClient(
            [
                FakeResponse(
                    parsed=GroceryIntent(
                        intent="add_items", items=[GroceryItem(name="rice", quantity=1, unit="kg")]
                    )
                )
            ]
        )
    )
    mock = MockInstamart()
    runner = await TextTurnRunner.create(mock, extractor=extractor)
    await runner.dispatch(CallStarted(user_known=True))
    await runner.dispatch_text((await PrismaSTT("secret", language="kn-IN").transcribe(path)).text)
    assert runner.session.confirmed_version is None
    assert mock.checkout_calls == 0
