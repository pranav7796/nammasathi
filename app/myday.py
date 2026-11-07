"""My Day: one screen of the person's own day, from real connected data only.

Nothing here is invented. If Gmail or Calendar is not connected the section says so, and says what
to do. If a service refuses or cannot be reached the section says that instead of going blank or
showing practice data. Reminders and recent activity are the person's own records in this site.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request

from app.accounts.service import UNAVAILABLE_MESSAGE, AccountService, AccountsUnavailable
from app.integrations.base import IntegrationError, NotConnected, ReauthRequired
from app.personal.dates import IST
from app.personal.service import PersonalService, from_ms
from app.tasks import kinds as K
from app.tasks.services import ServiceHub
from app.tasks.store import TaskStore

_CONNECT_CALENDAR = "Connect Google Calendar in Apps to see today's events."
_CONNECT_GMAIL = "Connect Gmail in Apps to see the email that matters."


def _hhmm(moment: datetime) -> str:
    return moment.astimezone(IST).strftime("%H:%M")


async def _calendar(hub: ServiceHub, user_id: str, now: datetime) -> dict[str, Any]:
    services = await hub.for_user(user_id)
    if services.demo["calendar"]:
        return {"state": "not_connected", "message": _CONNECT_CALENDAR, "events": []}
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        events = await services.calendar.events(user_id, start, start + timedelta(days=1))
    except ReauthRequired:
        return {
            "state": "authorization_failed",
            "message": "Google no longer accepts this connection. Connect again in Apps.",
            "events": [],
        }
    except (IntegrationError, NotConnected):
        return {
            "state": "unavailable",
            "message": "Couldn't reach Google Calendar just now.",
            "events": [],
        }
    return {
        "state": "ok",
        "message": "" if events else "Nothing on your calendar today.",
        "events": [
            {
                "id": e.id,
                "title": e.title,
                "start": _hhmm(e.start),
                "end": _hhmm(e.end),
                "now": e.start <= now < e.end,
            }
            for e in events[:12]
        ],
    }


async def _emails(hub: ServiceHub, user_id: str) -> dict[str, Any]:
    services = await hub.for_user(user_id)
    if services.demo["gmail"]:
        return {"state": "not_connected", "message": _CONNECT_GMAIL, "items": []}
    try:
        found = await services.email.important(user_id, 5)
    except ReauthRequired:
        return {
            "state": "authorization_failed",
            "message": "Google no longer accepts this connection. Connect again in Apps.",
            "items": [],
        }
    except (IntegrationError, NotConnected):
        return {"state": "unavailable", "message": "Couldn't reach Gmail just now.", "items": []}
    return {
        "state": "ok",
        "message": "" if found else "No important email right now.",
        "items": [
            {
                "id": m.id,
                "sender": m.sender_name or m.sender_email,
                "subject": m.subject,
                "needs_reply": m.needs_reply,
                "unread": m.unread,
            }
            for m in found
        ],
    }


async def build(
    user_id: str,
    accounts: AccountService,
    hub: ServiceHub,
    store: TaskStore,
    now: datetime,
) -> dict[str, Any]:
    personal = PersonalService(accounts)
    reminders = [r for r in await personal.list_reminders(user_id) if r.done_at is None]
    today = now.date()
    due: list[dict[str, Any]] = []
    for r in reminders:
        when = None if r.due_at is None else from_ms(r.due_at)
        due.append(
            {
                "id": r.id,
                "text": r.text,
                "due": None if when is None else when.isoformat(),
                "overdue": when is not None and when < now,
                "today": when is not None and when.date() == today,
            }
        )
    due.sort(key=lambda r: (r["due"] is None, r["due"] or ""))
    recent = await accounts.run(store.recent, user_id, 8)
    waiting = [t for t in recent if t.status in ("waiting_for_approval", "waiting_for_input")]
    return {
        "date": today.isoformat(),
        "calendar": await _calendar(hub, user_id, now),
        "emails": await _emails(hub, user_id),
        "reminders": {
            "state": "ok",
            "message": "" if due else "No open reminders.",
            "items": due[:8],
            "total": len(due),
        },
        "waiting": [
            {"id": t.id, "title": K.spec_for(t.kind).label, "status": t.status} for t in waiting
        ],
        "activity": [
            {
                "title": K.spec_for(t.kind).label,
                "status": t.status,
                "demo": bool(t.result.get("demo")),
                "at": from_ms(t.updated_at).isoformat(),
            }
            for t in recent
            if t.status in ("completed", "failed", "cancelled")
        ][:6],
    }


def myday_router() -> APIRouter:
    router = APIRouter(prefix="/api/myday")

    @router.get("")
    async def myday(request: Request) -> dict[str, object]:
        state = request.app.state
        accounts = cast(AccountService | None, getattr(state, "accounts", None))
        hub = cast(ServiceHub | None, getattr(state, "hub", None))
        store = cast(TaskStore | None, getattr(state, "task_store", None))
        if accounts is None or hub is None or store is None:
            raise HTTPException(status_code=404, detail="My day is not available here.")
        try:
            await accounts.require_ready()
            user = await accounts.current_user(request)
        except AccountsUnavailable as exc:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from exc
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        return await build(user.id, accounts, hub, store, datetime.now(IST))

    return router
