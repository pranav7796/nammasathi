"""Same-origin browser API for the voice assistant, on one machine or as a hosted service."""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg_pool import PoolTimeout
from pydantic import BaseModel

from app.accounts.mailer import Mailer
from app.accounts.routes import account_router
from app.accounts.service import (
    UNAVAILABLE_MESSAGE,
    AccountService,
    AccountsUnavailable,
    SwiggyStub,
)
from app.accounts.store import DemoOrder, Turn, User
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
from app.commerce.order_journal import OrderJournal
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
from app.genui.builders import CalendarInput, share_panel_widget
from app.genui.models import Widget
from app.genui.validate import GenUIError, to_wire
from app.google.oauth import ConnectionStore, GoogleOAuth
from app.google.routes import google_router
from app.integrations.routes import integrations_router
from app.integrations.status import IntegrationNotes
from app.llm.chain import SCRIPTED, build_extractor
from app.llm.extractor import IntentExtractionError
from app.memory.routes import memory_router
from app.myday import myday_router
from app.oauth_web import callback_url, oauth_router
from app.observability import configure_logging, log_event
from app.orchestrator.confirmation import explicit_confirmation
from app.orchestrator.state_machine import Money, Say
from app.orchestrator.turn_runner import IntentExtractor, TextTurnRunner
from app.personal import voice as reminder_voice
from app.personal.dates import IST
from app.personal.routes import personal_router
from app.personal.service import PersonalService, WriteLimited
from app.personal.store import PersonalError
from app.tasks import turn as task_turn
from app.tasks.engine import TaskEngine
from app.tasks.llm_planner import LlmPlanExtractor
from app.tasks.planner import PlanExtractor
from app.tasks.routes import tasks_router
from app.tasks.services import ServiceHub
from app.tasks.store import TaskStore
from app.timing import TurnTimer, begin_turn, stage
from app.voice.gnani import (
    DEFAULT_VOICES,
    SUPPORTED_LANGUAGES,
    GnaniError,
    PrismaSTT,
    TimbreTTS,
)
from app.voice.presentation import response_text, speech_error_text
from app.waitlist import waitlist_router
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.routes import whatsapp_router

MAX_AUDIO_BYTES = 12 * 1024 * 1024
MAX_TYPED_CHARACTERS = 400
MAX_JSON_BYTES = 64 * 1024
DEFAULT_SESSION_TTL_SECONDS = 30 * 60
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
logger = logging.getLogger(__name__)


class StartSessionRequest(BaseModel):
    language: Literal["kn-IN", "hi-IN", "en-IN"]
    address_id: str | None = None
    live: bool = False  # the person's own Swiggy account; never assumed


class TextTurnRequest(BaseModel):
    text: str


class SessionActionRequest(BaseModel):
    action: Literal["confirm", "cancel", "select_option", "resume"]
    capability: Literal["grocery", "food", "dining"] | None = None
    cart_version: int | None = None
    cart_hash: str | None = None
    option_index: int | None = None


@dataclass
class BrowserSession:
    assistant: Assistant
    language: str
    last_activity: float
    lock: asyncio.Lock
    settings: Settings
    last_reply: str = ""
    owner: str | None = None  # the signed-in user, who alone may use this conversation
    recorded_orders: set[str] = field(default_factory=set[str])
    # Which workflow the person is in now: "order" (groceries, food, tables), whatsapp, email,
    # calendar, reminders, day, tasks (a mixed board), or "none" before anything has been asked.
    # The page shows that workflow's panel. An unfinished order is kept, not shown, meanwhile.
    workflow: str = "none"
    waiting_for: str | None = None

    @property
    def runner(self) -> TextTurnRunner:
        """The grocery order engine, for inspection in tests."""
        workflow = self.assistant.workflows["grocery"]
        assert isinstance(workflow, OrderWorkflow)
        assert workflow.runner is not None
        return workflow.runner


ExtractorFactory = Callable[[], IntentExtractor]
STTFactory = Callable[[Settings, str], PrismaSTT]
TTSFactory = Callable[[Settings, str], TimbreTTS]


