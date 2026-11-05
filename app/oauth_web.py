"""The hosted Swiggy sign-in: authorization code with PKCE, ending at /auth/callback.

Contract: https://mcp.swiggy.com/builders/docs/start/authenticate.md (Dynamic Client
Registration, /auth/authorize, /auth/token, /auth/logout). The token request is form-encoded
with client_id, which is what the MCP SDK sends in the working localhost login.

Without accounts, tokens stay in memory, tied to one browser cookie, and nothing can reach a Swiggy
tool, so this only proves the sign-in works. With accounts and MULTI_USER_LIVE, the signed-in user's
tokens are stored encrypted (app/accounts/swiggy_tokens.py) and used only for that user.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import time
from dataclasses import dataclass
from secrets import compare_digest, token_urlsafe
from typing import cast
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.accounts.service import AccountService, issuer_of
from app.config import Settings
from app.observability import log_event

CALLBACK_PATH = "/auth/callback"
COOKIE_NAME = "saathi_auth"
PENDING_TTL_SECONDS = 10 * 60
MAX_PENDING = 200
MAX_CONNECTED = 200
_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class _Pending:
    verifier: str
    browser: str
    created: float
    user_id: str = ""


@dataclass(frozen=True)
class _Connected:
    access_token: str
    expires_at: float


def callback_url(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}{CALLBACK_PATH}"


def _issuer(settings: Settings) -> str:
    return issuer_of(settings)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _page(title: str, message: str, status: int = 200) -> HTMLResponse:
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        '<link rel="stylesheet" href="/static/style.css"></head>'
        '<body class="plain-page">'
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        '<p><a href="/">Back to Namma Saathi</a></p></body></html>'
    )
    return HTMLResponse(
        body,
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def oauth_router(transport: httpx.AsyncBaseTransport | None = None) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    pending: dict[str, _Pending] = {}
    connected: dict[str, _Connected] = {}
    registered: dict[str, str] = {}

    def accounts_of(request: Request) -> AccountService | None:
        return cast(AccountService | None, getattr(request.app.state, "accounts", None))

    def client(service: AccountService | None = None) -> httpx.AsyncClient:
        stubbed = service.stub.oauth_transport if service and service.stub else transport
        return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=stubbed)

    def forget_expired() -> None:
        now = time.monotonic()
        for state, item in list(pending.items()):
            if now - item.created >= PENDING_TTL_SECONDS:
                del pending[state]
        for browser, account in list(connected.items()):
            if account.expires_at <= now:
                del connected[browser]

    async def client_id(settings: Settings, service: AccountService | None = None) -> str:
        """Dynamic Client Registration, once per redirect URI; kept across restarts if we can."""
        redirect = callback_url(settings)
        if service is not None:
            saved = await asyncio.to_thread(service.store.oauth_client, _issuer(settings), redirect)
            if saved:
                return saved
        if redirect not in registered:
            async with client(service) as http:
                response = await http.post(
                    f"{_issuer(settings)}/auth/register",
                    json={
                        "client_name": "Namma Saathi",
                        "redirect_uris": [redirect],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                    },
                )
            response.raise_for_status()
            issued = cast(dict[str, object], response.json()).get("client_id")
            if not isinstance(issued, str) or not issued:
                raise ValueError("Registration returned no client_id")
            registered[redirect] = issued
            if service is not None:
                await asyncio.to_thread(
                    service.store.save_oauth_client, _issuer(settings), redirect, issued
                )
        return registered[redirect]

    @router.get("/auth/login")
    async def login(request: Request) -> Response:
        settings = cast(Settings, request.app.state.settings)
        if not settings.public_base_url:
            return JSONResponse(status_code=404, content={"detail": "Not configured."})
        forget_expired()
        service = accounts_of(request)
        user_id = ""
        if service is not None:
            user = await service.current_user(request)
            if user is None:
                return RedirectResponse("/#signin", status_code=302)
            if not settings.multi_user_live:
                return _page("Not open yet", "Live Swiggy is not open on this site yet.", 403)
            if not user.email_verified or not user.live_access:
                return _page(
                    "Not available for this account yet",
                    "Live Swiggy is invite-only for now, and needs a verified email. "
                    "You can still use the demo shops.",
                    403,
                )
            user_id = user.id
        if len(pending) >= MAX_PENDING:
            return _page("Please try again", "Too many sign-ins are in progress.", 429)
        try:
            issued = await client_id(settings, service)
        except (httpx.HTTPError, ValueError):
            log_event("oauth", op="register", outcome="failed")
            return _page(
                "Swiggy sign-in is not available",
                "Swiggy did not accept this app's sign-in registration. "
                "This usually means its redirect address is not yet allowlisted.",
                503,
            )
        verifier = token_urlsafe(48)
        state = token_urlsafe(32)
        browser = request.cookies.get(COOKIE_NAME) or token_urlsafe(32)
        pending[state] = _Pending(verifier, browser, time.monotonic(), user_id)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": issued,
                "redirect_uri": callback_url(settings),
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "state": state,
                "scope": "mcp:tools",
            }
        )
        # Only a test harness's stub can change where the browser goes.
        browser_base = service.stub.browser_base if service and service.stub else _issuer(settings)
        response = RedirectResponse(f"{browser_base}/auth/authorize?{query}", status_code=302)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            COOKIE_NAME,
            browser,
            httponly=True,
            secure=settings.public_base_url.startswith("https://"),
            samesite="lax",
            path="/auth",
            max_age=24 * 60 * 60,
        )
        return response

    @router.get(CALLBACK_PATH)
    async def callback(request: Request) -> Response:
        settings = cast(Settings, request.app.state.settings)
        if not settings.public_base_url:
            return JSONResponse(status_code=404, content={"detail": "Not configured."})
        forget_expired()
        params = request.query_params
        state = params.get("state", "")
        item = pending.pop(state, None)
        browser = request.cookies.get(COOKIE_NAME, "")
        if item is None or not browser or not compare_digest(item.browser, browser):
            log_event("oauth", op="callback", outcome="bad_state")
            return _page(
                "This sign-in link is not valid",
                "It may have expired or come from another browser. Please start again.",
                400,
            )
        if params.get("error") or not params.get("code"):
            log_event("oauth", op="callback", outcome="declined")
            return _page("Swiggy was not connected", "The sign-in was cancelled or refused.", 400)
        service = accounts_of(request)
        if service is not None:
            # A token goes only to the account that started this sign-in, in this browser.
            user = await service.current_user(request)
            if user is None or user.id != item.user_id:
                log_event("oauth", op="callback", outcome="wrong_user")
                return _page(
                    "This sign-in link is not valid",
                    "It was started from a different account. Please start again.",
                    400,
                )
        try:
            registered_id = await client_id(settings, service)
            async with client(service) as http:
                response = await http.post(
                    f"{_issuer(settings)}/auth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": params["code"],
                        "code_verifier": item.verifier,
                        "redirect_uri": callback_url(settings),
                        "client_id": registered_id,
                    },
                )
            response.raise_for_status()
            token = cast(dict[str, object], response.json())
            access_token = token.get("access_token")
            lifetime = token.get("expires_in")
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("Token response has no access token")
        except (httpx.HTTPError, ValueError):
            log_event("oauth", op="token", outcome="failed")
            return _page("Swiggy was not connected", "The sign-in could not be completed.", 502)
        if service is not None:
            try:
                await service.tokens.save(item.user_id, token)
            except ValueError:
                log_event("oauth", op="token", outcome="failed")
                return _page("Swiggy was not connected", "The sign-in could not be completed.", 502)
            log_event("oauth", op="token", outcome="connected")
            redirect = RedirectResponse("/#connected", status_code=303)
            redirect.headers["Cache-Control"] = "no-store"
            return redirect
        if len(connected) >= MAX_CONNECTED:
            connected.pop(next(iter(connected)))
        seconds = float(lifetime) if isinstance(lifetime, (int, float)) else 3600.0
        connected[browser] = _Connected(access_token, time.monotonic() + seconds)
        log_event("oauth", op="token", outcome="connected")
        return _page(
            "Swiggy is connected",
            "Your sign-in worked. This public demo still uses mock shops and will not read or "
            "change your Swiggy account.",
        )

    @router.get("/auth/status")
    async def status(request: Request) -> JSONResponse:
        forget_expired()
        service = accounts_of(request)
        if service is not None:
            user = await service.current_user(request)
            is_connected = user is not None and await service.tokens.connected(user.id)
            return JSONResponse({"connected": is_connected}, headers={"Cache-Control": "no-store"})
        return JSONResponse(
            {"connected": request.cookies.get(COOKIE_NAME, "") in connected},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        settings = cast(Settings, request.app.state.settings)
        service = accounts_of(request)
        if service is not None:
            user = await service.current_user(request)
            if user is None:
                return JSONResponse(status_code=401, content={"detail": "Please sign in."})
            await request.app.state.close_user_conversations(user.id)
            await service.tokens.revoke(user.id)
            return JSONResponse({"connected": False})
        account = connected.pop(request.cookies.get(COOKIE_NAME, ""), None)
        if account is not None:
            try:
                async with client() as http:
                    await http.post(
                        f"{_issuer(settings)}/auth/logout",
                        headers={"Authorization": f"Bearer {account.access_token}"},
                    )
            except httpx.HTTPError:
                log_event("oauth", op="logout", outcome="failed")
        return JSONResponse({"connected": False})

    return router
