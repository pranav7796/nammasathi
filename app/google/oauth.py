"""Google OAuth 2.0 for a web server application, one connection per person and service.

Contract (read 2026-11-07):
- https://developers.google.com/identity/protocols/oauth2/web-server  (authorization endpoint
  https://accounts.google.com/o/oauth2/v2/auth with access_type=offline, prompt=consent, state;
  token endpoint https://oauth2.googleapis.com/token for authorization_code and refresh_token
  grants; POST https://oauth2.googleapis.com/revoke with `token`; `invalid_grant` means the code or
  refresh token is no longer valid; a user who declines is sent back with error=access_denied.)
- PKCE (code_challenge / code_challenge_method=S256, code_verifier) is described at
  https://developers.google.com/identity/protocols/oauth2/native-app and RFC 7636. The web-server
  page does not mention it; Google accepts it for web clients, but that is NOT verified here.
- Scopes: https://developers.google.com/identity/protocols/oauth2/scopes (gmail.readonly,
  gmail.send, calendar.events). `openid email` only labels the account, from the id_token that the
  token endpoint returns over TLS.

Only the refresh token is stored, encrypted with AES-GCM bound to the user and the provider
("gmail" or "calendar"), so a row copied to another user cannot be opened. Access tokens live in
memory. Nothing secret, no code, no email address is logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode

import httpx

from app.accounts.crypto import TokenCipher, TokenDecryptError, parse_keys
from app.accounts.db import Database
from app.accounts.service import AccountService
from app.config import Settings
from app.integrations.base import IntegrationError, NotConnected, ReauthRequired
from app.observability import log_event

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALLBACK_PATH = "/api/google/callback"

_LABEL_SCOPES = ["openid", "email"]
SCOPES: dict[str, list[str]] = {
    "gmail": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "calendar": ["https://www.googleapis.com/auth/calendar.events"],
}
SERVICES = tuple(SCOPES)

STATE_SECONDS = 10 * 60
REFRESH_MARGIN_SECONDS = 60
_TIMEOUT_SECONDS = 10.0
_MAX_PENDING = 2000
RECONNECT_MESSAGE = "Your Google connection has ended. Please connect Google again."


class GoogleOAuthError(Exception):
    """Base for failures of the connect flow. Messages are safe to show; never provider text."""


class AccessDenied(GoogleOAuthError):
    """The person declined on Google's consent screen."""


class InvalidState(GoogleOAuthError):
    """Unknown, expired, reused or someone else's state."""


class ExchangeFailed(GoogleOAuthError):
    """Google would not turn the code into a connection."""


class UnknownService(ValueError):
    pass


@dataclass(frozen=True)
class Connection:
    service: str
    account_label: str


@dataclass(frozen=True)
class StoredConnection:
    ciphertext: str
    scopes: str
    account_label: str


@dataclass
class _Pending:
    user_id: str
    service: str
    verifier: str
    expires: float


@dataclass
class _Access:
    token: str
    expires: float


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}{CALLBACK_PATH}"


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# Database work, run in a worker thread through AccountService.run ------------------------------