class DemoBudget:
    """A public demo spends real speech and LLM quota, so it gets a fixed daily allowance."""

    def __init__(self) -> None:
        self.day = ""
        self.used = 0

    def spend(self, limit: int) -> bool:
        today = datetime.now(UTC).date().isoformat()
        if today != self.day:
            self.day, self.used = today, 0
        if self.used >= limit:
            return False
        self.used += 1
        return True


def create_app(
    *,
    settings: Settings | None = None,
    extractor_factory: ExtractorFactory | None = None,
    stt_factory: STTFactory | None = None,
    tts_factory: TTSFactory | None = None,
    session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    oauth_transport: httpx.AsyncBaseTransport | None = None,
    mailer: Mailer | None = None,
    swiggy_stub: SwiggyStub | None = None,
) -> FastAPI:
    """Create an isolated web app; factories make provider calls mockable in tests."""
    sessions: dict[str, BrowserSession] = {}
    sessions_lock = asyncio.Lock()
    extractor_lock = asyncio.Lock()
    default_extractor: IntentExtractor | None = None
    budget = DemoBudget()
    site_budget = DemoBudget()  # every turn, signed in or not: protects speech and LLM quota
    user_budgets: dict[str, DemoBudget] = {}
    current_conversation: dict[str, str] = {}  # user id -> their one conversation
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
        service = (
            await AccountService.start(runtime_settings, mailer=mailer, stub=swiggy_stub)
            if runtime_settings.accounts_enabled
            else None
        )
        application.state.accounts = service
        application.state.close_user_conversations = close_user_conversations
        application.state.google = None
        application.state.whatsapp = None
        application.state.hub = None
        application.state.task_store = None
        application.state.integration_notes = IntegrationNotes()
        application.state.tasks = None
        engine: TaskEngine | None = None
        application.state.plan_extractor = None
        if service is not None:
            # Tests set this to stand in for Google and Meta. In production it is unset.
            transport = getattr(application.state, "integration_transport", None)
            google = GoogleOAuth(runtime_settings, ConnectionStore(service), transport=transport)
            application.state.google = google
            if runtime_settings.whatsapp_configured:
                application.state.whatsapp = WhatsAppClient(
                    runtime_settings, service, transport=transport
                )
            hub = ServiceHub(service, google, transport=transport)
            task_store = TaskStore(service.database)
            application.state.hub = hub
            application.state.task_store = task_store
            engine = TaskEngine(
                task_store,
                hub.for_user,
                run_blocking=service.run,
                notes=application.state.integration_notes,
            )
            try:
                await engine.start()
            except AccountsUnavailable:
                log_event("tasks", op="recover", outcome="database_unavailable")
            application.state.tasks = engine
            if runtime_settings.demo_understanding == "llm" and (
                runtime_settings.groq_api_key or runtime_settings.gemini_api_key
            ):
                application.state.plan_extractor = LlmPlanExtractor(runtime_settings)
        log_event("app_started", mode=runtime_settings.commerce_backend)
        yield
        sessions.clear()
        if engine is not None:
            await engine.close()
        if service is not None:
            await service.close()
        for client in (shared_adapter, shared_dining, *shared_food.values()):
            if isinstance(client, SwiggyMcpClient):
                await client.aclose()

    application = FastAPI(title="Namma Saathi", lifespan=lifespan)

    async def accounts_unavailable(request: Request, exc: Exception) -> JSONResponse:
        del request
        log_event("database", op="request", outcome="unavailable", kind=type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": UNAVAILABLE_MESSAGE})

    for database_error in (AccountsUnavailable, psycopg.Error, PoolTimeout, sqlite3.Error):
        application.add_exception_handler(database_error, accounts_unavailable)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    application.include_router(oauth_router(oauth_transport))
    application.include_router(account_router())
    application.include_router(waitlist_router())
    application.include_router(personal_router())
    application.include_router(google_router())
    application.include_router(integrations_router())
    application.include_router(myday_router())
    application.include_router(whatsapp_router())
    application.include_router(memory_router())
    application.include_router(tasks_router())

    async def close_user_conversations(user_id: str) -> None:
        """After sign-out, disconnect, or deletion nothing of this user's may keep running."""
        async with sessions_lock:
            for session_id, entry in list(sessions.items()):
                if entry.owner == user_id:
                    del sessions[session_id]
        current_conversation.pop(user_id, None)
        service = cast(AccountService | None, application.state.accounts)
        if service is not None:
            await service.drop_adapters(user_id)

    @application.middleware("http")
    async def local_only_for_real_account(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """One shared login sits behind this API, so accept only this machine's own browser.

        With MULTI_USER_LIVE there is no shared login: each request acts only for its own user.
        """
        runtime_settings = cast(Settings | None, getattr(request.app.state, "settings", None))
        if (
            runtime_settings is not None
            and runtime_settings.commerce_backend != "mock"
            and not runtime_settings.multi_user_live
            and not _is_local_request(request)
        ):
            return JSONResponse(status_code=403, content={"detail": "Local access only."})
        return await call_next(request)

    @application.middleware("http")
    async def same_origin_writes_and_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Refuse cross-site writes, which would otherwise carry the sign-in cookie."""
        runtime_settings = cast(Settings | None, getattr(request.app.state, "settings", None))
        declared = request.headers.get("content-length", "0")
        if (
            request.url.path != "/api/session/turn"
            and declared.isdigit()
            and int(declared) > MAX_JSON_BYTES
        ):
            return JSONResponse(status_code=413, content={"detail": "That is too large."})
        if request.method not in {"GET", "HEAD", "OPTIONS"} and _cross_site(
            request, runtime_settings
        ):
            log_event("request_refused", reason="cross_site")
            return JSONResponse(status_code=403, content={"detail": "Cross-site request refused."})
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
        if runtime_settings is not None and runtime_settings.public_base_url.startswith("https://"):
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path.startswith(("/api/", "/auth/")):
            headers.setdefault("Cache-Control", "no-store")
        return response

    def accounts_of(request: Request) -> AccountService | None:
        return cast(AccountService | None, getattr(request.app.state, "accounts", None))

    async def signed_in(request: Request) -> User | None:
        """The signed-in person, or None. If accounts are down, they can still use the demo."""
        service = accounts_of(request)
        if service is None:
            return None
        try:
            return await service.current_user(request)
        except AccountsUnavailable:
            return None

    async def owned_session(request: Request) -> tuple[str, BrowserSession]:
        """A conversation, only for the person it belongs to. Others see it as not existing."""
        session_id = _session_id(request)
        entry = await _get_session(session_id, sessions, sessions_lock, session_ttl_seconds)
        if entry.owner is not None:
            service = accounts_of(request)
            # Ownership must be proven, so a database outage is a 503, never a free pass.
            user = await service.current_user(request) if service is not None else None
            if user is None or user.id != entry.owner:
                log_event("request_refused", reason="not_owner")
                raise HTTPException(
                    status_code=410,
                    detail="This conversation expired. Start a new conversation to continue.",
                )
            multi_user = _get_settings(request).multi_user_live
            if multi_user and entry.settings.commerce_backend == "swiggy" and not user.live_access:
                # An operator's revoke takes effect on this person's very next turn.
                async with sessions_lock:
                    sessions.pop(session_id, None)
                raise HTTPException(
                    status_code=410, detail="Live Swiggy is no longer available on this account."
                )
        return session_id, entry

    def spend_turn(settings_now: Settings, owner: str | None) -> None:
        """Signed-in people have their own daily allowance, so a busy anonymous demo can't lock
        them out. Everyone together stays under one site-wide ceiling, which also bounds anyone
        who makes extra accounts to get more turns."""
        if settings_now.public_deployment and not site_budget.spend(
            settings_now.global_daily_turn_limit
        ):
            raise HTTPException(
                status_code=429,
                detail="Saathi is very busy today and has paused for now. Please try tomorrow.",
            )
        if owner is None:
            spend_public_budget(settings_now)
            return
        allowance = user_budgets.setdefault(owner, DemoBudget())
        if not allowance.spend(settings_now.user_daily_turn_limit):
            raise HTTPException(
                status_code=429,
                detail="You have reached today's limit. Please try again tomorrow.",
            )

    async def remember(
        request: Request, entry: BrowserSession, said: str, result: dict[str, object]
    ) -> None:
        """Keep a signed-in person's demo conversation and finished demo orders.

        Best effort: a database hiccup never costs the person their reply. Live turns are not
        kept, because their replies hold real addresses and orders, which live in Swiggy.
        """
        service = accounts_of(request)
        if service is None or entry.owner is None or entry.settings.commerce_backend != "mock":
            return
        now = int(time.time() * 1000)
        reply = str(result.get("assistant_text") or "")
        try:
            if said or reply:
                await service.run(
                    service.store.add_turn,
                    entry.owner,
                    Turn(now, entry.language, str(result.get("capability") or ""), said, reply),
                )
            ids = [str(item) for item in cast(list[object], result.get("order_ids") or [])]
            fresh = [order_id for order_id in ids if order_id not in entry.recorded_orders]
            if result.get("state") == "DONE" and fresh:
                cart = cast(list[dict[str, object]], result.get("cart") or [])
                capability = str(result.get("capability") or "")
                summary = ", ".join(
                    f"{line['name']} ({line['pack_size']})"
                    if capability == "dining" or not line.get("quantity")
                    else f"{line['quantity']} x {line['name']}"
                    for line in cart
                )
                total = "0" if capability == "dining" else str(result.get("total_inr") or "0")
                await service.run(
                    service.store.add_demo_order,
                    entry.owner,
                    DemoOrder(now, capability, ", ".join(fresh), summary, total),
                )
                entry.recorded_orders.update(fresh)
        except AccountsUnavailable:
            log_event("history", outcome="not_saved")

    async def resolve_extractor() -> IntentExtractor:
        if extractor_factory is not None:
            return extractor_factory()
        nonlocal default_extractor
        async with extractor_lock:
            if default_extractor is None:
                default_extractor = build_extractor(cast(Settings, application.state.settings))
            return default_extractor

    def spend_public_budget(runtime_settings: Settings) -> None:
        if runtime_settings.public_deployment and not budget.spend(
            runtime_settings.public_daily_turn_limit
        ):
            raise HTTPException(
                status_code=429,
                detail="This public demo has used its allowance for today. Please try tomorrow.",
            )

    personal_services: dict[int, PersonalService] = {}

    async def reminder_turn(
        request: Request, entry: BrowserSession, text: str
    ) -> tuple[str, list[Widget]] | None:
        """Handles "remind me ..." itself. Not stored in conversation history: it is private."""
        wording = reminder_voice.reminder_request(text)
        if wording is None:
            return None
        accounts = accounts_of(request)
        user_id = entry.owner
        if accounts is None or user_id is None:
            return reminder_voice.sign_in_text(entry.language), []
        personal = personal_services.setdefault(id(accounts), PersonalService(accounts))
        try:
            saved = await personal.create_reminder_from_text(user_id, wording)
            everything = await personal.list_reminders(user_id)
        except (AccountsUnavailable, PersonalError, WriteLimited):
            return reminder_voice.unavailable_text(entry.language), []
        log_event("personal", op="voice_reminder", outcome="saved")
        return (
            reminder_voice.reply_text(entry.language, saved),
            reminder_voice.reminders_ui(everything[:50]),
        )

    async def run_turn(
        request: Request,
        entry: BrowserSession,
        session_id: str,
        timer: TurnTimer,
        text: str,
    ) -> dict[str, object]:
        runtime_settings = entry.settings
        engine = cast(TaskEngine | None, request.app.state.tasks)
        if engine is not None:
            planned = await task_turn.handle(
                engine,
                entry.owner,
                entry.language,
                text,
                cast(PlanExtractor | None, request.app.state.plan_extractor),
                order_busy=entry.assistant.view().state == "READBACK",
            )
            if planned is not None:
                return await _response_payload(
                    entry,
                    None,
                    runtime_settings,
                    tts_factory,
                    session_id=session_id,
                    transcript=text,
                    timer=timer,
                    message_override=planned.message,
                    ui=planned.widgets,
                    plan=(
                        {"id": planned.plan_id, "active": planned.active}
                        if planned.plan_id
                        else None
                    ),
                    workflow=planned.workflow,
                    waiting_for=planned.waiting_for,
                )
        spoken_reminder = await reminder_turn(request, entry, text)
        if spoken_reminder is not None:
            message, widgets = spoken_reminder
            return await _response_payload(
                entry,
                None,
                runtime_settings,
                tts_factory,
                session_id=session_id,
                transcript=text,
                timer=timer,
                message_override=message,
                ui=widgets,
                workflow="reminders",
            )
        if (
            entry.workflow not in ("order", "none")
            and entry.assistant.view().state == "READBACK"
            and explicit_confirmation(text) is True
        ):
            # A bare "yes" while another workflow is on screen must not confirm a paused order.
            return await _response_payload(
                entry,
                None,
                runtime_settings,
                tts_factory,
                session_id=session_id,
                transcript=text,
                timer=timer,
                message_override=task_turn.stray_confirmation_reply(entry.language),
                workflow=entry.workflow,
                waiting_for=entry.waiting_for,
            )
        reply = await entry.assistant.turn(text, heard=_heard_readback(request, runtime_settings))
        result = await _response_payload(
            entry,
            reply,
            runtime_settings,
            tts_factory,
            session_id=session_id,
            transcript=text,
            timer=timer,
        )
        await remember(request, entry, text, result)
        return result

    @application.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> dict[str, object]:
        """For the host's health check. Says which mode this is and nothing else."""
        runtime_settings = _get_settings(request)
        return {
            "status": "ok",
            # What anyone gets without signing in. Mock unless this is one person's own machine.
            "commerce_mode": (
                "mock" if runtime_settings.multi_user_live else runtime_settings.commerce_backend
            ),
            "public_deployment": runtime_settings.public_deployment,
            "oauth_callback": (
                callback_url(runtime_settings) if runtime_settings.public_base_url else None
            ),
        }

    @application.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        """Can this server reach its database? Kept apart so a slow database can't restart it."""
        service = accounts_of(request)
        live = _get_settings(request).multi_user_live
        if service is None:
            return JSONResponse({"status": "ok", "database": "none", "multi_user_live": live})
        try:
            if not await service.ensure_ready():
                raise AccountsUnavailable(UNAVAILABLE_MESSAGE)
            await asyncio.to_thread(service.database.ping)
        except Exception:
            log_event("readiness", outcome="database_unreachable")
            # The demo still works; only accounts are waiting. /healthz stays green for that.
            return JSONResponse(
                status_code=503, content={"status": "database_unreachable", "demo": "ok"}
            )
        return JSONResponse(
            {"status": "ok", "database": service.database.dialect, "multi_user_live": live}
        )

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/privacy", include_in_schema=False)
    async def privacy(request: Request) -> HTMLResponse:
        contact = _get_settings(request).support_email or "the person who runs this site"
        page = (STATIC_DIR / "privacy.html").read_text(encoding="utf-8")
        return HTMLResponse(page.replace("{{contact}}", html.escape(contact)))

    async def live_for(request: Request) -> tuple[AccountService, User]:
        """The caller's own account, if they may use it live. Refused otherwise, never mocked."""
        service = accounts_of(request)
        if service is None:
            raise HTTPException(status_code=403, detail="Live Swiggy is not open on this site.")
        user = await service.current_user(request)
        refusal = await service.live_refusal(user)
        if refusal is not None or user is None:
            raise HTTPException(status_code=403, detail=refusal or "Sign in first.")
        return service, user

    def session_settings(runtime_settings: Settings) -> Settings:
        """Without a live request, a multi-user server gives its mock shops, labelled as such."""
        if runtime_settings.multi_user_live:
            return replace(
                runtime_settings,
                commerce_backend="mock",
                swiggy_live_access=False,
                allow_real_checkout=False,
                allow_swiggy_cart_writes=False,
                allow_real_booking=False,
            )
        return runtime_settings

    @application.get("/api/addresses")
    async def list_addresses(request: Request) -> dict[str, object]:
        wants_live = request.query_params.get("live") == "1"
        try:
            if wants_live:
                service, user = await live_for(request)
                effective = _get_settings(request)
                addresses = await service.adapters(user.id).grocery.get_addresses()
            else:
                effective = session_settings(_get_settings(request))
                addresses = await adapter_for(effective).get_addresses()
        except CommerceAuthRequired as exc:
            raise HTTPException(
                status_code=409, detail="Your Swiggy connection has ended. Please connect again."
            ) from exc
        except CommerceError as exc:
            if wants_live:
                raise HTTPException(
                    status_code=503, detail="Could not read your Swiggy addresses just now."
                ) from exc
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not read delivery addresses. Connect Swiggy with "
                    "`uv run python -m app.swiggy_auth` and retry."
                ),
            ) from exc
        return {
            "commerce_mode": effective.commerce_backend,
            "swiggy_environment": _swiggy_environment(effective),
            "public_deployment": effective.public_deployment,
            "checkout_enabled": (
                effective.commerce_backend == "mock" or effective.allow_real_checkout
            ),
            "addresses": [
                {"id": item.id, "label": item.label, "full_address": item.full_address}
                for item in addresses
            ],
        }

    @application.post("/api/sessions")
    async def create_session(payload: StartSessionRequest, request: Request) -> dict[str, object]:
        base_settings = _get_settings(request)
        user = await signed_in(request)
        service = accounts_of(request)
        live_user: User | None = None
        if payload.live:
            service, live_user = await live_for(request)
            runtime_settings = base_settings
        else:
            runtime_settings = session_settings(base_settings)
        # Anonymous conversations share a cap. A signed-in person has one conversation at a
        # time, which replaces their previous one, so they are bounded by the number of people.
        if user is None and runtime_settings.public_deployment:
            async with sessions_lock:
                _remove_expired_sessions(sessions, session_ttl_seconds)
                anonymous = sum(entry.owner is None for entry in sessions.values())
            if anonymous >= runtime_settings.public_max_sessions:
                raise HTTPException(status_code=429, detail="The demo is busy. Please try shortly.")
        spend_turn(runtime_settings, user.id if user else None)
        try:
            extractor = await resolve_extractor()
            if live_user is not None and service is not None:
                adapters = service.adapters(live_user.id)
                grocery: CommerceAdapter = adapters.grocery
                journal: OrderJournal = service.journal(live_user.id)
            else:
                adapters = None
                grocery = adapter_for(runtime_settings)
                journal = order_journal(runtime_settings)
            address = _select_address(await grocery.get_addresses(), payload.address_id)
            policy = checkout_policy(runtime_settings)

            async def build(domain: Domain) -> Workflow:
                if domain == "dining":
                    return DiningWorkflow(
                        adapters.dining if adapters else dining_for(runtime_settings),
                        journal,
                        policy,
                        today=date.today(),
                    )
                if domain == "food":
                    adapter = (
                        adapters.food(address.id)
                        if adapters
                        else food_for(runtime_settings, address.id)
                    )
                else:
                    adapter = grocery
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
            if live_user is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Your Swiggy connection has ended. Please connect again.",
                ) from exc
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
            settings=runtime_settings,
            owner=user.id if user else None,
        )
        async with sessions_lock:
            _remove_expired_sessions(sessions, session_ttl_seconds)
            sessions[session_id] = entry
            if user is not None:
                # One conversation per person, reachable from any of their devices. For live use
                # this also matters for safety: a Swiggy account has one cart to overwrite.
                earlier = current_conversation.get(user.id)
                if earlier is not None and earlier != session_id:
                    sessions.pop(earlier, None)
                current_conversation[user.id] = session_id
        if user is not None and service is not None:
            try:
                await service.run(service.store.set_language, user.id, payload.language)
            except AccountsUnavailable:
                log_event("preferences", outcome="not_saved")
        result = await _response_payload(
            entry,
            reply,
            runtime_settings,
            tts_factory,
            session_id=session_id,
            workflow="none",  # the greeting is not an order
        )
        await remember(request, entry, "", result)
        return result

    @application.get("/api/session/current")
    async def current_session(request: Request) -> dict[str, object]:
        """A signed-in person's conversation, so another tab or device can carry it on."""
        user = await signed_in(request)
        session_id = current_conversation.get(user.id) if user else None
        if session_id is None or session_id not in sessions:
            return {"session_id": None}
        return {"session_id": session_id}

    @application.get("/api/session")
    async def get_session(request: Request) -> dict[str, object]:
        session_id, entry = await owned_session(request)
        return _session_payload(session_id, entry, entry.settings)

    @application.post("/api/session/turn")
    async def upload_turn(request: Request) -> dict[str, object]:
        session_id, entry = await owned_session(request)
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
        runtime_settings = entry.settings
        spend_turn(runtime_settings, entry.owner)

        async with entry.lock:
            entry.last_activity = time.monotonic()
            timer = begin_turn()
            try:
                with tempfile.TemporaryDirectory(prefix="saathi-voice-") as temp_dir:
                    audio_path = Path(temp_dir) / "turn.wav"
                    audio_path.write_bytes(audio_bytes)
                    stt = (stt_factory or _default_stt_factory)(runtime_settings, entry.language)
                    with stage("stt"):
                        transcript = await stt.transcribe(audio_path)
                    return await run_turn(request, entry, session_id, timer, transcript.text)
            except GnaniError as exc:
                raise HTTPException(status_code=_gnani_status(exc), detail=str(exc)) from exc
            except (IntentExtractionError, CommerceError) as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Saathi could not safely process that turn. Please try again.",
                ) from exc

    @application.post("/api/session/text")
    async def typed_turn(payload: TextTurnRequest, request: Request) -> dict[str, object]:
        """The same turn as a spoken one, minus speech recognition. The same approvals apply."""
        session_id, entry = await owned_session(request)
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="Type what you would like.")
        if len(text) > MAX_TYPED_CHARACTERS:
            raise HTTPException(status_code=413, detail="Please keep it shorter.")
        spend_turn(entry.settings, entry.owner)
        async with entry.lock:
            entry.last_activity = time.monotonic()
            timer = begin_turn()
            try:
                return await run_turn(request, entry, session_id, timer, text)
            except (IntentExtractionError, CommerceError) as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Saathi could not safely process that turn. Please try again.",
                ) from exc

    @application.post("/api/session/action", response_model=None)
    async def session_action(
        payload: SessionActionRequest, request: Request
    ) -> dict[str, object] | JSONResponse:
        session_id, entry = await owned_session(request)
        runtime_settings = entry.settings
        spend_turn(runtime_settings, entry.owner)
        async with entry.lock:
            entry.last_activity = time.monotonic()
            begin_turn()
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
            elif payload.action == "resume":
                if payload.capability is None:
                    raise HTTPException(status_code=422, detail="Choose a task to go back to.")
                reply = await _run_action(entry.assistant.resume(payload.capability))
            else:
                reply = await _run_action(entry.assistant.cancel())
            result = await _response_payload(
                entry,
                reply,
                runtime_settings,
                tts_factory,
                session_id=session_id,
            )
            await remember(request, entry, f"[{payload.action}]", result)
            return result

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


