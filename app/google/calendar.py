"""Google Calendar adapter implementing `CalendarClient` over the Calendar API v3 REST endpoints.

Official references this relies on (all under https://developers.google.com/workspace/calendar/api/):
- v3/reference/events/list: timeMin/timeMax are RFC3339 with an offset, singleEvents, orderBy=
  startTime (needs singleEvents), maxResults (default 250, max 2500), showDeleted, pageToken.
- v3/reference/events/insert: client-supplied `id` uses base32hex characters (a-v and 0-9), length
  5 to 1024; `sendUpdates` is all | externalOnly | none; start/end take `date` (all-day, end
  exclusive) or `dateTime` (RFC3339); `transparency` is opaque | transparent.
- v3/reference/events/delete: sendUpdates query parameter, empty body on success.
- guides/errors: error body {"error": {"code", "message", "errors": [{"reason", ...}]}};
  409 `duplicate` when an id exists, 410 `deleted`, 403/429 `rateLimitExceeded`,
  403 `userRateLimitExceeded`, 500 `backendError`, 401 `authError`.

Not documented (so handled defensively, see `_classify`): what events.get returns for a deleted
event. Both 404/410 and a 200 with status "cancelled" count as gone. The exact 403 reason strings
for a missing scope or a disabled API are also not on the errors page; they are matched loosely.

All-day events: Google gives a `date`, not a time. They are returned as Events running from
00:00 to 24:00 on that date in Asia/Kolkata (the product is for people in India).

Only reads are retried (bounded, honouring Retry-After). A create, patch or delete whose outcome is
unknown is never repeated: the caller gets a retryable error, and the engine reads back first.
Nothing here logs titles, attendees or tokens, and Google's text is returned as data only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.integrations.base import (
    CalendarClient,
    CreatedEvent,
    Event,
    EventDraft,
    IntegrationError,
    RateLimited,
    ReauthRequired,
    TokenProvider,
)

PROVIDER = "calendar"
BASE_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
IST = ZoneInfo("Asia/Kolkata")
TIMEOUT = httpx.Timeout(10.0)
MAX_READ_RETRIES = 2
MAX_RETRY_WAIT = 5.0
PAGE_SIZE = 250
MAX_PAGES = 10

_EVENT_ID = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")

_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}


def event_id_for(idempotency_key: str) -> str:
    """A valid Calendar event id (base32hex, lowercase, 52 chars) that is stable for a key."""
    digest = hashlib.sha256(("namma-saathi-event:" + idempotency_key).encode()).digest()
    return base64.b32hexencode(digest).decode().rstrip("=").lower()


def _check_event_id(event_id: str) -> str:
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise IntegrationError("That event id is not valid.", retryable=False)
    return event_id


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise IntegrationError("A calendar time needs a time zone.", retryable=False)
    return value


def _parse_point(point: Any, *, end: bool) -> datetime | None:
    if not isinstance(point, dict):
        return None
    p: dict[str, Any] = point  # pyright: ignore[reportUnknownVariableType]
    date_time = p.get("dateTime")
    if isinstance(date_time, str):
        try:
            parsed = datetime.fromisoformat(date_time)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else None
    day = p.get("date")
    if isinstance(day, str):
        try:
            parsed = datetime.fromisoformat(day).replace(tzinfo=IST)
        except ValueError:
            return None
        # An all-day event's `end.date` is already exclusive (the next midnight).
        return parsed
    return None


def _parse_event(raw: Any) -> Event | None:
    """None for a cancelled event or one we cannot read."""
    if not isinstance(raw, dict):
        return None
    r: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    if r.get("status") == "cancelled":
        return None
    event_id = r.get("id")
    start = _parse_point(r.get("start"), end=False)
    end = _parse_point(r.get("end"), end=True)
    if not isinstance(event_id, str) or start is None or end is None:
        return None
    emails: list[str] = []
    attendees = r.get("attendees")
    if isinstance(attendees, list):
        for a in attendees:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(a, dict):
                email = a.get("email")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(email, str) and email:
                    emails.append(email)
    return Event(
        id=event_id,
        title=str(r.get("summary") or ""),
        start=start,
        end=end,
        attendees=tuple(emails),
        location=str(r.get("location") or ""),
        link=str(r.get("htmlLink") or ""),
    )


def _blocks_time(raw: dict[str, Any]) -> bool:
    if raw.get("transparency") == "transparent":
        return False
    attendees = raw.get("attendees")
    if isinstance(attendees, list):
        for a in attendees:  # pyright: ignore[reportUnknownVariableType]
            if (
                isinstance(a, dict)
                and a.get("self") is True  # pyright: ignore[reportUnknownMemberType]
                and a.get("responseStatus") == "declined"  # pyright: ignore[reportUnknownMemberType]
            ):
                return False
    return True


def _overlaps(ev: Event, start: datetime, end: datetime) -> bool:
    return ev.start < end and ev.end > start


def _reason(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(err, dict):
            errs = err.get("errors")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                reason = errs[0].get("reason")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(reason, str):
                    return reason
            status = err.get("status")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(status, str):
                return status
    return ""


def _message(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(err, dict):
            return str(err.get("message", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return ""


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, min(float(response.headers.get("Retry-After", "1")), 60.0))
    except ValueError:
        return 1.0


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class GoogleCalendarClient(CalendarClient):
    def __init__(
        self, tokens: TokenProvider, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._tokens = tokens
        self._transport = transport

    # -- transport ---------------------------------------------------------------------------

    async def _send(
        self,
        user_id: str,
        method: str,
        event_id: str | None,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        allow: tuple[int, ...] = (),
        read: bool = False,
    ) -> tuple[int, Any]:
        """One logical call: (status, json). Statuses in `allow` are returned, not raised."""
        url = BASE_URL + (f"/{_check_event_id(event_id)}" if event_id is not None else "")
        refreshed = False
        retries = 0
        token = await self._tokens.access_token(user_id, PROVIDER)
        while True:
            try:
                async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT) as http:
                    response = await http.request(
                        method,
                        url,
                        params=params,
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError as exc:
                if read and retries < MAX_READ_RETRIES:
                    retries += 1
                    await _sleep(1.0)
                    continue
                note = "" if read else " The change may or may not have happened."
                raise IntegrationError(
                    f"Google Calendar could not be reached ({type(exc).__name__}).{note}",
                    retryable=True,
                ) from None
            status = response.status_code
            if 200 <= status < 300 or status in allow:
                if status == 204 or not response.content:
                    return status, None
                try:
                    return status, response.json()
                except ValueError:
                    if status in allow:
                        return status, None
                    raise IntegrationError(
                        "Google Calendar sent a reply that could not be read.", retryable=True
                    ) from None
            try:
                data: Any = response.json()
            except ValueError:
                data = None
            if status == 401:
                if refreshed:
                    raise ReauthRequired("Google Calendar access has to be connected again.")
                refreshed = True
                token = await self._tokens.access_token(user_id, PROVIDER)
                continue
            error = self._classify(response, data)
            if error.retryable and read and retries < MAX_READ_RETRIES:
                retries += 1
                wait = error.retry_after if isinstance(error, RateLimited) else 1.0
                await _sleep(min(wait, MAX_RETRY_WAIT))
                continue
            raise error

    @staticmethod
    def _classify(response: httpx.Response, body: Any) -> IntegrationError:
        status = response.status_code
        reason = _reason(body).lower()
        text = _message(body).lower()
        if status == 429 or (status == 403 and reason in _RATE_REASONS):
            return RateLimited(
                "Google Calendar is busy right now.", retry_after=_retry_after(response)
            )
        if status == 403:
            if (
                reason in {"insufficientpermissions", "insufficient_scope", "permission_denied"}
                or "scope" in text
                or "insufficient" in text
            ):
                return ReauthRequired("Google Calendar needs permission to be granted again.")
            if reason in {"accessnotconfigured", "service_disabled"} or "not been used" in text:
                return IntegrationError(
                    "The Google Calendar service is not switched on for this app.",
                    retryable=False,
                )
            return IntegrationError("Google Calendar did not allow that.", retryable=False)
        if status >= 500:
            return IntegrationError("Google Calendar had a problem.", retryable=True)
        if status == 404:
            return IntegrationError("That calendar event was not found.", retryable=False)
        if status == 410:
            return IntegrationError("That calendar event was already deleted.", retryable=False)
        if status == 409:
            return IntegrationError("That calendar event already exists.", retryable=False)
        return IntegrationError("Google Calendar rejected the request.", retryable=False)

    async def _get_raw(self, user_id: str, event_id: str) -> dict[str, Any]:
        _, data = await self._send(user_id, "GET", event_id, read=True)
        if not isinstance(data, dict):
            raise IntegrationError("Google Calendar sent a reply that could not be read.")
        return data  # pyright: ignore[reportUnknownVariableType]

    async def _get_or_gone(self, user_id: str, event_id: str) -> dict[str, Any] | None:
        status, data = await self._send(user_id, "GET", event_id, allow=(404, 410), read=True)
        if status in (404, 410):
            return None
        if not isinstance(data, dict):
            raise IntegrationError("Google Calendar sent a reply that could not be read.")
        return data  # pyright: ignore[reportUnknownVariableType]

    # -- reads -------------------------------------------------------------------------------

    async def _window(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[tuple[Event, dict[str, Any]]]:
        _aware(start)
        _aware(end)
        if end <= start:
            return []
        found: list[tuple[Event, dict[str, Any]]] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            params = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": str(PAGE_SIZE),
                "showDeleted": "false",
            }
            if page_token:
                params["pageToken"] = page_token
            _, data = await self._send(user_id, "GET", None, params=params, read=True)
            if not isinstance(data, dict):
                raise IntegrationError("Google Calendar sent a reply that could not be read.")
            d: dict[str, Any] = data  # pyright: ignore[reportUnknownVariableType]
            items = d.get("items")
            for raw in items if isinstance(items, list) else []:  # pyright: ignore[reportUnknownVariableType]
                ev = _parse_event(raw)
                if ev is not None and _overlaps(ev, start, end):
                    found.append((ev, raw))  # pyright: ignore[reportUnknownArgumentType]
            token = d.get("nextPageToken")
            if not isinstance(token, str) or not token:
                break
            page_token = token
        found.sort(key=lambda pair: pair[0].start)
        return found

    async def events(self, user_id: str, start: datetime, end: datetime) -> list[Event]:
        return [ev for ev, _ in await self._window(user_id, start, end)]

    async def conflicts(self, user_id: str, start: datetime, end: datetime) -> list[Event]:
        return [ev for ev, raw in await self._window(user_id, start, end) if _blocks_time(raw)]

    # -- writes ------------------------------------------------------------------------------

    @staticmethod
    def _matches(ev: Event, draft: EventDraft) -> bool:
        return (
            ev.title == draft.title
            and ev.start == draft.start
            and ev.end == draft.end
            and {a.lower() for a in ev.attendees} == {a.lower() for a in draft.attendees}
        )

    async def create(self, user_id: str, draft: EventDraft, idempotency_key: str) -> CreatedEvent:
        _aware(draft.start)
        _aware(draft.end)
        if not idempotency_key:
            raise IntegrationError("An event needs an idempotency key.", retryable=False)
        event_id = event_id_for(idempotency_key)
        body: dict[str, Any] = {
            "id": event_id,
            "summary": draft.title,
            "start": {"dateTime": draft.start.isoformat()},
            "end": {"dateTime": draft.end.isoformat()},
        }
        if draft.description:
            body["description"] = draft.description
        if draft.attendees:
            body["attendees"] = [{"email": a} for a in draft.attendees]
        notify = draft.notify_attendees and bool(draft.attendees)
        params = {"sendUpdates": "all" if notify else "none"}
        status, _ = await self._send(user_id, "POST", None, params=params, body=body, allow=(409,))
        # 409 duplicate: this key already made the event. Do not insert again; read it back.
        del status
        raw = await self._get_or_gone(user_id, event_id)
        event = _parse_event(raw) if raw is not None else None
        if event is None:
            raise IntegrationError(
                "The event was sent to Google Calendar but could not be read back.",
                retryable=False,
            )
        return CreatedEvent(event=event, verified=self._matches(event, draft))

    async def reschedule(
        self, user_id: str, event_id: str, start: datetime, end: datetime
    ) -> CreatedEvent:
        _check_event_id(event_id)
        _aware(start)
        _aware(end)
        body = {"start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
        await self._send(user_id, "PATCH", event_id, params={"sendUpdates": "none"}, body=body)
        raw = await self._get_or_gone(user_id, event_id)
        event = _parse_event(raw) if raw is not None else None
        if event is None:
            raise IntegrationError("That calendar event was not found.", retryable=False)
        return CreatedEvent(event=event, verified=event.start == start and event.end == end)

    async def cancel(self, user_id: str, event_id: str) -> bool:
        _check_event_id(event_id)
        await self._send(
            user_id, "DELETE", event_id, params={"sendUpdates": "none"}, allow=(404, 410)
        )
        raw = await self._get_or_gone(user_id, event_id)
        return raw is None or raw.get("status") == "cancelled"
