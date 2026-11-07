"""Reminders and notes for one signed-in person, over the accounts database."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime
from typing import cast

from app.accounts.service import AccountService
from app.observability import log_event
from app.personal.dates import IST, parse_when
from app.personal.store import Note, PersonalError, PersonalStore, Reminder

DUPLICATE_WINDOW_SECONDS = 60
WRITES_PER_MINUTE = 60


class WriteLimited(PersonalError):
    pass


class WriteLimiter:
    """At most `limit` writes per user per sliding minute, in memory."""

    def __init__(self, limit: int = WRITES_PER_MINUTE, clock: Callable[[], float] = time.time):
        self.limit = limit
        self._clock = clock
        self._seen: defaultdict[str, deque[float]] = defaultdict(deque)

    def check(self, user_id: str) -> None:
        now = self._clock()
        seen = self._seen[user_id]
        while seen and seen[0] <= now - 60:
            seen.popleft()
        if len(seen) >= self.limit:
            raise WriteLimited("Too many changes too fast. Try again in a minute.")
        seen.append(now)
        if len(self._seen) > 10_000:
            for key in [k for k, v in self._seen.items() if not v]:
                del self._seen[key]


def to_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, IST)


class PersonalService:
    """Every method takes the user id of the signed-in person, never one from a request body."""

    def __init__(self, accounts: AccountService, now: Callable[[], float] = time.time) -> None:
        self.accounts = accounts
        self.store = PersonalStore(accounts.database)
        self._now = now
        self.limiter = WriteLimiter(clock=now)

    def _ms(self) -> int:
        return int(self._now() * 1000)

    # Reminders

    async def create_reminder(
        self, user_id: str, text: str, due: datetime | None, *, when_text: str | None = None
    ) -> Reminder:
        """Make a reminder; an identical one made in the last minute is returned instead."""
        if due is None and when_text:
            parsed = parse_when(when_text, from_ms(self._ms()))
            if parsed is not None:
                due = parsed.due
        return await self._create(user_id, text, due)

    async def create_reminder_from_text(self, user_id: str, text: str) -> Reminder:
        """ "Call mom tomorrow at 7 pm": the time phrase is taken out and becomes the due time."""
        original = text.strip()
        parsed = parse_when(original, from_ms(self._ms()))
        if parsed is None:
            return await self._create(user_id, original, None)
        return await self._create(user_id, parsed.text or original, parsed.due)

    async def _create(self, user_id: str, text: str, due: datetime | None) -> Reminder:
        due_ms = None if due is None else to_ms(due)
        now_ms = self._ms()
        since = now_ms - DUPLICATE_WINDOW_SECONDS * 1000

        def work() -> Reminder:
            existing = self.store.find_recent_duplicate(user_id, text, due_ms, since)
            if existing is not None:
                return existing
            self.limiter.check(user_id)
            return self.store.create_reminder(user_id, text, due_ms, now_ms)

        return await self._run(work, "reminder_create")

    async def list_reminders(self, user_id: str) -> list[Reminder]:
        return await self._run(lambda: self.store.list_reminders(user_id), "reminder_list")

    async def get_reminder(self, user_id: str, reminder_id: str) -> Reminder:
        return await self._run(
            lambda: self.store.get_reminder(user_id, reminder_id), "reminder_get"
        )

    async def edit_reminder(
        self,
        user_id: str,
        reminder_id: str,
        *,
        text: str | None = None,
        due: datetime | None = None,
        change_due: bool = False,
    ) -> Reminder:
        self.limiter.check(user_id)
        return await self._run(
            lambda: self.store.update_reminder(
                user_id,
                reminder_id,
                text=text,
                due_at=None if due is None else to_ms(due),
                change_due=change_due,
                now_ms=self._ms(),
            ),
            "reminder_edit",
        )

    async def complete_reminder(self, user_id: str, reminder_id: str) -> Reminder:
        self.limiter.check(user_id)
        return await self._run(
            lambda: self.store.complete_reminder(user_id, reminder_id, self._ms()),
            "reminder_done",
        )

    async def uncomplete_reminder(self, user_id: str, reminder_id: str) -> Reminder:
        self.limiter.check(user_id)
        return await self._run(
            lambda: self.store.uncomplete_reminder(user_id, reminder_id, self._ms()),
            "reminder_undo",
        )

    async def delete_reminder(self, user_id: str, reminder_id: str) -> None:
        self.limiter.check(user_id)
        await self._run(lambda: self.store.delete_reminder(user_id, reminder_id), "reminder_delete")

    # Notes

    async def create_note(self, user_id: str, body: str) -> Note:
        self.limiter.check(user_id)
        return await self._run(
            lambda: self.store.create_note(user_id, body, self._ms()), "note_create"
        )

    async def list_notes(self, user_id: str) -> list[Note]:
        return await self._run(lambda: self.store.list_notes(user_id), "note_list")

    async def edit_note(self, user_id: str, note_id: str, body: str) -> Note:
        self.limiter.check(user_id)
        return await self._run(
            lambda: self.store.update_note(user_id, note_id, body, self._ms()), "note_edit"
        )

    async def delete_note(self, user_id: str, note_id: str) -> None:
        self.limiter.check(user_id)
        await self._run(lambda: self.store.delete_note(user_id, note_id), "note_delete")

    async def _run[T](self, call: Callable[[], T], op: str) -> T:
        """Off the event loop. Database failures become AccountsUnavailable; no text is logged."""

        def guarded() -> tuple[T | None, PersonalError | None]:
            # AccountService.run turns every exception into "database unavailable", so the
            # person's own mistakes travel back as values.
            try:
                return call(), None
            except PersonalError as exc:
                return None, exc

        result, error = await self.accounts.run(guarded)
        if error is not None:
            log_event("personal", op=op, outcome=type(error).__name__)
            raise error
        log_event("personal", op=op, outcome="ok")
        return cast(T, result)