def _cross_site(request: Request, settings: Settings | None) -> bool:
    """Browsers send Origin on cross-site writes. Requests with neither header carry no risk here,
    because SameSite=Lax keeps the cookie off a cross-site form post in any case."""
    if request.headers.get("sec-fetch-site", "") == "cross-site":
        return True
    origin = request.headers.get("origin")
    if not origin:
        return False
    if settings is not None and settings.public_base_url:
        return origin != settings.public_base_url.rstrip("/")
    return origin != f"{request.url.scheme}://{request.headers.get('host', '')}"


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


def _swiggy_environment(settings: Settings) -> str:
    if settings.commerce_backend != "swiggy":
        return ""
    return "staging" if "staging" in settings.swiggy_mcp_url else "production"


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
    in_order = entry.workflow in ("order", "none")
    return {
        "session_id": session_id,
        "workflow": {"kind": entry.workflow, "waiting_for": entry.waiting_for},
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
        # A paused order is kept but not offered while another workflow is on screen.
        "show_readback": in_order and view.state == "READBACK" and bool(view.lines),
        "can_confirm": in_order and checkout_enabled and view.confirmable,
        "options": [
            {
                "name": option.name,
                "pack_size": option.pack_size,
                "quantity": option.quantity,
                "unit_price_inr": _money_json(option.unit_price_inr),
            }
            for option in (view.options if in_order else ())
        ],
        "order_ids": list(view.order_ids),
        "order_status": view.order_status,
        "order_verified": view.order_verified,
        "order_partial": view.order_partial,
        "tasks": [
            {
                "capability": task.domain,
                "state": task.state,
                "item_count": task.item_count,
                "total_inr": _money_json(task.total_inr),
                "active": task.active,
                "open": task.open,
                "done": task.done,
            }
            for task in entry.assistant.tasks()
        ],
        "public_deployment": settings.public_deployment,
        "swiggy_environment": _swiggy_environment(settings),
        "last_reply": entry.last_reply,
        "tts_language": entry.language,
    }


