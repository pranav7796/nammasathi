"""Same-origin browser API for the local, mock-backed voice demo."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal, cast

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.commerce.base import CommerceError
from app.commerce.runtime import commerce_adapter
from app.config import Settings, load_settings
from app.llm.extractor import GeminiIntentExtractor, IntentExtractionError
from app.orchestrator.state_machine import (
    CallStarted,
    Effect,
    Say,
    State,
    UserCancelled,
    UserConfirmed,
)
from app.orchestrator.turn_runner import TextTurnRunner
from app.voice.gnani import SUPPORTED_LANGUAGES, GnaniError, PrismaSTT, TimbreTTS
from app.voice.presentation import response_text, speech_error_text

MAX_AUDIO_BYTES = 12 * 1024 * 1024
DEFAULT_SESSION_TTL_SECONDS = 30 * 60
STATIC_DIR = Path(__file__).resolve().parent / "static"


class StartSessionRequest(BaseModel):
    language: Literal["kn-IN", "hi-IN", "en-IN"]
    address_id: str | None = None


class SessionActionRequest(BaseModel):
    action: Literal["confirm", "cancel"]
    cart_version: int | None = None
    cart_hash: str | None = None


@dataclass
class BrowserSession:
    runner: TextTurnRunner
    language: str
    last_activity: float
    lock: asyncio.Lock


ExtractorFactory = Callable[[], GeminiIntentExtractor]
STTFactory = Callable[[Settings, str], PrismaSTT]
TTSFactory = Callable[[Settings, str], TimbreTTS]


def create_app(
    *,
    settings: Settings | None = None,
    extractor_factory: ExtractorFactory | None = None,
    stt_factory: STTFactory | None = None,
    tts_factory: TTSFactory | None = None,
    session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
) -> FastAPI:
    """Create an isolated web app; factories make provider calls mockable in tests."""
    sessions: dict[str, BrowserSession] = {}
    sessions_lock = asyncio.Lock()
    extractor_lock = asyncio.Lock()
    default_extractor: GeminiIntentExtractor | None = None

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        load_dotenv()
        runtime_settings = settings or load_settings()
        if (
            runtime_settings.commerce_backend == "swiggy"
            and not runtime_settings.swiggy_live_access
        ) or (runtime_settings.commerce_backend == "mock" and runtime_settings.allow_real_checkout):
            raise RuntimeError(
                "Use COMMERCE_BACKEND=mock by default; live Swiggy requires "
                "SWIGGY_LIVE_ACCESS=true and mock checkout cannot be real"
            )
        application.state.settings = runtime_settings
        application.state.sessions = sessions
        yield
        sessions.clear()

    application = FastAPI(title="Ajji Mart Voice Demo", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    async def resolve_extractor() -> GeminiIntentExtractor:
        if extractor_factory is not None:
            return extractor_factory()
        nonlocal default_extractor
        async with extractor_lock:
            if default_extractor is None:
                default_extractor = GeminiIntentExtractor.from_env()
            return default_extractor

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/api/addresses")
    async def list_addresses(request: Request) -> dict[str, object]:
        try:
            addresses = await commerce_adapter(_get_settings(request)).get_addresses()
        except CommerceError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not read delivery addresses. Connect Swiggy with "
                    "`uv run python -m app.swiggy_auth` and retry."
                ),
            ) from exc
        return {
            "commerce_mode": _get_settings(request).commerce_backend,
            "checkout_enabled": (
                _get_settings(request).commerce_backend == "mock"
                or _get_settings(request).allow_real_checkout
            ),
            "addresses": [
                {"id": item.id, "label": item.label, "full_address": item.full_address}
                for item in addresses
            ],
        }

    @application.post("/api/sessions")
    async def create_session(payload: StartSessionRequest, request: Request) -> dict[str, object]:
        runtime_settings = _get_settings(request)
        try:
            extractor = await resolve_extractor()
            runner = await TextTurnRunner.create(
                commerce_adapter(runtime_settings),
                extractor=extractor,
                address_id=payload.address_id,
            )
            effects = await runner.dispatch(CallStarted(user_known=True, language=payload.language))
        except IntentExtractionError as exc:
            raise HTTPException(status_code=503, detail=exc.user_message) from exc
        except CommerceError as exc:
            raise HTTPException(
                status_code=503, detail="The selected commerce service is unavailable."
            ) from exc

        session_id = token_urlsafe(32)
        entry = BrowserSession(
            runner=runner,
            language=payload.language,
            last_activity=time.monotonic(),
            lock=asyncio.Lock(),
        )
        async with sessions_lock:
            _remove_expired_sessions(sessions, session_ttl_seconds)
            sessions[session_id] = entry
        return await _response_payload(
            entry,
            effects,
            runtime_settings,
            tts_factory,
            session_id=session_id,
        )

    @application.get("/api/session")
    async def get_session(request: Request) -> dict[str, object]:
        session_id = _session_id(request)
        entry = await _get_session(session_id, sessions, sessions_lock, session_ttl_seconds)
        return _session_payload(session_id, entry, _get_settings(request))

    @application.post("/api/session/turn")
    async def upload_turn(request: Request) -> dict[str, object]:
        session_id = _session_id(request)
        entry = await _get_session(session_id, sessions, sessions_lock, session_ttl_seconds)
        requested_language = request.query_params.get("language")
        if requested_language is not None and requested_language not in SUPPORTED_LANGUAGES:
            raise HTTPException(status_code=422, detail="Choose Kannada, Hindi, or English.")
        if requested_language is not None and requested_language != entry.language:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This conversation uses a different language. "
                    "Start a new conversation to switch."
                ),
            )
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].lower()
        if content_type not in {"audio/wav", "audio/x-wav"}:
            raise HTTPException(
                status_code=415, detail="Upload a WAV recording from the microphone."
            )
        audio_bytes = await _read_bounded_audio(request)
        runtime_settings = _get_settings(request)

        async with entry.lock:
            entry.last_activity = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(prefix="ajji-voice-") as temp_dir:
                    audio_path = Path(temp_dir) / "turn.wav"
                    audio_path.write_bytes(audio_bytes)
                    stt = (stt_factory or _default_stt_factory)(runtime_settings, entry.language)
                    transcript = await stt.transcribe(audio_path)
                    effects = await entry.runner.dispatch_text(transcript.text)
                    return await _response_payload(
                        entry,
                        effects,
                        runtime_settings,
                        tts_factory,
                        session_id=session_id,
                        transcript=transcript.text,
                    )
            except GnaniError as exc:
                raise HTTPException(status_code=_gnani_status(exc), detail=str(exc)) from exc
            except (IntentExtractionError, CommerceError) as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Ajji could not safely process that turn. Please try again.",
                ) from exc

    @application.post("/api/session/action", response_model=None)
    async def session_action(
        payload: SessionActionRequest, request: Request
    ) -> dict[str, object] | JSONResponse:
        session_id = _session_id(request)
        entry = await _get_session(session_id, sessions, sessions_lock, session_ttl_seconds)
        runtime_settings = _get_settings(request)
        async with entry.lock:
            entry.last_activity = time.monotonic()
            if payload.action == "confirm":
                if payload.cart_version is None or payload.cart_hash is None:
                    raise HTTPException(
                        status_code=422,
                        detail="Review the current cart before confirming it.",
                    )
                if (
                    payload.cart_version != entry.runner.session.cart_version
                    or payload.cart_hash != entry.runner.session.cart_hash
                ):
                    session = entry.runner.session
                    lines = "; ".join(
                        f"{line.quantity} x {line.pack_size} {line.name}" for line in session.cart
                    )
                    readback = Say(
                        "readback",
                        data={
                            "lines": lines,
                            "total": str(session.total_inr),
                            "payment": "COD",
                            "address_label": session.address_label,
                            "address": session.delivery_address or session.address_label,
                        },
                    )
                    refreshed = await _response_payload(
                        entry,
                        [Say("cart_changed"), readback],
                        runtime_settings,
                        tts_factory,
                        session_id=session_id,
                    )
                    return JSONResponse(status_code=409, content=refreshed)
                event = UserConfirmed(
                    entry.runner.session.cart_version,
                    entry.runner.session.cart_hash,
                )
            else:
                event = UserCancelled()
            effects = await entry.runner.dispatch(event)
            return await _response_payload(
                entry,
                effects,
                runtime_settings,
                tts_factory,
                session_id=session_id,
            )

    return application


def _get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _session_id(request: Request) -> str:
    session_id = request.headers.get("x-session-id", "")
    if not session_id:
        raise HTTPException(status_code=401, detail="Start a conversation first.")
    return session_id


def _default_stt_factory(settings: Settings, language: str) -> PrismaSTT:
    return PrismaSTT(
        settings.gnani_api_key or "",
        language=language,
        timeout=settings.gnani_timeout_seconds,
    )


def _default_tts_factory(settings: Settings, language: str) -> TimbreTTS:
    return TimbreTTS(
        settings.gnani_api_key or "",
        voice=settings.gnani_tts_voice,
        language=language,
        model=settings.gnani_tts_model,
        speed=settings.gnani_tts_speed,
        timeout=settings.gnani_timeout_seconds,
    )


def _session_payload(
    session_id: str, entry: BrowserSession, settings: Settings
) -> dict[str, object]:
    session = entry.runner.session
    cart = [
        {
            "name": line.name,
            "pack_size": line.pack_size,
            "quantity": line.quantity,
            "unit_price_inr": line.unit_price_inr,
            "line_total_inr": line.quantity * line.unit_price_inr,
        }
        for line in session.cart
    ]
    return {
        "session_id": session_id,
        "state": session.state.value,
        "language": entry.language,
        "cart_version": session.cart_version,
        "cart_hash": session.cart_hash,
        "cart": cart,
        "total_inr": session.total_inr,
        "address_label": session.address_label,
        "delivery_address": session.delivery_address,
        "payment_method": "COD",
        "commerce_mode": settings.commerce_backend,
        "checkout_enabled": settings.commerce_backend == "mock" or settings.allow_real_checkout,
        "show_readback": session.state is State.READBACK and bool(session.cart),
        "can_confirm": (
            (settings.commerce_backend == "mock" or settings.allow_real_checkout)
            and session.state is State.READBACK
            and bool(session.cart)
        ),
        "tts_language": entry.language,
    }


async def _response_payload(
    entry: BrowserSession,
    effects: list[Effect],
    settings: Settings,
    tts_factory: TTSFactory | None,
    *,
    session_id: str,
    transcript: str | None = None,
) -> dict[str, object]:
    message = response_text(effects, language=entry.language)
    audio_base64: str | None = None
    audio_error: str | None = None
    if message:
        try:
            with tempfile.TemporaryDirectory(prefix="ajji-reply-") as temp_dir:
                audio_path = (tts_factory or _default_tts_factory)(settings, entry.language)
                output = await audio_path.synthesize(message, Path(temp_dir) / "reply.wav")
                audio_base64 = base64.b64encode(output.read_bytes()).decode("ascii")
        except GnaniError:
            audio_error = speech_error_text(entry.language)
        except OSError:
            audio_error = "Ajji's audio response could not be saved."

    result = _session_payload(session_id, entry, settings)
    result.update(
        {
            "transcript": transcript,
            "assistant_text": message,
            "audio_base64": audio_base64,
            "audio_error": audio_error,
        }
    )
    return result


async def _read_bounded_audio(request: Request) -> bytes:
    declared_size = request.headers.get("content-length")
    if declared_size is not None:
        try:
            if int(declared_size) > MAX_AUDIO_BYTES:
                raise HTTPException(status_code=413, detail="Recording is too large.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid upload size.") from exc
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Recording is too large.")
    if not audio:
        raise HTTPException(status_code=400, detail="The audio recording is empty.")
    return bytes(audio)


async def _get_session(
    session_id: str,
    sessions: dict[str, BrowserSession],
    sessions_lock: asyncio.Lock,
    ttl_seconds: float,
) -> BrowserSession:
    async with sessions_lock:
        entry = sessions.get(session_id)
        now = time.monotonic()
        if entry is None or now - entry.last_activity >= ttl_seconds:
            sessions.pop(session_id, None)
            raise HTTPException(
                status_code=410,
                detail="This conversation expired. Start a new conversation to continue.",
            )
        entry.last_activity = now
        return entry


def _remove_expired_sessions(sessions: dict[str, BrowserSession], ttl_seconds: float) -> None:
    now = time.monotonic()
    expired = [
        session_id
        for session_id, entry in sessions.items()
        if now - entry.last_activity >= ttl_seconds
    ]
    for session_id in expired:
        sessions.pop(session_id, None)


def _gnani_status(error: GnaniError) -> int:
    if error.category == "invalid_audio":
        return 422
    if error.category == "rate_limit":
        return 429
    if error.category in {"authentication", "configuration"}:
        return 503
    return 502


app = create_app()
