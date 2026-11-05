"""Each user's Swiggy tokens: stored encrypted, refreshed in time, never shared.

Contract: https://mcp.swiggy.com/builders/docs/start/enterprise/delegated-auth.md and
https://mcp.swiggy.com/builders/docs/start/authenticate.md. Refresh tokens rotate, so the newest one
is saved before anything else happens. Refresh within 60 s of expiry, refresh again on a 401, and
ask for a new sign-in only when the refresh itself is refused. The refresh request is a JSON body,
as those pages specify. It has not yet run against real Swiggy.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import cast

import httpx
import httpx2

from app.accounts.crypto import TokenCipher, TokenDecryptError
from app.accounts.store import AccountStore
from app.commerce.base import CommerceAuthRequired, CommerceError
from app.commerce.swiggy import DelegatedLogin
from app.observability import log_event

REFRESH_MARGIN_SECONDS = 60
RECONNECT_MESSAGE = "Your Swiggy connection has ended. Please connect Swiggy again."
_TIMEOUT_SECONDS = 15.0
_DEFAULT_LIFETIME_SECONDS = 3600


@dataclass(frozen=True)
class SwiggyToken:
    access_token: str
    refresh_token: str
    expires_at: int


class SwiggyTokens:
    def __init__(
        self,
        store: AccountStore,
        cipher: TokenCipher,
        issuer: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._store = store
        self._cipher = cipher
        self.issuer = issuer
        self._transport = transport
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def _read(self, user_id: str) -> SwiggyToken | None:
        stored = await asyncio.to_thread(self._store.get_token, user_id, self.issuer)
        if stored is None:
            return None
        try:
            value = cast(
                dict[str, str],
                json.loads(
                    self._cipher.decrypt(stored.ciphertext, user_id=user_id, issuer=self.issuer)
                ),
            )
        except (TokenDecryptError, ValueError):
            # Fail closed: an unreadable record means a new sign-in, never another user's token.
            log_event("oauth", op="token_read", outcome="undecryptable")
            return None
        return SwiggyToken(value["a"], value.get("r", ""), stored.expires_at)

    async def save(
        self, user_id: str, response: dict[str, object], previous_refresh: str = ""
    ) -> SwiggyToken:
        """Store a token response from /auth/token. Raises ValueError if it holds no token."""
        access = response.get("access_token")
        if not isinstance(access, str) or not access:
            raise ValueError("Token response has no access token")
        refresh = response.get("refresh_token")
        lifetime = response.get("expires_in")
        token = SwiggyToken(
            access,
            refresh if isinstance(refresh, str) and refresh else previous_refresh,
            int(time.time())
            + (int(lifetime) if isinstance(lifetime, (int, float)) else _DEFAULT_LIFETIME_SECONDS),
        )
        sealed = self._cipher.encrypt(
            json.dumps({"a": token.access_token, "r": token.refresh_token}),
            user_id=user_id,
            issuer=self.issuer,
        )
        await asyncio.to_thread(
            self._store.put_token, user_id, self.issuer, sealed, token.expires_at
        )
        return token

    async def connected(self, user_id: str) -> bool:
        token = await self._read(user_id)
        return token is not None and token.expires_at > time.time()

    async def access_token(self, user_id: str, *, rejected: str = "") -> str:
        """A usable access token for this user. `rejected` is one Swiggy has just refused."""
        async with self._lock(user_id):
            token = await self._read(user_id)
            if token is None:
                raise CommerceAuthRequired(RECONNECT_MESSAGE)
            # Someone else may have refreshed while this call waited for the lock.
            stale = token.expires_at - time.time() <= REFRESH_MARGIN_SECONDS
            if not stale and token.access_token != rejected:
                return token.access_token
            return (await self._refresh(user_id, token)).access_token

    async def _refresh(self, user_id: str, token: SwiggyToken) -> SwiggyToken:
        if not token.refresh_token:
            await self.forget(user_id)
            raise CommerceAuthRequired(RECONNECT_MESSAGE)
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as http:
                response = await http.post(
                    f"{self.issuer}/auth/token",
                    json={"grant_type": "refresh_token", "refresh_token": token.refresh_token},
                )
        except httpx.HTTPError as exc:
            log_event("oauth", op="refresh", outcome="unreachable")
            raise CommerceError("Swiggy sign-in could not be reached") from exc
        if response.status_code >= 500:
            # server_error is worth another try later, and the token may still be good.
            log_event("oauth", op="refresh", outcome="server_error")
            raise CommerceError("Swiggy sign-in is having trouble")
        if response.status_code != 200:
            log_event("oauth", op="refresh", outcome="refused")
            await self.forget(user_id)
            raise CommerceAuthRequired(RECONNECT_MESSAGE)
        try:
            refreshed = await self.save(
                user_id, cast(dict[str, object], response.json()), token.refresh_token
            )
        except ValueError as exc:
            log_event("oauth", op="refresh", outcome="unreadable")
            raise CommerceError("Swiggy sign-in sent an unreadable reply") from exc
        log_event("oauth", op="refresh", outcome="ok")
        return refreshed

    async def forget(self, user_id: str) -> None:
        await asyncio.to_thread(self._store.delete_token, user_id, self.issuer)

    async def revoke(self, user_id: str) -> None:
        """End the session at Swiggy if we can, and always drop the token here."""
        async with self._lock(user_id):
            token = await self._read(user_id)
            await self.forget(user_id)
        if token is None:
            return
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as http:
                await http.post(
                    f"{self.issuer}/auth/logout",
                    headers={"Authorization": f"Bearer {token.access_token}"},
                )
        except httpx.HTTPError:
            log_event("oauth", op="logout", outcome="failed")


class DelegatedAuth(DelegatedLogin):
    """Puts one user's token on every MCP request, and refreshes it once if Swiggy refuses it.

    Resending after a 401 is safe even for checkout, because a refused token means Swiggy did not
    act on the request. The MCP SDK swallows exceptions raised in here, so a lost login is
    recorded in `login_lost` for the client to report, instead of hanging until a timeout.
    """

    def __init__(self, tokens: SwiggyTokens, user_id: str) -> None:
        self._tokens = tokens
        self._user_id = user_id
        self.login_lost = False

    async def ensure_fresh(self) -> None:
        """Before a call: fail now if this user has no usable login."""
        await self._tokens.access_token(self._user_id)
        self.login_lost = False

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        try:
            token = await self._tokens.access_token(self._user_id)
        except CommerceAuthRequired:
            self.login_lost = True
            raise
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            try:
                token = await self._tokens.access_token(self._user_id, rejected=token)
            except CommerceAuthRequired:
                self.login_lost = True
                return
            request.headers["Authorization"] = f"Bearer {token}"
            response = yield request
        if response.status_code in {401, 419}:
            # 419 means the session was revoked, so only a new sign-in helps.
            await self._tokens.forget(self._user_id)
            self.login_lost = True