def _share_widgets(entry: BrowserSession, settings: Settings) -> list[Widget]:
    """Share and calendar buttons once an order or booking is DONE. Nothing is sent by the app.

    A demo is labelled as one in the text itself, because the text travels beyond this screen.
    A live result is offered only when Swiggy's own history verified it.
    """
    view = entry.assistant.view()
    if view.state != "DONE":
        return []
    demo = settings.commerce_backend == "mock"
    if not demo and not view.order_verified:
        return []
    try:
        if view.domain == "dining":
            workflow = entry.assistant.workflows.get("dining")
            session = getattr(workflow, "session", None)
            if (
                session is None
                or session.restaurant is None
                or not session.date
                or not session.time
            ):
                return []
            start = datetime.fromisoformat(f"{session.date}T{session.time}").replace(tzinfo=IST)
            label = "Demo booking (not a real reservation)" if demo else "Table booking"
            summary = (
                f"{label}: {session.restaurant.name}, {session.date} at {session.time}, "
                f"{session.guests} guests."
            )
            calendar = CalendarInput(
                title=f"{'Demo: ' if demo else ''}Dinner at {session.restaurant.name}",
                start=start,
                end=start + timedelta(minutes=90),
                location=session.restaurant.area or None,
                description=summary,
            )
            return [share_panel_widget(summary, calendar)]
        items = ", ".join(f"{line.quantity} x {line.name}" for line in view.lines)
        label = "Demo order (not a real order)" if demo else "Swiggy order"
        total = _money_json(view.total_inr)
        return [share_panel_widget(f"{label}: {items}. Total Rs. {total}.")]
    except (GenUIError, ValueError, TypeError):
        return []


