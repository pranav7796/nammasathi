"""The hosted Swiggy sign-in: authorization code with PKCE, ending at /auth/callback.

Contract: https://mcp.swiggy.com/builders/docs/start/authenticate.md (Dynamic Client
Registration, /auth/authorize, /auth/token, /auth/logout). The token request is form-encoded
with client_id, which is what the MCP SDK sends in the working localhost login.

Tokens stay in memory, tied to one browser cookie, and are never logged or written anywhere.
Nothing here can reach a Swiggy tool: a public deployment only runs the mock shops, so this
proves the sign-in works and stops there. Using the token for live ordering per user is a
separate step, after Swiggy grants access.
"""

from __future__ import annotations

import base64
import hashlib
import html
import time
from dataclasses import dataclass
from secrets import compare_digest, token_urlsafe
from typing import cast
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

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


@dataclass(frozen=True)
class _Connected:
    access_token: str
    expires_at: float


def callback_url(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}{CALLBACK_PATH}"


def _issuer(settings: Settings) -> str:
    parts = urlsplit(settings.swiggy_mcp_url)
    return f"{parts.scheme}://{parts.netloc}"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _page(title: str, message: str, status: int = 200) -> HTMLResponse:
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title></head>"
        '<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem">'
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

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=transport)

    def forget_expired() -> None:
        now = time.monotonic()
        for state, item in list(pending.items()):
            if now - item.created >= PENDING_TTL_SECONDS:
                del pending[state]
        for browser, account in list(connected.items()):
            if account.expires_at <= now:
                del connected[browser]

    async def client_id(settings: Settings) -> str:
        """Dynamic Client Registration, once per redirect URI for this process."""
        redirect = callback_url(settings)
        if redirect not in registered:
            async with client() as http:
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
        return registered[redirect]

    @router.get("/auth/login")
    async def login(request: Request) -> Response:
        settings = cast(Settings, request.app.state.settings)
        if not settings.public_base_url:
            return JSONResponse(status_code=404, content={"detail": "Not configured."})
        forget_expired()
        if len(pending) >= MAX_PENDING:
            return _page("Please try again", "Too many sign-ins are in progress.", 429)
        try:
            issued = await client_id(settings)
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
        pending[state] = _Pending(verifier, browser, time.monotonic())
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
        response = RedirectResponse(f"{_issuer(settings)}/auth/authorize?{query}", status_code=302)
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
        try:
            registered_id = await client_id(settings)
            async with client() as http:
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
        return JSONResponse(
            {"connected": request.cookies.get(COOKIE_NAME, "") in connected},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        settings = cast(Settings, request.app.state.settings)
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
