"""Same-origin browser API for the local voice assistant."""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal, cast
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.assistant.orchestrator import Assistant, Reply, Workflow
from app.assistant.router import Domain
from app.assistant.workflows import OrderWorkflow
from app.commerce.base import (
    Address,
    CommerceAdapter,
    CommerceAuthRequired,
    CommerceError,
    CommerceRejected,
    same_address_id,
)
from app.commerce.runtime import (
    checkout_policy,
    commerce_adapter,
    dining_adapter,
    food_adapter,
    order_journal,
)
from app.commerce.swiggy import SwiggyMcpClient
from app.config import Settings, load_settings
from app.dining.base import DiningAdapter
from app.dining.workflow import DiningWorkflow
from app.llm.extractor import GeminiIntentExtractor, IntentExtractionError
from app.observability import configure_logging, log_event
from app.orchestrator.state_machine import Money, Say
from app.orchestrator.turn_runner import TextTurnRunner
from app.voice.gnani import (
    DEFAULT_VOICES,
    SUPPORTED_LANGUAGES,
    GnaniError,
    PrismaSTT,
    TimbreTTS,
)
from app.voice.presentation import response_text, speech_error_text

MAX_AUDIO_BYTES = 12 * 1024 * 1024
DEFAULT_SESSION_TTL_SECONDS = 30 * 60
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
logger = logging.getLogger(__name__)


class StartSessionRequest(BaseModel):
    language: Literal["kn-IN", "hi-IN", "en-IN"]
    address_id: str | None = None


class SessionActionRequest(BaseModel):
    action: Literal["confirm", "cancel", "select_option"]
    cart_version: int | None = None
    cart_hash: str | None = None
    option_index: int | None = None


