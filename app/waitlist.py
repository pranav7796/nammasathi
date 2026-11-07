"""Public waitlist: people who want an email when live ordering opens.

Only stores what the form sends. Nothing is emailed from here, there is no public read endpoint,
and the address is never logged. Operators read and remove entries with `app.accounts_admin`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Literal, cast

import psycopg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.accounts.service import AccountService
from app.observability import log_event

CONSENT_TEXT = (
    "Tell me by email when live ordering opens. I can ask you to remove my email at any time."
)
MAX_ENTRIES = 20_000
PER_ADDRESS_PER_HOUR = 5
PER_HOUR = 300
WINDOW_SECONDS = 60 * 60
_MAX_TRACKED = 20_000
_EMAIL = re.compile(
    r"^[a-z0-9._%+\-']+@[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?)+$"
)

Interest = Literal["groceries", "food", "dining", "reminders", "other"]
Language = Literal["kn-IN", "hi-IN", "en-IN"]

FULL_MESSAGE = "The waitlist is full for now. Please try again later."
BUSY_MESSAGE = "Too many sign-ups just now. Please try again in a while."


class WaitlistRequest(BaseModel):
    email: str = Field(max_length=254)
    interests: list[Interest] = Field(default_factory=list[Interest], max_length=5)
    language: Language = "en-IN"
    consent: bool = False


def clean_email(raw: str) -> str | None:
    """Lower-cased, trimmed address, or None if it is not plausibly an email."""
    email = raw.strip().lower()
    if not email or len(email) > 254 or not _EMAIL.match(email):
        return None
    return email


class WaitlistLimiter:
    """In-memory, per process: attempts per address and site-wide, per hour."""

    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - WINDOW_SECONDS
        kept = [moment for moment in self._events.get(key, []) if moment > cutoff]
        if kept:
            self._events[key] = kept
        else:
            self._events.pop(key, None)
        return kept

    def allow(self, address: str) -> bool:
        """Record an attempt; False if it is over a limit."""
        if len(self._events) > _MAX_TRACKED:
            self._events.clear()
        if (
            len(self._recent(f"a:{address}")) >= PER_ADDRESS_PER_HOUR
            or len(self._recent("*")) >= PER_HOUR
        ):
            return False
        now = time.monotonic()
        self._events.setdefault(f"a:{address}", []).append(now)
        self._events.setdefault("*", []).append(now)
        return True


def _is_duplicate(exc: Exception) -> bool:
    return isinstance(exc, (sqlite3.IntegrityError, psycopg.errors.UniqueViolation))


def _add(service: AccountService, email: str, interests: list[str], language: str) -> str:
    """'joined', 'already_listed', or 'full'. Runs in a worker thread."""
    entry = (
        uuid.uuid4().hex,
        email,
        json.dumps(interests),
        language,
        CONSENT_TEXT,
        int(time.time() * 1000),
    )
    try:
        with service.database.transaction() as tx:
            if tx.execute("SELECT 1 AS found FROM waitlist_entries WHERE email = ?", (email,)):
                return "already_listed"
            count = tx.execute("SELECT COUNT(*) AS n FROM waitlist_entries")
            if int(str(count[0]["n"])) >= MAX_ENTRIES:
                return "full"
            tx.execute(
                "INSERT INTO waitlist_entries "
                "(id, email, interests, language, consent_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                entry,
            )
    except Exception as exc:
        if _is_duplicate(exc):  # two requests raced; the other one won
            return "already_listed"
        raise
    return "joined"


def _service(request: Request) -> AccountService:
    service = cast(AccountService | None, getattr(request.app.state, "accounts", None))
    if service is None:
        raise HTTPException(status_code=404, detail="The waitlist is not available here.")
    return service


def waitlist_router(accounts: AccountService | None = None) -> APIRouter:
    """With no argument, the service is the one the app started with (404 if accounts are off)."""
    router = APIRouter(prefix="/api/waitlist")
    limiter = WaitlistLimiter()

    @router.post("")
    async def join(body: WaitlistRequest, request: Request) -> dict[str, str]:
        address = request.client.host if request.client else ""
        if not limiter.allow(address):
            log_event("waitlist", outcome="rate_limited")
            raise HTTPException(status_code=429, detail=BUSY_MESSAGE)
        if not body.consent:
            raise HTTPException(status_code=400, detail="Please tick the box to join the list.")
        email = clean_email(body.email)
        if email is None:
            raise HTTPException(status_code=400, detail="That email address does not look right.")
        interests = list(dict.fromkeys(body.interests))
        # service.run turns any database failure into AccountsUnavailable (a 503 in web.py).
        service = accounts or _service(request)
        outcome = await service.run(_add, service, email, interests, body.language)
        log_event("waitlist", outcome=outcome)
        if outcome == "full":
            raise HTTPException(status_code=503, detail=FULL_MESSAGE)
        return {"status": outcome}

    return router
