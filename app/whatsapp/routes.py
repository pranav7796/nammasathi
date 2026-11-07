"""WhatsApp webhook and status routes.

/api/whatsapp/webhook (GET and POST) is PUBLIC: Meta calls it without a session, so it is
protected by the verify token (GET) and the X-Hub-Signature-256 HMAC (POST) instead.

Docs: https://developers.facebook.com/docs/graph-api/webhooks/getting-started
"""

from __future__ import annotations

import hmac
import json
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.accounts.service import AccountService
from app.observability import log_event
from app.whatsapp import store
from app.whatsapp.webhook import StatusUpdate, count_inbound, parse_statuses, verify_signature

MAX_BODY_BYTES = 256 * 1024


def _service(request: Request) -> AccountService:
    service = cast(AccountService | None, getattr(request.app.state, "accounts", None))
    if service is None:
        raise HTTPException(status_code=404, detail="WhatsApp is not available here.")
    return service


def whatsapp_router(accounts: AccountService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/whatsapp")

    @router.get("/webhook")
    async def verify(
        request: Request,
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ) -> PlainTextResponse:
        service = accounts or _service(request)
        settings = service.settings
        if not settings.whatsapp_configured or not settings.whatsapp_verify_token:
            raise HTTPException(status_code=404, detail="WhatsApp is not configured.")
        if mode != "subscribe" or not hmac.compare_digest(
            token.encode(), settings.whatsapp_verify_token.encode()
        ):
            log_event("whatsapp", op="verify", outcome="refused")
            raise HTTPException(status_code=403, detail="Verification failed.")
        log_event("whatsapp", op="verify", outcome="ok")
        return PlainTextResponse(challenge)

    @router.post("/webhook")
    async def receive(request: Request) -> dict[str, bool]:
        service = accounts or _service(request)
        settings = service.settings
        if not settings.whatsapp_configured:
            raise HTTPException(status_code=404, detail="WhatsApp is not configured.")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="That is too large.")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="That is too large.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        # Signature first, over the raw bytes, before anything is parsed.
        if not verify_signature(
            settings.whatsapp_app_secret, raw, request.headers.get("x-hub-signature-256")
        ):
            log_event("whatsapp", op="webhook", outcome="bad_signature")
            raise HTTPException(status_code=403, detail="Bad signature.")
        try:
            payload: object = json.loads(raw)
        except ValueError:
            log_event("whatsapp", op="webhook", outcome="unparseable")
            return {"ok": True}  # signed but unusable: do not make Meta retry it forever
        updates = [(u.message_id, u.status, _detail(u)) for u in parse_statuses(payload)]
        inbound = count_inbound(payload)
        changed = 0
        if updates:
            changed = await service.run(store.apply_statuses, service.database, updates)
        log_event(
            "whatsapp",
            op="webhook",
            outcome="ok",
            items=len(updates) + inbound,
            kind=f"inbound={inbound} changed={changed}",
        )
        return {"ok": True}

    @router.get("/status")
    async def status(request: Request) -> dict[str, bool]:
        service = accounts or _service(request)
        user = await service.current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        return {"configured": service.settings.whatsapp_configured}

    return router


def _detail(update: StatusUpdate) -> str:
    if update.error_code is None and not update.error_title:
        return ""
    return f"error {update.error_code}: {update.error_title}".strip()[:200]