def _read_row(database: Database, user_id: str, provider: str) -> StoredConnection | None:
    with database.transaction() as tx:
        rows = tx.execute(
            "SELECT ciphertext, scopes, account_label FROM connections "
            "WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
    if not rows:
        return None
    row = rows[0]
    return StoredConnection(str(row["ciphertext"]), str(row["scopes"]), str(row["account_label"]))


def _write_row(
    database: Database, user_id: str, provider: str, ciphertext: str, scopes: str, label: str
) -> None:
    now = int(time.time())
    with database.transaction() as tx:
        rows = tx.execute(
            "SELECT created_at FROM connections WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        created = int(str(rows[0]["created_at"])) if rows else now
        tx.execute(
            "DELETE FROM connections WHERE user_id = ? AND provider = ?", (user_id, provider)
        )
        tx.execute(
            "INSERT INTO connections "
            "(user_id, provider, ciphertext, scopes, account_label, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, provider, ciphertext, scopes, label, created, now),
        )


def _delete_row(database: Database, user_id: str, provider: str) -> None:
    with database.transaction() as tx:
        tx.execute(
            "DELETE FROM connections WHERE user_id = ? AND provider = ?", (user_id, provider)
        )


class ConnectionStore:
    """The `connections` table, through the account service's database."""

    def __init__(self, accounts: AccountService) -> None:
        self._accounts = accounts

    async def get(self, user_id: str, provider: str) -> StoredConnection | None:
        return await self._accounts.run(_read_row, self._accounts.database, user_id, provider)

    async def put(
        self, user_id: str, provider: str, ciphertext: str, scopes: str, label: str
    ) -> None:
        await self._accounts.run(
            _write_row, self._accounts.database, user_id, provider, ciphertext, scopes, label
        )

    async def delete(self, user_id: str, provider: str) -> None:
        await self._accounts.run(_delete_row, self._accounts.database, user_id, provider)


def _email_from_id_token(id_token: object) -> str:
    """The `email` claim. The token came straight from Google's token endpoint over TLS, so it is
    read for a label only and its signature is not needed. Any problem gives an empty label."""
    if not isinstance(id_token, str):
        return ""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        email = claims.get("email")
        return email if isinstance(email, str) else ""
    except (IndexError, ValueError):
        return ""


def _error_code(response: httpx.Response) -> str:
    try:
        body = cast(object, response.json())
    except ValueError:
        return ""
    code = cast(dict[str, object], body).get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) else ""


class GoogleTokens:
    """TokenProvider for Google: stored refresh token in, short-lived access token out."""

    def __init__(
        self,
        settings: Settings,
        store: ConnectionStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._cipher = TokenCipher(parse_keys(settings.token_encryption_keys or ""))
        self._transport = transport
        self._access: dict[tuple[str, str], _Access] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock(self, key: tuple[str, str]) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _issuer(provider: str) -> str:
        return f"google:{provider}"

    async def _post(self, url: str, data: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport) as http:
            return await http.post(url, data=data)

    async def save(
        self, user_id: str, service: str, refresh_token: str, scopes: str, label: str
    ) -> None:
        sealed = self._cipher.encrypt(
            json.dumps({"r": refresh_token}), user_id=user_id, issuer=self._issuer(service)
        )
        await self._store.put(user_id, service, sealed, scopes, label)

    def remember(self, user_id: str, provider: str, access: str, lifetime: float) -> None:
        self._access[(user_id, provider)] = _Access(access, time.time() + lifetime)

    async def _refresh_token(self, user_id: str, provider: str) -> str | None:
        stored = await self._store.get(user_id, provider)
        if stored is None:
            return None
        try:
            value = json.loads(
                self._cipher.decrypt(
                    stored.ciphertext, user_id=user_id, issuer=self._issuer(provider)
                )
            )
            refresh = value["r"]
            if not isinstance(refresh, str) or not refresh:
                raise ValueError
        except (TokenDecryptError, ValueError, KeyError, TypeError):
            # Fail closed: an unreadable record is never used and never guessed at.
            log_event("google", op="token_read", outcome="undecryptable")
            await self._store.delete(user_id, provider)
            raise ReauthRequired(RECONNECT_MESSAGE) from None
        return refresh

    async def access_token(self, user_id: str, provider: str) -> str:
        return await self._access_token(user_id, provider, rejected="")

    async def invalidate(self, user_id: str, provider: str, rejected: str) -> str:
        """Google refused `rejected`. Return a fresh token (refreshing if it is still cached)."""
        return await self._access_token(user_id, provider, rejected=rejected)

    async def _access_token(self, user_id: str, provider: str, *, rejected: str) -> str:
        if provider not in SCOPES:
            raise NotConnected("That service is not available.")
        key = (user_id, provider)
        async with self._lock(key):
            cached = self._access.get(key)
            if (
                cached is not None
                and cached.token != rejected
                and cached.expires - time.time() > REFRESH_MARGIN_SECONDS
            ):
                return cached.token
            refresh = await self._refresh_token(user_id, provider)
            if refresh is None:
                self._access.pop(key, None)
                raise NotConnected("Google is not connected.")
            return await self._refresh(user_id, provider, refresh)

    async def _refresh(self, user_id: str, provider: str, refresh: str) -> str:
        key = (user_id, provider)
        try:
            response = await self._post(
                TOKEN_URL,
                {
                    "client_id": self._settings.google_client_id,
                    "client_secret": self._settings.google_client_secret or "",
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError:
            log_event("google", op="refresh", outcome="unreachable")
            raise IntegrationError("Google sign-in could not be reached", retryable=True) from None
        if response.status_code >= 500:
            log_event("google", op="refresh", outcome="server_error")
            raise IntegrationError("Google sign-in is having trouble", retryable=True)
        if response.status_code != 200:
            if _error_code(response) == "invalid_grant":
                log_event("google", op="refresh", outcome="invalid_grant")
                self._access.pop(key, None)
                await self._store.delete(user_id, provider)
                raise ReauthRequired(RECONNECT_MESSAGE)
            log_event("google", op="refresh", outcome="refused")
            raise IntegrationError("Google refused the sign-in request")
        try:
            body = cast(dict[str, object], response.json())
            access = body["access_token"]
            if not isinstance(access, str) or not access:
                raise ValueError
            lifetime = body.get("expires_in")
        except (ValueError, KeyError, TypeError):
            log_event("google", op="refresh", outcome="unreadable")
            raise IntegrationError("Google sign-in sent an unreadable reply") from None
        self._access[key] = _Access(
            access, time.time() + (float(lifetime) if isinstance(lifetime, (int, float)) else 3600)
        )
        rotated = body.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != refresh:
            stored = await self._store.get(user_id, provider)
            if stored is not None:
                await self.save(user_id, provider, rotated, stored.scopes, stored.account_label)
        log_event("google", op="refresh", outcome="ok")
        return access

    async def disconnect(self, user_id: str, service: str) -> None:
        """Revoke at Google if we can, and always delete the stored credential."""
        key = (user_id, service)
        async with self._lock(key):
            refresh: str | None = None
            try:
                refresh = await self._refresh_token(user_id, service)
            except ReauthRequired:
                refresh = None
            self._access.pop(key, None)
            await self._store.delete(user_id, service)
        if refresh is None:
            return
        try:
            response = await self._post(REVOKE_URL, {"token": refresh})
            log_event(
                "google", op="revoke", outcome="ok" if response.status_code == 200 else "refused"
            )
        except httpx.HTTPError:
            log_event("google", op="revoke", outcome="failed")

    async def status(self, user_id: str) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for service in SERVICES:
            stored = await self._store.get(user_id, service)
            result[service] = {
                "connected": stored is not None,
                "account": (stored.account_label or None) if stored else None,
            }
        return result


class GoogleOAuth:
    """Starts and finishes the consent flow for one service (a name in SCOPES)."""

    def __init__(
        self,
        settings: Settings,
        store: ConnectionStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        tokens: GoogleTokens | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self.tokens = tokens or GoogleTokens(settings, store, transport=transport)
        self._pending: dict[str, _Pending] = {}

    @property
    def configured(self) -> bool:
        return self._settings.google_configured

    def build_authorization_url(self, user_id: str, service: str) -> str:
        if service not in SCOPES:
            raise UnknownService(service)
        now = time.time()
        for stale in [s for s, p in self._pending.items() if p.expires <= now]:
            del self._pending[stale]
        if len(self._pending) >= _MAX_PENDING:
            self._pending.clear()  # bounded memory; those people simply start again
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        self._pending[state] = _Pending(user_id, service, verifier, now + STATE_SECONDS)
        query = {
            "client_id": self._settings.google_client_id,
            "redirect_uri": redirect_uri(self._settings),
            "response_type": "code",
            "scope": " ".join([*_LABEL_SCOPES, *SCOPES[service]]),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(query)}"

    def _take(self, user_id: str, state: str) -> _Pending:
        """Single use: the state is consumed whether or not it turns out to be valid."""
        pending = self._pending.pop(state, None)
        if pending is None or pending.expires <= time.time() or pending.user_id != user_id:
            raise InvalidState("This sign-in link is not valid any more. Please start again.")
        return pending

    def service_for_state(self, user_id: str, state: str) -> str | None:
        """Which service a pending sign-in is for, if the state is this person's and still open."""
        pending = self._pending.get(state)
        return pending.service if pending is not None and pending.user_id == user_id else None

    async def handle_callback(
        self, user_id: str, code: str, state: str, error: str | None = None
    ) -> Connection:
        pending = self._take(user_id, state)
        if error:
            outcome = "denied" if error == "access_denied" else "error"
            log_event("google", op="callback", outcome=outcome)
            if error == "access_denied":
                raise AccessDenied("Google was not connected.")
            raise ExchangeFailed("Google could not connect.")
        if not code:
            raise ExchangeFailed("Google could not connect.")
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as http:
                response = await http.post(
                    TOKEN_URL,
                    data={
                        "client_id": self._settings.google_client_id,
                        "client_secret": self._settings.google_client_secret or "",
                        "code": code,
                        "code_verifier": pending.verifier,
                        "grant_type": "authorization_code",
                        "redirect_uri": redirect_uri(self._settings),
                    },
                )
        except httpx.HTTPError:
            log_event("google", op="exchange", outcome="unreachable")
            raise ExchangeFailed("Google could not be reached.") from None
        if response.status_code != 200:
            log_event("google", op="exchange", outcome="refused")
            raise ExchangeFailed("Google would not complete the connection.")
        try:
            body = cast(dict[str, object], response.json())
            refresh = body.get("refresh_token")
            access = body.get("access_token")
            granted = str(body.get("scope", "")).split()
        except (ValueError, TypeError):
            raise ExchangeFailed("Google sent an unreadable reply.") from None
        if not isinstance(refresh, str) or not refresh or not isinstance(access, str):
            log_event("google", op="exchange", outcome="no_refresh_token")
            raise ExchangeFailed("Google did not allow lasting access.")
        if not set(SCOPES[pending.service]) <= set(granted):
            # Granular consent: the person unticked something we need. Give the access back.
            log_event("google", op="exchange", outcome="missing_scope")
            await self._revoke_quietly(refresh)
            raise ExchangeFailed("Not every permission was allowed.")
        label = _email_from_id_token(body.get("id_token"))
        await self.tokens.save(user_id, pending.service, refresh, " ".join(granted), label)
        lifetime = body.get("expires_in")
        self.tokens.remember(
            user_id,
            pending.service,
            access,
            float(lifetime) if isinstance(lifetime, (int, float)) else 3600.0,
        )
        log_event("google", op="exchange", outcome="connected")
        return Connection(pending.service, label)

    async def _revoke_quietly(self, token: str) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as http:
                await http.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError:
            pass

    async def disconnect(self, user_id: str, service: str) -> None:
        await self.tokens.disconnect(user_id, service)

    async def status(self, user_id: str) -> dict[str, dict[str, object]]:
        return await self.tokens.status(user_id)