async def _response_payload(
    entry: BrowserSession,
    reply: Reply | None,
    settings: Settings,
    tts_factory: TTSFactory | None,
    *,
    session_id: str,
    transcript: str | None = None,
    timer: TurnTimer | None = None,
    message_override: str | None = None,
    ui: list[Widget] | None = None,
    plan: dict[str, object] | None = None,
    workflow: str | None = None,
    waiting_for: str | None = None,
) -> dict[str, object]:
    """`message_override` and `ui` serve turns the ordering assistant did not handle."""
    if workflow is not None:
        entry.workflow, entry.waiting_for = workflow, waiting_for
    elif reply is not None:
        entry.workflow, entry.waiting_for = "order", None  # the ordering assistant handled it
    if message_override is not None:
        message = message_override
    else:
        assert reply is not None
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
            with tempfile.TemporaryDirectory(prefix="saathi-reply-") as temp_dir:
                audio_path = (tts_factory or _default_tts_factory)(settings, entry.language)
                with stage("speech"):
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
    if plan is not None:
        result["plan"] = plan
    if ui is None and reply is not None:
        ui = _share_widgets(entry, settings)
    if ui:
        try:
            result["ui"] = to_wire(ui)
        except GenUIError:
            log_event("genui", outcome="rejected")  # the reply still goes out without the widget
    if timer is not None:
        timings = timer.report()
        log_event(
            "turn_timing",
            understood_by=timer.understood_by or "none",
            **{f"{name}_ms": value for name, value in timings.items()},
        )
        result["timings_ms"] = timings
        result["understood_by"] = timer.understood_by
        result["scripted_understanding"] = timer.understood_by == SCRIPTED
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
