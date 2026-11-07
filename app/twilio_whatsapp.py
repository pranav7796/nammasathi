"""Inbound WhatsApp from Twilio (the Sandbox, or a Twilio WhatsApp number).

    POST {PUBLIC_BASE_URL}/api/twilio/whatsapp

Twilio posts a form (application/x-www-form-urlencoded) with `From`, `To`, `Body`, `MessageSid` and
more. Official documentation, read for this module:
  * request signing: https://www.twilio.com/docs/usage/security (X-Twilio-Signature is the base64 of
    HMAC-SHA1, keyed with the Auth Token, over the full URL followed by every POST field sorted by
    name, each as name+value with no delimiters)
  * inbound WhatsApp: https://www.twilio.com/docs/whatsapp/api (To and From are `whatsapp:+E164`;
    the reply may be TwiML in the webhook response)

What this does, in order, and what it refuses:
1. Fails closed: with no Auth Token configured the route does not exist (404).
2. Verifies the signature BEFORE reading anything else. A missing or wrong signature is a 403.
3. Finds the person from the sender's number. A number counts only after that number itself sent the
   person's one-time link code, so a message can only ever act on its own sender's account.
4. Hands the text to the same task engine the website uses (planning, spoken-style approval by
   readback, reminders). Anything that changes something outside Saathi still waits for the exact
   content to be read back and an explicit "confirm".
5. Replies in the webhook response (TwiML). That needs no Twilio credentials, and nothing is sent
   on its own.
Groceries, food and tables stay on the website: they need the basket and readback screens.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from typing import Any, cast
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request, Response

from app.accounts.db import Database
from app.accounts.service import UNAVAILABLE_MESSAGE, AccountService, AccountsUnavailable
from app.observability import log_event
from app.personal import voice as reminder_voice
from app.personal.service import PersonalService
from app.tasks import kinds as K
from app.tasks import turn as task_turn
from app.tasks.engine import Snapshot, TaskEngine
from app.tasks.kinds import is_phone
from app.tasks.planner import PlanExtractor
from app.twilio_messaging import STATUS as TWILIO_STATUS
from app.whatsapp import store

PATH = "/api/twilio/whatsapp"
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT = 1000
REPLY_LIMIT = 1500  # WhatsApp allows 1600 characters
CODE_SECONDS = 15 * 60
PER_SENDER_PER_HOUR = 40
_SEEN_KEEP = 2000
_LINK = re.compile(r"^\W*link\s+([A-Za-z0-9]{6,10})\W*$", re.IGNORECASE)


# Signing ----------------------------------------------------------------------------------------


def expected_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's signature for a POST: the URL, then every field sorted by name as name+value."""
    text = url + "".join(f"{name}{params[name]}" for name in sorted(params))
    digest = hmac.new(auth_token.encode(), text.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(auth_token: str, url: str, params: dict[str, str], header: str) -> bool:
    if not header:
        return False
    return hmac.compare_digest(expected_signature(auth_token, url, params), header.strip())


def webhook_url(public_base_url: str, query: str = "") -> str:
    """The exact address Twilio called. Built from the configured public address, never from the
    request, because behind a proxy the request would say http and the wrong host."""
    base = f"{public_base_url.rstrip('/')}{PATH}"
    return f"{base}?{query}" if query else base


# Numbers ----------------------------------------------------------------------------------------


def normalise_sender(raw: str) -> str | None:
    """ "whatsapp:+919876543210" -> "+919876543210", or None if it is not a WhatsApp number."""
    if not raw.startswith("whatsapp:"):
        return None
    number = raw.removeprefix("whatsapp:").replace(" ", "")
    return number if is_phone(number) else None


def mask(phone: str) -> str:
    return f"{phone[:3]}•••••{phone[-4:]}" if len(phone) > 8 else "•••"


def link_phone(db: Database, user_id: str, phone: str, now_ms: int) -> str:
    """'linked', 'already' (they already have this number) or 'taken' (someone else has it)."""
    with db.transaction() as tx:
        tx.lock_user(user_id)
        owner = tx.execute("SELECT user_id FROM whatsapp_links WHERE phone = ?", (phone,))
        if owner:
            return "already" if str(owner[0]["user_id"]) == user_id else "taken"
        tx.execute("DELETE FROM whatsapp_links WHERE user_id = ?", (user_id,))
        tx.execute(
            "INSERT INTO whatsapp_links (phone, user_id, created_at) VALUES (?, ?, ?)",
            (phone, user_id, now_ms),
        )
    return "linked"


def user_for_phone(db: Database, phone: str) -> str | None:
    with db.transaction() as tx:
        rows = tx.execute("SELECT user_id FROM whatsapp_links WHERE phone = ?", (phone,))
    return str(rows[0]["user_id"]) if rows else None


def phone_for_user(db: Database, user_id: str) -> str | None:
    with db.transaction() as tx:
        rows = tx.execute("SELECT phone FROM whatsapp_links WHERE user_id = ?", (user_id,))
    return str(rows[0]["phone"]) if rows else None


def unlink_user(db: Database, user_id: str) -> None:
    with db.transaction() as tx:
        tx.execute("DELETE FROM whatsapp_links WHERE user_id = ?", (user_id,))


# Results ----------------------------------------------------------------------------------------

RESULT_WAIT_SECONDS = 8.0  # Twilio waits about 15 s for the webhook; reads usually finish sooner


async def _results(engine: TaskEngine, user_id: str, plan_id: str) -> str:
    """What the plan found, in a few lines, once its reads finish (or as far as they got)."""
    deadline = time.monotonic() + RESULT_WAIT_SECONDS
    snapshot = await engine.snapshot(user_id, plan_id)
    while snapshot is not None and snapshot.active and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        snapshot = await engine.snapshot(user_id, plan_id)
    if snapshot is None:
        return ""
    return summarise(snapshot)


def summarise(snapshot: Snapshot) -> str:
    """Plain text for a chat. Demo results say so. Nothing is called done that isn't."""
    lines: list[str] = []
    for task in snapshot.tasks:
        view = cast(dict[str, Any], task.result.get("view") or {})
        demo = " (demo data)" if view.get("demo") else ""
        if task.status in ("pending", "running"):
            lines.append(f"\u2026 {K.spec_for(task.kind).label}: still working.")
        elif task.status == "failed":
            lines.append(f"\u2717 {K.spec_for(task.kind).label}: {task.error or "didn't finish"}")
        elif task.status == "waiting_for_input":
            lines.append(f"? {task.result.get('needs') or 'I need one more detail.'}")
        elif task.status == "waiting_for_approval":
            lines.append(
                f'\u23f8 {K.spec_for(task.kind).label} is ready. Reply "send it" to hear it, '
                'then "confirm".'
            )
        elif "emails" in view:
            mails = cast(list[dict[str, Any]], view["emails"])
            if not mails:
                lines.append(f"Email{demo}: nothing important.")
            else:
                lines.append(f"Email{demo}:")
                lines += [f"\u2022 {m['sender']}: {m['subject']}" for m in mails[:4]]
        elif "days" in view:
            days = cast(list[dict[str, Any]], view["days"])
            events = [(d["date"], e) for d in days for e in d["events"]]
            if not events:
                lines.append(f"Calendar{demo}: nothing scheduled.")
            else:
                lines.append(f"Calendar{demo}:")
                lines += [f"\u2022 {day} {e['start']} {e['title']}" for day, e in events[:5]]
        elif "window" in view:
            if view.get("free"):
                lines.append(f"That time is free{demo}.")
            else:
                clashes = ", ".join(c["title"] for c in view.get("conflicts", [])[:3])
                lines.append(f"That time clashes with {clashes}{demo}.")
        elif "items" in view:
            items = cast(list[dict[str, Any]], view["items"])
            lines.append(f"Your day{demo}:")
            lines += [f"\u2022 {i.get('time') or '--:--'} {i['title']}" for i in items[:6]]
    return "\n".join(lines)


# Replies ----------------------------------------------------------------------------------------


def twiml(message: str | None) -> Response:
    """A TwiML reply, or an empty one (Twilio then sends nothing)."""
    inner = f"<Message>{escape(message[:REPLY_LIMIT])}</Message>" if message else ""
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>'
    return Response(content=xml, media_type="application/xml")


_HELP = (
    "Hi, I'm Namma Saathi. Email, calendar, reminders, WhatsApp drafts and your day plan work "
    "here. For groceries, food and tables, open the website."
)
_NOT_LINKED = (
    "This number isn't linked to a Namma Saathi account yet. Sign in on the website, open Apps, "
    "choose WhatsApp (Twilio), and send me the code it shows, like: link ABC123"
)


class _Limiter:
    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        kept = [t for t in self._events.get(key, []) if now - t < 3600]
        if len(kept) >= PER_SENDER_PER_HOUR:
            self._events[key] = kept
            return False
        kept.append(now)
        self._events[key] = kept
        if len(self._events) > 5000:
            self._events.clear()
        return True


def twilio_router() -> APIRouter:
    router = APIRouter(prefix="/api/twilio")
    codes: dict[str, tuple[str, float]] = {}  # code -> (user id, expires)
    seen: OrderedDict[str, None] = OrderedDict()
    limiter = _Limiter()

    def service_of(request: Request) -> AccountService:
        service = cast(AccountService | None, getattr(request.app.state, "accounts", None))
        if service is None:
            raise HTTPException(status_code=404, detail="WhatsApp isn't available here.")
        return service

    async def person(request: Request, service: AccountService) -> str:
        try:
            await service.require_ready()
            user = await service.current_user(request)
        except AccountsUnavailable as exc:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from exc
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        return user.id

    async def read_form(request: Request) -> dict[str, str]:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Too large.")
        return dict(parse_qsl(bytes(raw).decode("utf-8", "replace"), keep_blank_values=True))

    @router.post("/whatsapp")
    async def inbound(request: Request) -> Response:
        service = service_of(request)
        settings = service.settings
        if not settings.twilio_configured or settings.twilio_auth_token is None:
            raise HTTPException(status_code=404, detail="Not found.")  # fail closed
        params = await read_form(request)
        url = webhook_url(settings.public_base_url, request.url.query)
        if not valid_signature(
            settings.twilio_auth_token, url, params, request.headers.get("X-Twilio-Signature", "")
        ):
            log_event("twilio", op="inbound", outcome="bad_signature")
            raise HTTPException(status_code=403, detail="Forbidden.")
        if settings.twilio_account_sid and params.get("AccountSid") != settings.twilio_account_sid:
            log_event("twilio", op="inbound", outcome="wrong_account")
            raise HTTPException(status_code=403, detail="Forbidden.")

        message_id = params.get("MessageSid", "")
        if message_id:
            if message_id in seen:
                return twiml(None)  # Twilio retried: it was already handled
            seen[message_id] = None
            while len(seen) > _SEEN_KEEP:
                seen.popitem(last=False)
        sender = normalise_sender(params.get("From", ""))
        if sender is None:
            return twiml(None)
        if not limiter.allow(sender):
            log_event("twilio", op="inbound", outcome="rate_limited")
            return twiml(None)
        body = params.get("Body", "").strip()[:MAX_TEXT]

        try:
            if _LINK.match(body):
                return twiml(await _try_link(service, sender, body))  # linked or not
            user_id = await service.run(user_for_phone, service.database, sender)
            if user_id is None:
                log_event("twilio", op="inbound", outcome="not_linked")
                return twiml(_NOT_LINKED)
            if not body:
                return twiml("I can read text messages only for now.")
            return twiml(await _handle_text(request, service, user_id, body))
        except AccountsUnavailable:
            return twiml("I'm resting for a moment. Please try again shortly.")

    async def _try_link(service: AccountService, sender: str, body: str) -> str:
        match = _LINK.match(body)
        assert match is not None
        entry = codes.pop(match.group(1).upper(), None)
        if entry is None or time.monotonic() > entry[1]:
            log_event("twilio", op="link", outcome="bad_code")
            return "That code didn't work or has expired. Get a new one on the website."
        result = await service.run(
            link_phone, service.database, entry[0], sender, int(time.time() * 1000)
        )
        log_event("twilio", op="link", outcome=result)
        if result == "taken":
            return "This number is already linked to another account. Unlink it there first."
        return "Linked. You can now ask me for email, calendar, reminders and your day plan here."

    async def _handle_text(
        request: Request, service: AccountService, user_id: str, text: str
    ) -> str:
        engine = cast(TaskEngine | None, getattr(request.app.state, "tasks", None))
        extractor = cast(PlanExtractor | None, getattr(request.app.state, "plan_extractor", None))
        language = await service.run(service.store.language, user_id) or "en-IN"
        if engine is not None:
            reply = await task_turn.handle(
                engine, user_id, language, text, extractor, order_busy=False
            )
            if reply is not None:
                if reply.plan_id is None:
                    return reply.message
                summary = await _results(engine, user_id, reply.plan_id)
                return f"{reply.message}\n\n{summary}" if summary else reply.message
        wording = reminder_voice.reminder_request(text)
        if wording is not None:
            personal = PersonalService(service)
            saved = await personal.create_reminder_from_text(user_id, wording)
            return reminder_voice.reply_text(language, saved)
        return _HELP

    @router.post("/status")
    async def delivery(request: Request) -> Response:
        """Twilio's status callback for a message Saathi sent. Signed like the inbound webhook."""
        service = service_of(request)
        settings = service.settings
        if not settings.twilio_configured or settings.twilio_auth_token is None:
            raise HTTPException(status_code=404, detail="Not found.")
        params = await read_form(request)
        url = f"{settings.public_base_url.rstrip('/')}/api/twilio/status"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        if not valid_signature(
            settings.twilio_auth_token, url, params, request.headers.get("X-Twilio-Signature", "")
        ):
            log_event("twilio", op="status", outcome="bad_signature")
            raise HTTPException(status_code=403, detail="Forbidden.")
        message_id = params.get("MessageSid", "")
        status = TWILIO_STATUS.get(params.get("MessageStatus", ""), "")
        if message_id and status and status != "accepted":
            detail = f"Twilio error {params['ErrorCode']}" if params.get("ErrorCode") else ""
            try:
                await service.run(store.apply_status, service.database, message_id, status, detail)
            except AccountsUnavailable:
                raise HTTPException(status_code=503, detail="Try again.") from None
        log_event("twilio", op="status", outcome=status or "ignored")
        return Response(status_code=204)

    # The person's side: get a code, see the status, unlink. These need a signed-in session.

    @router.post("/link")
    async def create_code(request: Request) -> dict[str, object]:
        service = service_of(request)
        user_id = await person(request, service)
        if not service.settings.twilio_configured:
            raise HTTPException(status_code=503, detail="WhatsApp (Twilio) isn't set up here yet.")
        for old in [c for c, (owner, _) in codes.items() if owner == user_id]:
            del codes[old]
        code = secrets.token_hex(3).upper()
        codes[code] = (user_id, time.monotonic() + CODE_SECONDS)
        if len(codes) > 2000:
            codes.clear()
            codes[code] = (user_id, time.monotonic() + CODE_SECONDS)
        sandbox = service.settings.twilio_whatsapp_from.removeprefix("whatsapp:")
        return {
            "code": code,
            "expires_in": CODE_SECONDS,
            "send_to": sandbox,
            "message": f"link {code}",
        }

    @router.get("/status")
    async def status(request: Request) -> dict[str, object]:
        service = service_of(request)
        user_id = await person(request, service)
        settings = service.settings
        phone = await service.run(phone_for_user, service.database, user_id)
        return {
            "configured": settings.twilio_configured,
            "linked": phone is not None,
            "phone": mask(phone) if phone else None,
            "webhook_url": webhook_url(settings.public_base_url)
            if settings.twilio_configured
            else None,
            "send_to": settings.twilio_whatsapp_from.removeprefix("whatsapp:") or None,
        }

    @router.post("/unlink")
    async def unlink(request: Request) -> dict[str, str]:
        service = service_of(request)
        user_id = await person(request, service)
        await service.run(unlink_user, service.database, user_id)
        return {"status": "unlinked"}

    return router
