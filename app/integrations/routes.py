"""The Connected apps screen: GET /api/integrations.

Needs a signed-in person and shows only their own connections. Verification calls Google and Meta
with short timeouts, so the page asks once when the screen opens, not on a timer.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request

from app.accounts.service import UNAVAILABLE_MESSAGE, AccountService, AccountsUnavailable
from app.google.oauth import GoogleOAuth
from app.integrations.status import IntegrationNotes, collect
from app.whatsapp.client import WhatsAppClient


def integrations_router() -> APIRouter:
    router = APIRouter(prefix="/api/integrations")

    @router.get("")
    async def apps(request: Request) -> dict[str, object]:
        state = request.app.state
        accounts = cast(AccountService | None, getattr(state, "accounts", None))
        google = cast(GoogleOAuth | None, getattr(state, "google", None))
        if accounts is None or google is None:
            raise HTTPException(status_code=404, detail="Connected apps are not available here.")
        try:
            await accounts.require_ready()
            user = await accounts.current_user(request)
        except AccountsUnavailable as exc:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from exc
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        whatsapp = cast(WhatsAppClient | None, getattr(state, "whatsapp", None))
        notes = cast(IntegrationNotes, state.integration_notes)
        transport = getattr(state, "integration_transport", None)
        return {"apps": await collect(user, accounts, google, whatsapp, notes, transport)}

    return router
