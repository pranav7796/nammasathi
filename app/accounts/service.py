"""What the web layer needs from accounts, wired together at startup."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import httpx2
from fastapi import Request

from app.accounts.crypto import (
    DUMMY_PASSWORD_HASH,
    TokenCipher,
    hash_password,
    parse_keys,
    user_hash,
    verify_password,
)
from app.accounts.db import Database, connect, migrate
from app.accounts.journal import DatabaseOrderJournal
from app.accounts.mailer import Mailer, build_mailer
from app.accounts.store import AccountStore, User
from app.accounts.swiggy_tokens import DelegatedAuth, SwiggyTokens
from app.commerce.base import CommerceAdapter
from app.commerce.swiggy import SWIGGY_MCP_URL, SwiggyInstamart, food_or_dining_url
from app.config import Settings
from app.dining.swiggy import SwiggyDineout
from app.food.swiggy import SwiggyFood

COOKIE_SECURE = "__Host-saathi_session"
COOKIE_LOCAL = "saathi_session"
MAX_FAILURES_PER_ACCOUNT = 10
MAX_FAILURES_PER_ADDRESS = 50
FAILURE_WINDOW_SECONDS = 15 * 60
_MAX_LIVE_USERS = 500
_MAX_TRACKED_FAILURES = 20_000


@dataclass(frozen=True)
class SwiggyStub:
    """Points the real adapters at a stand-in. Only a test or dev harness can create one; no
    setting or environment variable can, so a deployment cannot be pointed at it by accident."""

    oauth_transport: httpx.AsyncBaseTransport
    mcp_transport: httpx2.AsyncBaseTransport
    browser_base: str  # where a browser is sent to sign in


def issuer_of(settings: Settings) -> str:
    parts = urlsplit(settings.swiggy_mcp_url or SWIGGY_MCP_URL)
    return f"{parts.scheme}://{parts.netloc}"


def environment_name(settings: Settings) -> str:
    return "staging" if "staging" in settings.swiggy_mcp_url else "production"


class LoginLimiter:
    """Slows password guessing. In memory, so it resets on restart, and only bounds one server."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - FAILURE_WINDOW_SECONDS
        kept = [moment for moment in self._failures.get(key, []) if moment > cutoff]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)
        return kept

    def blocked(self, email: str, address: str) -> bool:
        return (
            len(self._recent(f"e:{email}")) >= MAX_FAILURES_PER_ACCOUNT
            or len(self._recent(f"a:{address}")) >= MAX_FAILURES_PER_ADDRESS
        )

    def record_failure(self, email: str, address: str) -> None:
        if len(self._failures) > _MAX_TRACKED_FAILURES:
            self._failures.clear()
        now = time.monotonic()
        self._failures.setdefault(f"e:{email}", []).append(now)
        self._failures.setdefault(f"a:{address}", []).append(now)

    def clear(self, email: str) -> None:
        self._failures.pop(f"e:{email}", None)


class LiveAdapters:
    """One user's Swiggy adapters: their token, their carts, their MCP sessions."""

    def __init__(
        self, settings: Settings, tokens: SwiggyTokens, user_id: str, stub: SwiggyStub | None
    ) -> None:
        self._settings = settings
        self._user_id = user_id
        self._tokens = tokens
        self._transport = stub.mcp_transport if stub else None
        self._user_hash = user_hash(user_id)
        self._grocery: SwiggyInstamart | None = None
        self._food: dict[str, SwiggyFood] = {}
        self._dining: SwiggyDineout | None = None

    def _auth(self) -> DelegatedAuth:
        return DelegatedAuth(self._tokens, self._user_id)

    @property
    def grocery(self) -> SwiggyInstamart:
        if self._grocery is None:
            self._grocery = SwiggyInstamart(
                endpoint=self._settings.swiggy_mcp_url,
                delegated_auth=self._auth(),
                http_transport=self._transport,
                user_label=self._user_hash,
                allow_real_checkout=self._settings.allow_real_checkout,
                allow_cart_writes=self._settings.allow_swiggy_cart_writes,
            )
        return self._grocery

    def food(self, address_id: str) -> CommerceAdapter:
        if address_id not in self._food:
            self._food[address_id] = SwiggyFood(
                address_id=address_id,
                endpoint=food_or_dining_url(self._settings.swiggy_mcp_url, "food"),
                delegated_auth=self._auth(),
                http_transport=self._transport,
                user_label=self._user_hash,
                allow_real_checkout=self._settings.allow_real_checkout,
                allow_cart_writes=self._settings.allow_swiggy_cart_writes,
            )
        return self._food[address_id]

    @property
    def dining(self) -> SwiggyDineout:
        if self._dining is None:
            self._dining = SwiggyDineout(
                endpoint=food_or_dining_url(self._settings.swiggy_mcp_url, "dineout"),
                delegated_auth=self._auth(),
                http_transport=self._transport,
                user_label=self._user_hash,
                allow_real_checkout=self._settings.allow_real_booking,
            )
        return self._dining

    async def aclose(self) -> None:
        clients = [self._grocery, self._dining, *self._food.values()]
        for client in clients:
            if client is not None:
                await client.aclose()


