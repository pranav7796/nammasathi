"""Memory and contacts for one signed-in person.

Only the person, through the UI or a spoken "remember that ...", may write memory. Anything else
(email text, web pages, model output, tool results) is refused with PermissionError.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from app.accounts.service import AccountService
from app.memory.store import (
    MEMORY_KEYS,
    Contact,
    MemoryError_,
    MemoryItem,
    MemoryStore,
)
from app.observability import log_event

WRITES_PER_MINUTE = 60
MIN_PREFIX = 2

# Words for family members. These match only a contact saved under that exact word.
RELATIONSHIP_WORDS = frozenset(
    {
        "mom", "mum", "mother", "mummy", "mommy", "amma", "maa", "mata", "dad", "father",
        "papa", "appa", "daddy", "baba", "ajji", "ajja", "thatha", "dadi", "nani", "dada",
        "nana", "bhai", "brother", "didi", "sister", "akka", "anna", "tamma", "wife",
        "husband", "son", "daughter",
    }
)  # fmt: skip


class WriteLimited(MemoryError_):
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


@dataclass(frozen=True)
class ContactMatch:
    kind: Literal["one", "none", "ambiguous"]
    contact: Contact | None = None
    candidates: tuple[Contact, ...] = ()


def match_contact(contacts: list[Contact], spoken_name: str) -> ContactMatch:
    """Exact (case-insensitive) first, then a unique prefix or first name. Never a fuzzy guess."""
    spoken = " ".join(spoken_name.casefold().split())
    if spoken.startswith("my "):
        spoken = spoken[3:].strip()
    if not spoken:
        return ContactMatch("none")
    exact = [c for c in contacts if c.name.casefold() == spoken]
    if exact:
        return ContactMatch("one", exact[0], (exact[0],))
    if spoken in RELATIONSHIP_WORDS or len(spoken) < MIN_PREFIX:
        return ContactMatch("none")
    found = [
        c
        for c in contacts
        if c.name.casefold().startswith(spoken) or c.name.casefold().split()[0] == spoken
    ]
    if len(found) == 1:
        return ContactMatch("one", found[0], (found[0],))
    if found:
        return ContactMatch("ambiguous", None, tuple(found))
    return ContactMatch("none")


class MemoryService:
    """Every method takes the user id of the signed-in person, never one from a request body."""

    def __init__(self, accounts: AccountService, now: Callable[[], float] = time.time) -> None:
        self.accounts = accounts
        self.store = MemoryStore(accounts.database)
        self._now = now
        self.limiter = WriteLimiter(clock=now)

    def _ms(self) -> int:
        return int(self._now() * 1000)

    @staticmethod
    def _only_user(source: str) -> None:
        if source != "user":
            raise PermissionError("Only the person can change what Saathi remembers.")

    # Memory

    async def list_items(self, user_id: str) -> list[MemoryItem]:
        return await self._run(lambda: self.store.list_items(user_id), "memory_list")

    async def get_item(self, user_id: str, key: str) -> MemoryItem | None:
        return await self._run(lambda: self.store.get_item(user_id, key), "memory_get")

    async def set_item(
        self, user_id: str, key: str, value: str, *, source: str = "user"
    ) -> MemoryItem:
        self._only_user(source)
        self.limiter.check(user_id)
        return await self._run(
            lambda: self.store.set_item(user_id, key, value, self._ms()), "memory_set"
        )

    async def forget(self, user_id: str, key: str, *, source: str = "user") -> bool:
        self._only_user(source)
        self.limiter.check(user_id)
        return await self._run(lambda: self.store.forget(user_id, key), "memory_forget")

    async def forget_all(self, user_id: str, *, source: str = "user") -> None:
        self._only_user(source)
        self.limiter.check(user_id)
        await self._run(lambda: self.store.forget_all(user_id), "memory_forget_all")

    async def preferred_language(self, user_id: str) -> str | None:
        item = await self.get_item(user_id, "response_language")
        return None if item is None else item.value

    async def memory_context(self, user_id: str) -> dict[str, str]:
        """Allow-listed remembered values only, for the replying code. Never contact details."""
        items = await self.list_items(user_id)
        return {i.key: i.value for i in items if i.key in MEMORY_KEYS}

    # Contacts

    async def create_contact(
        self,
        user_id: str,
        name: str,
        email: str | None = None,
        phone: str | None = None,
        *,
        source: str = "user",
    ) -> Contact:
        self._only_user(source)
        self.limiter.check(user_id)
        return await self._run(
            lambda: self.store.create_contact(user_id, name, email, phone, self._ms()),
            "contact_create",
        )

    async def list_contacts(self, user_id: str) -> list[Contact]:
        return await self._run(lambda: self.store.list_contacts(user_id), "contact_list")

    async def update_contact(
        self,
        user_id: str,
        contact_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        change_email: bool = False,
        change_phone: bool = False,
        source: str = "user",
    ) -> Contact:
        self._only_user(source)
        self.limiter.check(user_id)
        return await self._run(
            lambda: self.store.update_contact(
                user_id,
                contact_id,
                name=name,
                email=email,
                phone=phone,
                change_email=change_email,
                change_phone=change_phone,
            ),
            "contact_update",
        )

    async def delete_contact(self, user_id: str, contact_id: str, *, source: str = "user") -> None:
        self._only_user(source)
        self.limiter.check(user_id)
        await self._run(lambda: self.store.delete_contact(user_id, contact_id), "contact_delete")

    async def resolve_contact(self, user_id: str, spoken_name: str) -> ContactMatch:
        contacts = await self.list_contacts(user_id)
        return match_contact(contacts, spoken_name)

    async def _run[T](self, call: Callable[[], T], op: str) -> T:
        """Off the event loop. No remembered text or contact detail is ever logged."""

        def guarded() -> tuple[T | None, MemoryError_ | None]:
            try:
                return call(), None
            except MemoryError_ as exc:
                return None, exc

        result, error = await self.accounts.run(guarded)
        if error is not None:
            log_event("memory", op=op, outcome=type(error).__name__)
            raise error
        log_event("memory", op=op, outcome="ok")
        return cast(T, result)
