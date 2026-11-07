"""What a person asked Saathi to keep, and their contacts. Every query is scoped to one user_id.

Memory is not conversation history. Only the allow-listed keys below can be stored, values that
look like secrets or account numbers are refused, and nothing here is ever sent to a log.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from uuid import uuid4

from app.accounts.db import Database, Row, Transaction

MAX_CONTACTS = 200
MAX_CONTACT_NAME = 80
LANGUAGES = ("kn-IN", "hi-IN", "en-IN")

# key -> (label shown to the person, maximum characters)
MEMORY_KEYS: dict[str, tuple[str, int]] = {
    "response_language": ("Reply language", 5),
    "dietary": ("Food preferences", 200),
    "reminder_style": ("How to remind me", 100),
    "frequent_services": ("Services I use most", 200),
    "home_area": ("My area", 100),
    "greeting_name": ("What to call me", 60),
}

_DIGIT_RUN = re.compile(r"\d(?:[ \-]?\d){11,}")
_TOKEN = re.compile(r"[A-Za-z0-9]{24,}")
_SECRET_WORDS = re.compile(
    r"\b(?:password|passwd|passcode|otp|pin is|pin number|api[ _-]?key|secret|cvv)\b", re.I
)
_EMAIL = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}$")
_PHONE = re.compile(r"^\+[1-9]\d{7,14}$")


class MemoryError_(Exception):
    """A request the person can fix. The message is safe to show and never repeats their text."""


class InvalidValue(MemoryError_):
    pass


class UnknownKey(InvalidValue):
    pass


class SensitiveValue(InvalidValue):
    pass


class LimitReached(MemoryError_):
    pass


class DuplicateName(MemoryError_):
    pass


class NotFound(MemoryError_):
    pass


@dataclass(frozen=True)
class MemoryItem:
    key: str
    value: str
    updated_at: int


@dataclass(frozen=True)
class Contact:
    id: str
    name: str
    email: str | None
    phone: str | None
    created_at: int


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def clean_value(key: str, value: str) -> str:
    """The trimmed value for an allow-listed key, or an error that does not echo the value."""
    if key not in MEMORY_KEYS:
        raise UnknownKey("Saathi cannot remember that kind of thing.")
    label, limit = MEMORY_KEYS[key]
    text = value.strip()
    if not text:
        raise InvalidValue(f"{label} is empty.")
    if _has_control(text):
        raise InvalidValue(f"{label} has characters that are not allowed.")
    if len(text) > limit:
        raise InvalidValue(f"{label} can be at most {limit} characters.")
    if key == "response_language" and text not in LANGUAGES:
        raise InvalidValue("Choose Kannada, Hindi or English.")
    if _DIGIT_RUN.search(text) or _TOKEN.search(text) or _SECRET_WORDS.search(text):
        raise SensitiveValue(
            "That looks like a number, password or code. Saathi does not keep those."
        )
    return text


def clean_name(name: str) -> str:
    text = name.strip()
    if not text or len(text) > MAX_CONTACT_NAME:
        raise InvalidValue(f"A name needs 1 to {MAX_CONTACT_NAME} characters.")
    if _has_control(text):
        raise InvalidValue("The name has characters that are not allowed.")
    return text


def clean_email(email: str | None) -> str | None:
    if email is None or not email.strip():
        return None
    text = email.strip().lower()
    if len(text) > 254 or not _EMAIL.match(text):
        raise InvalidValue("That email address does not look right.")
    return text


def clean_phone(phone: str | None) -> str | None:
    if phone is None or not phone.strip():
        return None
    text = re.sub(r"[ \-]", "", phone.strip())
    if not _PHONE.match(text):
        raise InvalidValue("Use a phone number with country code, like +919876543210.")
    return text


def _int(row: Row, key: str) -> int:
    value = row[key]
    assert isinstance(value, int)
    return value


def _opt(row: Row, key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


def _contact(row: Row) -> Contact:
    return Contact(
        id=str(row["id"]),
        name=str(row["name"]),
        email=_opt(row, "email"),
        phone=_opt(row, "phone"),
        created_at=_int(row, "created_at"),
    )


def _now(now_ms: int | None) -> int:
    return int(time.time() * 1000) if now_ms is None else now_ms


class MemoryStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    # Remembered items

    def list_items(self, user_id: str) -> list[MemoryItem]:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT key, value, updated_at FROM memory_items WHERE user_id = ? ORDER BY key",
                (user_id,),
            )
        return [MemoryItem(str(r["key"]), str(r["value"]), _int(r, "updated_at")) for r in rows]

    def get_item(self, user_id: str, key: str) -> MemoryItem | None:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT key, value, updated_at FROM memory_items WHERE user_id = ? AND key = ?",
                (user_id, key),
            )
        if not rows:
            return None
        r = rows[0]
        return MemoryItem(str(r["key"]), str(r["value"]), _int(r, "updated_at"))

    def set_item(self, user_id: str, key: str, value: str, now_ms: int | None = None) -> MemoryItem:
        text = clean_value(key, value)
        now = _now(now_ms)
        with self.database.transaction() as tx:
            tx.lock_user(user_id)
            tx.execute("DELETE FROM memory_items WHERE user_id = ? AND key = ?", (user_id, key))
            tx.execute(
                "INSERT INTO memory_items (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, key, text, now),
            )
        return MemoryItem(key, text, now)

    def forget(self, user_id: str, key: str) -> bool:
        if key not in MEMORY_KEYS:
            raise UnknownKey("Saathi cannot remember that kind of thing.")
        with self.database.transaction() as tx:
            found = tx.execute(
                "SELECT key FROM memory_items WHERE user_id = ? AND key = ?", (user_id, key)
            )
            tx.execute("DELETE FROM memory_items WHERE user_id = ? AND key = ?", (user_id, key))
        return bool(found)

    def forget_all(self, user_id: str) -> None:
        with self.database.transaction() as tx:
            tx.execute("DELETE FROM memory_items WHERE user_id = ?", (user_id,))

    # Contacts

    def create_contact(
        self,
        user_id: str,
        name: str,
        email: str | None = None,
        phone: str | None = None,
        now_ms: int | None = None,
    ) -> Contact:
        name = clean_name(name)
        email, phone = clean_email(email), clean_phone(phone)
        if email is None and phone is None:
            raise InvalidValue("Add an email address or a phone number.")
        contact_id = uuid4().hex
        with self.database.transaction() as tx:
            tx.lock_user(user_id)
            rows = tx.execute("SELECT id, name FROM contacts WHERE user_id = ?", (user_id,))
            if len(rows) >= MAX_CONTACTS:
                raise LimitReached(f"You can keep up to {MAX_CONTACTS} contacts. Delete some.")
            if any(str(r["name"]).casefold() == name.casefold() for r in rows):
                raise DuplicateName("You already have a contact with that name.")
            tx.execute(
                "INSERT INTO contacts (id, user_id, name, email, phone, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (contact_id, user_id, name, email, phone, _now(now_ms)),
            )
            return _contact(self._row(tx, user_id, contact_id))

    def list_contacts(self, user_id: str) -> list[Contact]:
        with self.database.transaction() as tx:
            rows = tx.execute("SELECT * FROM contacts WHERE user_id = ?", (user_id,))
        return sorted((_contact(r) for r in rows), key=lambda c: (c.name.casefold(), c.id))

    def get_contact(self, user_id: str, contact_id: str) -> Contact:
        with self.database.transaction() as tx:
            return _contact(self._row(tx, user_id, contact_id))

    def update_contact(
        self,
        user_id: str,
        contact_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        change_email: bool = False,
        change_phone: bool = False,
    ) -> Contact:
        new_name = None if name is None else clean_name(name)
        with self.database.transaction() as tx:
            tx.lock_user(user_id)
            current = _contact(self._row(tx, user_id, contact_id))
            final_name = current.name if new_name is None else new_name
            final_email = clean_email(email) if change_email else current.email
            final_phone = clean_phone(phone) if change_phone else current.phone
            if final_email is None and final_phone is None:
                raise InvalidValue("Keep an email address or a phone number.")
            others = tx.execute(
                "SELECT id, name FROM contacts WHERE user_id = ? AND id <> ?",
                (user_id, contact_id),
            )
            if any(str(r["name"]).casefold() == final_name.casefold() for r in others):
                raise DuplicateName("You already have a contact with that name.")
            tx.execute(
                "UPDATE contacts SET name = ?, email = ?, phone = ? WHERE id = ? AND user_id = ?",
                (final_name, final_email, final_phone, contact_id, user_id),
            )
            return _contact(self._row(tx, user_id, contact_id))

    def delete_contact(self, user_id: str, contact_id: str) -> None:
        with self.database.transaction() as tx:
            self._row(tx, user_id, contact_id)
            tx.execute("DELETE FROM contacts WHERE id = ? AND user_id = ?", (contact_id, user_id))

    @staticmethod
    def _row(tx: Transaction, user_id: str, contact_id: str) -> Row:
        rows = tx.execute(
            "SELECT * FROM contacts WHERE id = ? AND user_id = ?", (contact_id, user_id)
        )
        if not rows:
            raise NotFound("No such contact.")
        row: Row = rows[0]
        return row