class AccountService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        mailer: Mailer | None = None,
        stub: SwiggyStub | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.store = AccountStore(database)
        self.mailer = mailer or build_mailer(settings)
        self.stub = stub
        self.limiter = LoginLimiter()
        self.issuer = issuer_of(settings)
        self.tokens = SwiggyTokens(
            self.store,
            TokenCipher(parse_keys(settings.token_encryption_keys or "")),
            self.issuer,
            transport=stub.oauth_transport if stub else None,
        )
        self._live: OrderedDict[str, LiveAdapters] = OrderedDict()
        self._closing: set[asyncio.Future[None]] = set()
        # Each hash needs about 32 MB, so a small server allows only a few at once.
        self._hashing = asyncio.Semaphore(4)

    @classmethod
    def start(
        cls, settings: Settings, *, mailer: Mailer | None = None, stub: SwiggyStub | None = None
    ) -> AccountService:
        database = connect(settings.database_url or "")
        migrate(database)
        return cls(settings, database, mailer=mailer, stub=stub)

    @property
    def secure_cookies(self) -> bool:
        return self.settings.public_base_url.startswith("https://")

    @property
    def cookie_name(self) -> str:
        return COOKIE_SECURE if self.secure_cookies else COOKIE_LOCAL

    async def hash_password(self, password: str) -> str:
        async with self._hashing:
            return await asyncio.to_thread(hash_password, password)

    async def check_password(self, password: str, stored: str | None) -> bool:
        async with self._hashing:
            matched = await asyncio.to_thread(
                verify_password, password, stored or DUMMY_PASSWORD_HASH
            )
        return matched and stored is not None

    async def current_user(self, request: Request) -> User | None:
        token = request.cookies.get(self.cookie_name)
        if not token:
            return None
        return await asyncio.to_thread(self.store.session_user, token)

    def journal(self, user_id: str) -> DatabaseOrderJournal:
        return DatabaseOrderJournal(self.database, user_id)

    def adapters(self, user_id: str) -> LiveAdapters:
        adapters = self._live.get(user_id)
        if adapters is None:
            adapters = LiveAdapters(self.settings, self.tokens, user_id, self.stub)
            self._live[user_id] = adapters
            if len(self._live) > _MAX_LIVE_USERS:
                _, oldest = self._live.popitem(last=False)
                closing = asyncio.ensure_future(oldest.aclose())
                self._closing.add(closing)
                closing.add_done_callback(self._closing.discard)
        self._live.move_to_end(user_id)
        return adapters

    async def drop_adapters(self, user_id: str) -> None:
        adapters = self._live.pop(user_id, None)
        if adapters is not None:
            await adapters.aclose()

    async def live_refusal(self, user: User | None) -> str | None:
        """Why this person cannot use their real Swiggy account right now, or None if they can."""
        if not self.settings.multi_user_live:
            return "Live Swiggy is not open yet."
        if user is None:
            return "Sign in first."
        if not user.email_verified:
            return "Verify your email first."
        if not user.live_access:
            return "Live Swiggy is invite-only for now."
        if not await self.tokens.connected(user.id):
            return "Connect your Swiggy account first."
        return None

    async def close(self) -> None:
        for adapters in self._live.values():
            await adapters.aclose()
        self._live.clear()
        await asyncio.to_thread(self.database.close)
