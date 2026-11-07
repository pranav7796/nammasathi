"""Fail-closed runtime configuration for commerce backends."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from app.accounts.crypto import parse_keys
from app.commerce.swiggy import ALLOWED_MCP_URLS, SWIGGY_MCP_URL

GROQ_DEFAULT_MODEL = "openai/gpt-oss-20b"
OPENROUTER_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
DEFAULT_ORDER_JOURNAL_PATH = "var/order_attempts.jsonl"


class ConfigurationError(ValueError):
    """Raised when commerce configuration is unsafe or invalid."""


@dataclass(frozen=True)
class Settings:
    commerce_backend: str = "mock"
    swiggy_live_access: bool = False
    allow_real_checkout: bool = False
    allow_swiggy_cart_writes: bool = False
    allow_real_booking: bool = False
    max_real_order_inr: Decimal | None = None
    real_order_limit: int = 1
    order_journal_path: str = DEFAULT_ORDER_JOURNAL_PATH
    swiggy_mcp_url: str = SWIGGY_MCP_URL
    gemini_api_key: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-3.8-flash"
    gemini_timeout_seconds: int = 60
    gemini_max_fallback_models: int = 2
    groq_api_key: str | None = field(default=None, repr=False)
    groq_model: str = GROQ_DEFAULT_MODEL
    openrouter_api_key: str | None = field(default=None, repr=False)
    openrouter_model: str = OPENROUTER_DEFAULT_MODEL
    gnani_api_key: str | None = field(default=None, repr=False)
    gnani_tts_model: str = "timbre-v2.5"
    gnani_tts_voice: str | None = None  # None selects each language's preferred voice
    gnani_tts_speed: float = 0.90
    gnani_timeout_seconds: int = 20
    llm_primary: str = "groq"
    llm_timeout_seconds: int = 10
    # llm, fallback (scripted after every LLM fails), or scripted. Mock mode only.
    demo_understanding: str = "llm"
    public_deployment: bool = False
    public_base_url: str = ""
    public_daily_turn_limit: int = 300
    public_max_sessions: int = 50
    # Accounts exist when a database is configured. See docs/production-activation.md.
    database_url: str | None = field(default=None, repr=False)
    token_encryption_keys: str | None = field(default=None, repr=False)
    open_registration: bool = True
    # Real Swiggy for signed-in, approved users on a public server. Everyone else gets mock shops.
    multi_user_live: bool = False
    real_order_daily_limit: int | None = None
    user_daily_turn_limit: int = 200
    global_daily_turn_limit: int = 5000
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str | None = field(default=None, repr=False)
    smtp_from: str = ""
    support_email: str = ""
    # Gmail and Google Calendar (OAuth, "Testing" mode is free) and WhatsApp Cloud API. Empty means
    # not configured: those features then offer a labelled demo, never a pretend success.
    google_client_id: str = ""
    google_client_secret: str | None = field(default=None, repr=False)
    whatsapp_access_token: str | None = field(default=None, repr=False)
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str | None = field(default=None, repr=False)
    whatsapp_verify_token: str | None = field(default=None, repr=False)
    whatsapp_graph_version: str = "v21.0"
    # Twilio's WhatsApp (sandbox or a Twilio number): inbound messages arrive as signed webhooks.
    twilio_account_sid: str = ""
    twilio_auth_token: str | None = field(default=None, repr=False)
    twilio_whatsapp_from: str = ""  # the number people message, e.g. whatsapp:+14155238886

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.public_base_url)

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_auth_token and self.public_base_url)

    @property
    def whatsapp_configured(self) -> bool:
        return bool(
            self.whatsapp_access_token
            and self.whatsapp_phone_number_id
            and self.whatsapp_app_secret
            and self.whatsapp_verify_token
        )

    @property
    def accounts_enabled(self) -> bool:
        return bool(self.database_url)

    def require_real_checkout(self) -> None:
        """Refuse an actual checkout unless explicitly enabled for Swiggy."""
        if self.commerce_backend != "swiggy" or not self.allow_real_checkout:
            raise ConfigurationError(
                "Real checkout requires COMMERCE_BACKEND=swiggy and ALLOW_REAL_CHECKOUT=true"
            )


def _flag(values: Mapping[str, str], name: str) -> bool:
    raw = values.get(name, "false").strip().lower()
    if raw not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")
    return raw == "true"


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load settings without ever defaulting into real checkout."""
    values = os.environ if environ is None else environ
    backend = values.get("COMMERCE_BACKEND", "mock").strip().lower()
    if backend not in {"mock", "swiggy"}:
        raise ConfigurationError("COMMERCE_BACKEND must be 'mock' or 'swiggy'")

    swiggy_live_access = _flag(values, "SWIGGY_LIVE_ACCESS")
    if swiggy_live_access and backend != "swiggy":
        raise ConfigurationError("SWIGGY_LIVE_ACCESS=true requires COMMERCE_BACKEND=swiggy")

    allow_cart_writes = _flag(values, "ALLOW_SWIGGY_CART_WRITES")
    if allow_cart_writes and not swiggy_live_access:
        raise ConfigurationError("ALLOW_SWIGGY_CART_WRITES=true requires SWIGGY_LIVE_ACCESS=true")

    allow_real_booking = _flag(values, "ALLOW_REAL_BOOKING")
    if allow_real_booking and not swiggy_live_access:
        raise ConfigurationError("ALLOW_REAL_BOOKING=true requires SWIGGY_LIVE_ACCESS=true")

    allow_real_checkout = _flag(values, "ALLOW_REAL_CHECKOUT")
    if allow_real_checkout and backend != "swiggy":
        raise ConfigurationError("ALLOW_REAL_CHECKOUT=true requires COMMERCE_BACKEND=swiggy")

    max_real_order = _positive_money(values.get("MAX_REAL_ORDER_INR", ""))
    if allow_real_checkout and not (allow_cart_writes and max_real_order is not None):
        raise ConfigurationError(
            "ALLOW_REAL_CHECKOUT=true requires ALLOW_SWIGGY_CART_WRITES=true "
            "and a positive MAX_REAL_ORDER_INR"
        )
    try:
        real_order_limit = int(values.get("REAL_ORDER_LIMIT", "1").strip())
    except ValueError as exc:
        raise ConfigurationError("REAL_ORDER_LIMIT must be a positive integer") from exc
    if real_order_limit <= 0:
        raise ConfigurationError("REAL_ORDER_LIMIT must be a positive integer")
    journal_path = values.get("ORDER_JOURNAL_PATH", DEFAULT_ORDER_JOURNAL_PATH).strip()
    if not journal_path:
        raise ConfigurationError("ORDER_JOURNAL_PATH must not be empty")

    endpoint = values.get("SWIGGY_MCP_URL", SWIGGY_MCP_URL).strip()
    if backend == "swiggy" and endpoint not in ALLOWED_MCP_URLS:
        raise ConfigurationError(f"SWIGGY_MCP_URL must be {SWIGGY_MCP_URL} or the staging server")
    model = values.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
    if not model:
        raise ConfigurationError("GEMINI_MODEL must not be empty")
    raw_timeout = values.get("GEMINI_TIMEOUT_SECONDS", "60").strip()
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError as exc:
        raise ConfigurationError("GEMINI_TIMEOUT_SECONDS must be a positive integer") from exc
    if timeout_seconds <= 0:
        raise ConfigurationError("GEMINI_TIMEOUT_SECONDS must be a positive integer")
    raw_fallbacks = values.get("GEMINI_MAX_FALLBACK_MODELS", "2").strip()
    try:
        max_fallback_models = int(raw_fallbacks)
    except ValueError as exc:
        raise ConfigurationError("GEMINI_MAX_FALLBACK_MODELS must be non-negative") from exc
    if max_fallback_models < 0:
        raise ConfigurationError("GEMINI_MAX_FALLBACK_MODELS must be non-negative")
    groq_model = values.get("GROQ_MODEL", GROQ_DEFAULT_MODEL).strip()
    if not groq_model:
        raise ConfigurationError("GROQ_MODEL must not be empty")
    openrouter_model = values.get("OPENROUTER_MODEL", OPENROUTER_DEFAULT_MODEL).strip()
    if not openrouter_model:
        raise ConfigurationError("OPENROUTER_MODEL must not be empty")
    tts_model = values.get("GNANI_TTS_MODEL", "timbre-v2.5").strip()
    if tts_model != "timbre-v2.5":
        raise ConfigurationError("GNANI_TTS_MODEL must be timbre-v2.5")
    try:
        tts_speed = float(values.get("GNANI_TTS_SPEED", "0.90"))
    except ValueError as exc:
        raise ConfigurationError("GNANI_TTS_SPEED must be between 0.85 and 1.15") from exc
    if not 0.85 <= tts_speed <= 1.15:
        raise ConfigurationError("GNANI_TTS_SPEED must be between 0.85 and 1.15")
    raw_gnani_timeout = values.get("GNANI_TIMEOUT_SECONDS", "20").strip()
    try:
        gnani_timeout = int(raw_gnani_timeout)
    except ValueError as exc:
        raise ConfigurationError("GNANI_TIMEOUT_SECONDS must be positive") from exc
    if gnani_timeout <= 0:
        raise ConfigurationError("GNANI_TIMEOUT_SECONDS must be positive")
    llm_primary = values.get("LLM_PRIMARY", "groq").strip().lower()
    if llm_primary not in {"groq", "gemini"}:
        raise ConfigurationError("LLM_PRIMARY must be groq or gemini")
    llm_timeout = _positive_int(values, "LLM_TIMEOUT_SECONDS", 10)
    demo_understanding = values.get("DEMO_UNDERSTANDING", "llm").strip().lower()
    if demo_understanding not in {"llm", "fallback", "scripted"}:
        raise ConfigurationError("DEMO_UNDERSTANDING must be llm, fallback, or scripted")
    if demo_understanding != "llm" and backend != "mock":
        raise ConfigurationError("Scripted understanding is only allowed with the mock backend")
    public_deployment = _flag(values, "PUBLIC_DEPLOYMENT")
    # Render sets RENDER_EXTERNAL_URL to this service's own https:// address.
    public_base_url = (
        values.get("PUBLIC_BASE_URL", "").strip() or values.get("RENDER_EXTERNAL_URL", "").strip()
    )
    database_url = values.get("DATABASE_URL", "").strip() or None
    encryption_keys = values.get("TOKEN_ENCRYPTION_KEYS", "").strip() or None
    multi_user_live = _flag(values, "MULTI_USER_LIVE")
    _check_accounts(database_url, encryption_keys)
    if multi_user_live:
        _check_multi_user_live(
            backend, swiggy_live_access, database_url, public_base_url, public_deployment
        )
    if public_deployment and not multi_user_live:
        _check_public_deployment(
            backend,
            public_base_url,
            live_flags=(
                swiggy_live_access,
                allow_cart_writes,
                allow_real_booking,
                allow_real_checkout,
            ),
        )
    raw_daily = values.get("REAL_ORDER_DAILY_LIMIT", "").strip()
    smtp_host = values.get("SMTP_HOST", "").strip()
    smtp_from = values.get("SMTP_FROM", "").strip()
    if smtp_host and not smtp_from:
        raise ConfigurationError("SMTP_HOST requires SMTP_FROM")
    return Settings(
        database_url=database_url,
        token_encryption_keys=encryption_keys,
        open_registration=values.get("OPEN_REGISTRATION", "true").strip().lower() != "false",
        multi_user_live=multi_user_live,
        real_order_daily_limit=(
            _positive_int(values, "REAL_ORDER_DAILY_LIMIT", 1) if raw_daily else None
        ),
        user_daily_turn_limit=_positive_int(values, "USER_DAILY_TURN_LIMIT", 200),
        global_daily_turn_limit=_positive_int(values, "GLOBAL_DAILY_TURN_LIMIT", 5000),
        smtp_host=smtp_host,
        smtp_port=_positive_int(values, "SMTP_PORT", 587),
        smtp_username=values.get("SMTP_USERNAME", "").strip(),
        smtp_password=values.get("SMTP_PASSWORD") or None,
        smtp_from=smtp_from,
        support_email=values.get("SUPPORT_EMAIL", "").strip(),
        google_client_id=values.get("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=values.get("GOOGLE_CLIENT_SECRET") or None,
        whatsapp_access_token=values.get("WHATSAPP_ACCESS_TOKEN") or None,
        whatsapp_phone_number_id=values.get("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
        whatsapp_app_secret=values.get("WHATSAPP_APP_SECRET") or None,
        whatsapp_verify_token=values.get("WHATSAPP_VERIFY_TOKEN") or None,
        whatsapp_graph_version=values.get("WHATSAPP_GRAPH_VERSION", "v21.0").strip() or "v21.0",
        twilio_account_sid=values.get("TWILIO_ACCOUNT_SID", "").strip(),
        twilio_auth_token=values.get("TWILIO_AUTH_TOKEN") or None,
        twilio_whatsapp_from=values.get("TWILIO_WHATSAPP_FROM", "").strip(),
        llm_primary=llm_primary,
        llm_timeout_seconds=llm_timeout,
        demo_understanding=demo_understanding,
        public_deployment=public_deployment,
        public_base_url=public_base_url,
        public_daily_turn_limit=_positive_int(values, "PUBLIC_DAILY_TURN_LIMIT", 300),
        public_max_sessions=_positive_int(values, "PUBLIC_MAX_SESSIONS", 50),
        commerce_backend=backend,
        swiggy_live_access=swiggy_live_access,
        allow_real_checkout=allow_real_checkout,
        allow_swiggy_cart_writes=allow_cart_writes,
        allow_real_booking=allow_real_booking,
        max_real_order_inr=max_real_order,
        real_order_limit=real_order_limit,
        order_journal_path=journal_path,
        swiggy_mcp_url=endpoint,
        gemini_api_key=values.get("GEMINI_API_KEY") or None,
        gemini_model=model,
        gemini_timeout_seconds=timeout_seconds,
        gemini_max_fallback_models=max_fallback_models,
        groq_api_key=values.get("GROQ_API_KEY") or None,
        groq_model=groq_model,
        openrouter_api_key=values.get("OPENROUTER_API_KEY") or None,
        openrouter_model=openrouter_model,
        gnani_api_key=values.get("GNANI_API_KEY") or None,
        gnani_tts_model=tts_model,
        gnani_tts_voice=values.get("GNANI_TTS_VOICE", "").strip() or None,
        gnani_tts_speed=tts_speed,
        gnani_timeout_seconds=gnani_timeout,
    )


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(values.get(name, str(default)).strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _check_accounts(database_url: str | None, encryption_keys: str | None) -> None:
    if not database_url:
        return
    if not database_url.startswith(("postgresql://", "postgres://", "sqlite:///")):
        raise ConfigurationError("DATABASE_URL must be postgresql://... or sqlite:///path")
    if not encryption_keys:
        raise ConfigurationError("DATABASE_URL requires TOKEN_ENCRYPTION_KEYS")
    try:
        parse_keys(encryption_keys)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def _check_multi_user_live(
    backend: str,
    live_access: bool,
    database_url: str | None,
    base_url: str,
    public: bool,
) -> None:
    """Real accounts on a shared server need per-user storage and a durable order journal."""
    if backend != "swiggy" or not live_access:
        raise ConfigurationError(
            "MULTI_USER_LIVE=true requires COMMERCE_BACKEND=swiggy and SWIGGY_LIVE_ACCESS=true"
        )
    if not database_url:
        raise ConfigurationError("MULTI_USER_LIVE=true requires DATABASE_URL")
    if not base_url:
        raise ConfigurationError("MULTI_USER_LIVE=true requires PUBLIC_BASE_URL")
    if public:
        if database_url.startswith("sqlite"):
            # The order journal must survive a redeploy, and a host's local disk may not.
            raise ConfigurationError("A public server needs a Postgres DATABASE_URL")
        host = urlsplit(database_url).hostname or ""
        if (
            "sslmode=require" not in database_url
            and "sslmode=verify" not in database_url
            and (host not in {"localhost", "127.0.0.1"})
        ):
            raise ConfigurationError("DATABASE_URL must set sslmode=require or stronger")
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.path not in {"", "/"}:
            raise ConfigurationError("PUBLIC_BASE_URL must be an https:// origin with no path")


def _check_public_deployment(backend: str, base_url: str, *, live_flags: tuple[bool, ...]) -> None:
    """Without MULTI_USER_LIVE, a public server has one shared login, so it only runs the mock."""
    if backend != "mock" or any(live_flags):
        raise ConfigurationError("PUBLIC_DEPLOYMENT=true only runs with the mock backend")
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.path not in {"", "/"}:
        raise ConfigurationError("PUBLIC_BASE_URL must be an https:// origin with no path")


def _positive_money(raw: str) -> Decimal | None:
    if not raw.strip():
        return None
    try:
        amount = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ConfigurationError("MAX_REAL_ORDER_INR must be a positive number") from exc
    if not amount.is_finite() or amount <= 0:
        raise ConfigurationError("MAX_REAL_ORDER_INR must be a positive number")
    return amount