@dataclass
class BrowserSession:
    assistant: Assistant
    language: str
    last_activity: float
    lock: asyncio.Lock
    last_reply: str = ""

    @property
    def runner(self) -> TextTurnRunner:
        """The grocery order engine, for inspection in tests."""
        workflow = self.assistant.workflows["grocery"]
        assert isinstance(workflow, OrderWorkflow)
        assert workflow.runner is not None
        return workflow.runner


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
    shared_adapter: CommerceAdapter | None = None
    shared_food: dict[str, CommerceAdapter] = {}
    shared_dining: DiningAdapter | None = None

    def food_for(runtime_settings: Settings, address_id: str) -> CommerceAdapter:
        if runtime_settings.commerce_backend == "mock":
            return food_adapter(runtime_settings, address_id)
        if address_id not in shared_food:
            shared_food[address_id] = food_adapter(runtime_settings, address_id)
        return shared_food[address_id]

    def dining_for(runtime_settings: Settings) -> DiningAdapter:
        nonlocal shared_dining
        if runtime_settings.commerce_backend == "mock":
            return dining_adapter(runtime_settings)
        if shared_dining is None:
            shared_dining = dining_adapter(runtime_settings)
        return shared_dining

    def adapter_for(runtime_settings: Settings) -> CommerceAdapter:
        """Mock carts are per session; a live account has one cart and one MCP session."""
        nonlocal shared_adapter
        if runtime_settings.commerce_backend == "mock":
            return commerce_adapter(runtime_settings)
        if shared_adapter is None:
            shared_adapter = commerce_adapter(runtime_settings)
        return shared_adapter

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if settings is None:
            load_dotenv()
        runtime_settings = settings or load_settings()
        configure_logging()
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
        log_event("app_started", mode=runtime_settings.commerce_backend)
        yield
        sessions.clear()
        for client in (shared_adapter, shared_dining, *shared_food.values()):
            if isinstance(client, SwiggyMcpClient):
                await client.aclose()

    application = FastAPI(title="Namma Saathi", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.middleware("http")
    async def local_only_for_real_account(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """A real account sits behind this API, so accept only this machine's own browser."""
        runtime_settings = cast(Settings | None, getattr(request.app.state, "settings", None))
        if (
            runtime_settings is not None
            and runtime_settings.commerce_backend != "mock"
            and not _is_local_request(request)
        ):
            return JSONResponse(status_code=403, content={"detail": "Local access only."})
        return await call_next(request)

    async def resolve_extractor() -> GeminiIntentExtractor:
        if extractor_factory is not None:
            return extractor_factory()
        nonlocal default_extractor
        async with extractor_lock:
            if default_extractor is None:
                allow_logged = cast(Settings, application.state.settings).commerce_backend == "mock"
                default_extractor = await asyncio.to_thread(
                    GeminiIntentExtractor.from_env, allow_logged_fallback=allow_logged
                )
            return default_extractor

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/api/addresses")
    async def list_addresses(request: Request) -> dict[str, object]:
        try:
            addresses = await adapter_for(_get_settings(request)).get_addresses()
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
            grocery = adapter_for(runtime_settings)
            address = _select_address(await grocery.get_addresses(), payload.address_id)
            journal = order_journal(runtime_settings)
            policy = checkout_policy(runtime_settings)

            async def build(domain: Domain) -> Workflow:
                if domain == "dining":
                    return DiningWorkflow(
                        dining_for(runtime_settings),
                        journal,
                        policy,
                        today=date.today(),
                    )
                adapter = food_for(runtime_settings, address.id) if domain == "food" else grocery
                return OrderWorkflow(
                    domain,
                    adapter,
                    address_id=address.id,
                    language=payload.language,
                    extractor=extractor,
                    journal=journal,
                    policy=policy,
                )

            assistant = Assistant(
                language=payload.language,
                extractor=extractor,
                factory=build,
                journal=journal,
                address_label=address.label,
                delivery_address=address.full_address,
                today=date.today(),
            )
            reply = await assistant.start()
        except IntentExtractionError as exc:
            raise HTTPException(status_code=503, detail=exc.user_message) from exc
        except CommerceAuthRequired as exc:
            raise HTTPException(
                status_code=503,
                detail="Swiggy login has expired. Run `uv run python -m app.swiggy_auth`.",
            ) from exc
        except CommerceRejected as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Swiggy could not open your basket just now, and nothing was changed. "
                    "Please try again in a minute, and check the Swiggy app if it continues."
                ),
            ) from exc
        except CommerceError as exc:
            raise HTTPException(
                status_code=503, detail="The selected commerce service is unavailable."
            ) from exc

        session_id = token_urlsafe(32)
        entry = BrowserSession(
            assistant=assistant,
            language=payload.language,
            last_activity=time.monotonic(),
            lock=asyncio.Lock(),
        )
        async with sessions_lock:
            _remove_expired_sessions(sessions, session_ttl_seconds)
            sessions[session_id] = entry
        return await _response_payload(
            entry,
            reply,
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
                    reply = await entry.assistant.turn(
                        transcript.text, heard=_heard_readback(request, runtime_settings)
                    )
                    return await _response_payload(
                        entry,
                        reply,
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
                    detail="Saathi could not safely process that turn. Please try again.",
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
                view = entry.assistant.view()
                stale = (
                    payload.cart_version != view.readback_version
                    or payload.cart_hash != view.readback_hash
                )
                # A stale page gets a fresh readback from the workflow, never an order.
                reply = await _run_action(
                    entry.assistant.confirm(payload.cart_version, payload.cart_hash)
                )
                if stale:
                    refreshed = await _response_payload(
                        entry,
                        Reply([Say("cart_changed"), *reply.effects], reply.domain),
                        runtime_settings,
                        tts_factory,
                        session_id=session_id,
                    )
                    return JSONResponse(status_code=409, content=refreshed)
            elif payload.action == "select_option":
                if payload.option_index is None:
                    raise HTTPException(status_code=422, detail="Choose one of the options.")
                reply = await _run_action(entry.assistant.choose(payload.option_index))
            else:
                reply = await _run_action(entry.assistant.cancel())
            return await _response_payload(
                entry,
                reply,
                runtime_settings,
                tts_factory,
                session_id=session_id,
            )

    return application


def _select_address(addresses: list[Address], address_id: str | None) -> Address:
    if not addresses:
        raise CommerceError("No delivery address available")
    if address_id is None:
        if len(addresses) != 1:
            raise CommerceError("Select a delivery address before starting")
        return addresses[0]
    for address in addresses:
        if same_address_id(address.id, address_id):
            return address
    raise CommerceError("Selected delivery address was not found")


async def _run_action(action: Awaitable[Reply]) -> Reply:
    try:
        return await action
    except (IntentExtractionError, CommerceError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Saathi could not safely process that turn. Please try again.",
        ) from exc


def _is_local_request(request: Request) -> bool:
    client = request.client.host if request.client else ""
    host_header = request.headers.get("host", "")
    host = urlsplit(f"//{host_header}").hostname or ""
    origin = request.headers.get("origin")
    same_origin = origin is None or origin == f"http://{host_header}"
    return client in LOOPBACK_CLIENTS and host in LOOPBACK_HOSTS and same_origin


def _heard_readback(request: Request, settings: Settings) -> tuple[int, str] | None:
    """The readback this browser actually received; a live order requires it."""
    version = request.headers.get("x-heard-cart-version", "")
    digest = request.headers.get("x-heard-cart-hash", "")
    if version.isdigit() and digest:
        return int(version), digest
    return None if settings.commerce_backend == "mock" else (-1, "")


def _money_json(amount: Money) -> int | float:
    value = Decimal(amount)
    return int(value) if value == value.to_integral_value() else float(value)


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
        voice=settings.gnani_tts_voice or DEFAULT_VOICES[language],
        language=language,
        model=settings.gnani_tts_model,
        speed=settings.gnani_tts_speed,
        timeout=settings.gnani_timeout_seconds,
    )


def _session_payload(
    session_id: str, entry: BrowserSession, settings: Settings
) -> dict[str, object]:
    view = entry.assistant.view()
    checkout_enabled = settings.commerce_backend == "mock" or (
        settings.allow_real_booking if view.domain == "dining" else settings.allow_real_checkout
    )
    cart = [
        {
            "name": line.name,
            "pack_size": line.pack_size,
            "quantity": line.quantity,
            "unit_price_inr": _money_json(line.unit_price_inr),
            "line_total_inr": _money_json(line.quantity * line.unit_price_inr),
        }
        for line in view.lines
    ]
    return {
        "session_id": session_id,
        "capability": view.domain,
        "state": view.state,
        "language": entry.language,
        "cart_version": view.cart_version,
        "cart_hash": view.cart_hash,
        "cart": cart,
        "total_inr": _money_json(view.total_inr),
        "fees_inr": _money_json(view.fees_inr),
        "savings_inr": _money_json(view.savings_inr),
        "store_count": view.store_count,
        "issues": list(view.issues),
        "address_label": view.address_label,
        "delivery_address": view.delivery_address,
        "payment_method": view.payment_method,
        "commerce_mode": settings.commerce_backend,
        "checkout_enabled": checkout_enabled,
        "show_readback": view.state == "READBACK" and bool(view.lines),
        "can_confirm": checkout_enabled and view.confirmable,
        "options": [
            {
                "name": option.name,
                "pack_size": option.pack_size,
                "quantity": option.quantity,
                "unit_price_inr": _money_json(option.unit_price_inr),
            }
            for option in view.options
        ],
        "order_ids": list(view.order_ids),
        "order_status": view.order_status,
        "order_verified": view.order_verified,
        "order_partial": view.order_partial,
        "last_reply": entry.last_reply,
        "tts_language": entry.language,
    }


async def _response_payload(
    entry: BrowserSession,
    reply: Reply,
    settings: Settings,
    tts_factory: TTSFactory | None,
    *,
    session_id: str,
    transcript: str | None = None,
) -> dict[str, object]:
    message = response_text(
        reply.effects,
        language=entry.language,
        commerce_mode=settings.commerce_backend,
        domain=reply.domain or "grocery",
    )
    if message:
        entry.last_reply = message  # survives a lost response, e.g. after a placed order
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
            audio_error = "Saathi's audio response could not be saved."
        except Exception:
            # Speech must never hide what just happened, least of all a placed order.
            logger.exception("Reply synthesis failed")
            audio_error = speech_error_text(entry.language)

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
