"""Web routes for connecting Google. Not mounted by this module; web.py includes the router.

The oauth object is `request.app.state.google` if the app set one (tests do), else it is built
once per account service from its settings and database.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.accounts.service import AccountService
from app.accounts.store import User
from app.google.oauth import (
    SERVICES,
    ConnectionStore,
    GoogleOAuth,
    GoogleOAuthError,
    UnknownService,
)
from app.observability import log_event

NOT_CONFIGURED = "Connecting Google is not set up on this site yet."


class DisconnectRequest(BaseModel):
    service: str = Field(max_length=20)


def _service(request: Request, accounts: AccountService | None) -> AccountService:
    service = accounts or cast(AccountService | None, getattr(request.app.state, "accounts", None))
    if service is None:
        raise HTTPException(status_code=404, detail="Accounts are not enabled here.")
    return service


def google_router(accounts: AccountService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/google")
    built: dict[int, GoogleOAuth] = {}

    def oauth_for(request: Request, service: AccountService) -> GoogleOAuth:
        shared = cast(GoogleOAuth | None, getattr(request.app.state, "google", None))
        if shared is not None:
            return shared
        if id(service) not in built:
            built[id(service)] = GoogleOAuth(service.settings, ConnectionStore(service))
        return built[id(service)]

    async def signed_in(service: AccountService, request: Request) -> User:
        user = await service.current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        return user

    @router.get("/status")
    async def status(request: Request) -> dict[str, object]:
        service = _service(request, accounts)
        user = await signed_in(service, request)
        oauth = oauth_for(request, service)
        return {"configured": service.settings.google_configured, **await oauth.status(user.id)}

    @router.get("/connect")
    async def connect(request: Request, service: str = "gmail") -> dict[str, str]:
        accounts_service = _service(request, accounts)
        user = await signed_in(accounts_service, request)
        if service not in SERVICES:
            raise HTTPException(status_code=400, detail="Unknown service.")
        if not accounts_service.settings.google_configured:
            raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
        oauth = oauth_for(request, accounts_service)
        try:
            return {"url": oauth.build_authorization_url(user.id, service)}
        except UnknownService:
            raise HTTPException(status_code=400, detail="Unknown service.") from None

    @router.get("/callback")
    async def callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: str = "",
    ) -> RedirectResponse:
        accounts_service = _service(request, accounts)
        user = await signed_in(accounts_service, request)
        oauth = oauth_for(request, accounts_service)
        try:
            await oauth.handle_callback(user.id, code, state, error or None)
        except GoogleOAuthError:
            return RedirectResponse("/#google=error", status_code=303)
        log_event("google", op="callback", outcome="connected")
        return RedirectResponse("/#google=connected", status_code=303)

    @router.post("/disconnect")
    async def disconnect(body: DisconnectRequest, request: Request) -> dict[str, str]:
        accounts_service = _service(request, accounts)
        user = await signed_in(accounts_service, request)
        if body.service not in SERVICES:
            raise HTTPException(status_code=400, detail="Unknown service.")
        await oauth_for(request, accounts_service).disconnect(user.id, body.service)
        return {"status": "disconnected"}

    return router
