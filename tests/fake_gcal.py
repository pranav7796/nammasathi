"""In-process fake of the Google Calendar v3 events endpoints used by app/google/calendar.py.

Built from the official reference (events list/get/insert/patch/delete and guides/errors). It
never touches the network: hand `FakeGCal().transport()` to the client. Behaviour that the docs
do not state (what `get` returns for a deleted event) is selectable with `deleted_get`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

IST = ZoneInfo("Asia/Kolkata")
_ID = re.compile(r"^[a-v0-9]{5,1024}$")  # base32hex, from the insert reference
_PATH = re.compile(r"^/calendar/v3/calendars/primary/events(?:/([^/]+))?$")


@dataclass
class Failure:
    status: int
    reason: str = ""
    message: str = "failure"
    method: str | None = None
    headers: dict[str, str] = field(default_factory=lambda: {})


def _point(p: dict[str, Any]) -> datetime:
    if "dateTime" in p:
        return datetime.fromisoformat(p["dateTime"])
    return datetime.fromisoformat(p["date"]).replace(tzinfo=IST)


class FakeGCal:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.failures: list[Failure] = []
        self.silent_delete = False  # delete answers 204 but keeps the event
        self.corrupt_insert = False  # insert answers success but stores a different title
        self.corrupt_patch = False  # patch answers success but changes nothing
        self.deleted_get = "404"  # what get returns for a deleted event: 404, 410, cancelled
        self._deleted: set[str] = set()
        self._seq = 0
        # Which access tokens Google would accept. A test that also runs the fake OAuth server
        # points this at the tokens that server issued.
        self.accept_token: Callable[[str], bool] = lambda token: token == "good-token"

    # -- setup helpers -----------------------------------------------------------------------

    def add(self, **fields: Any) -> str:
        self._seq += 1
        event_id = fields.pop("id", f"seed{self._seq:04d}")
        self.events[event_id] = {
            "id": event_id,
            "status": "confirmed",
            "htmlLink": f"https://calendar.example/e/{event_id}",
            **fields,
        }
        return event_id

    def fail(self, status: int, reason: str = "", method: str | None = None, **kw: Any) -> None:
        self.failures.append(Failure(status, reason, method=method, **kw))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def sent(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]

    # -- server ------------------------------------------------------------------------------

    @staticmethod
    def _error(status: int, reason: str, message: str, headers: dict[str, str] | None = None):
        body = {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"domain": "calendar", "reason": reason, "message": message}],
            }
        }
        return httpx.Response(status, json=body, headers=headers or {})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for i, f in enumerate(self.failures):
            if f.method is None or f.method == request.method:
                del self.failures[i]
                return self._error(f.status, f.reason, f.message, f.headers)
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not self.accept_token(bearer):
            return self._error(401, "authError", "Invalid Credentials")
        m = _PATH.match(request.url.path)
        if not m:
            return self._error(404, "notFound", "Not Found")
        event_id = m.group(1)
        q = dict(request.url.params)
        if event_id is None:
            if request.method == "GET":
                return self._list(q)
            if request.method == "POST":
                return self._insert(request)
        else:
            if request.method == "GET":
                return self._get(event_id)
            if request.method == "PATCH":
                return self._patch(event_id, request)
            if request.method == "DELETE":
                return self._delete(event_id)
        return self._error(405, "methodNotAllowed", "Method not allowed")

    def _list(self, q: dict[str, str]) -> httpx.Response:
        tmin = datetime.fromisoformat(q["timeMin"])
        tmax = datetime.fromisoformat(q["timeMax"])
        assert q.get("singleEvents") == "true" and q.get("orderBy") == "startTime"
        items = [
            e
            for e in self.events.values()
            if e["id"] not in self._deleted
            and e.get("status") != "cancelled"
            and _point(e["end"]) > tmin
            and _point(e["start"]) < tmax
        ]
        items.sort(key=lambda e: _point(e["start"]))
        return httpx.Response(200, json={"kind": "calendar#events", "items": items})

    def _insert(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        event_id = body.get("id")
        if event_id is not None and not _ID.fullmatch(event_id):
            return self._error(400, "invalid", "Invalid resource id value.")
        if event_id in self.events:
            return self._error(409, "duplicate", "The requested identifier already exists.")
        stored = {k: v for k, v in body.items() if k != "id"}
        if self.corrupt_insert:
            stored["summary"] = "something else"
        if "attendees" in stored:
            stored["attendees"] = [
                {"responseStatus": "needsAction", **a} for a in stored["attendees"]
            ]
        new_id = self.add(id=event_id, **stored)
        return httpx.Response(200, json=self.events[new_id])

    def _lookup(self, event_id: str) -> httpx.Response | None:
        if event_id in self._deleted:
            if self.deleted_get == "cancelled":
                return httpx.Response(200, json={"id": event_id, "status": "cancelled"})
            code = 410 if self.deleted_get == "410" else 404
            return self._error(code, "deleted" if code == 410 else "notFound", "gone")
        if event_id not in self.events:
            return self._error(404, "notFound", "Not Found")
        return None

    def _get(self, event_id: str) -> httpx.Response:
        return self._lookup(event_id) or httpx.Response(200, json=self.events[event_id])

    def _patch(self, event_id: str, request: httpx.Request) -> httpx.Response:
        gone = self._lookup(event_id)
        if gone:
            return gone
        if not self.corrupt_patch:
            for key, value in json.loads(request.content).items():
                self.events[event_id][key] = value
        return httpx.Response(200, json=self.events[event_id])

    def _delete(self, event_id: str) -> httpx.Response:
        if event_id in self._deleted:
            return self._error(410, "deleted", "Resource has been deleted")
        if event_id not in self.events:
            return self._error(404, "notFound", "Not Found")
        if not self.silent_delete:
            self._deleted.add(event_id)
        return httpx.Response(204)
