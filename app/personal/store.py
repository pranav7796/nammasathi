"""Reminders and notes in the accounts database. Every query is scoped to one user_id."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import uuid4

from app.accounts.db import Database, Row, Transaction

MAX_REMINDER_TEXT = 500
MAX_NOTE_BODY = 4000
MAX_REMINDERS = 200
MAX_NOTES = 500


class PersonalError(Exception):
    """A request the person can fix. The message is safe to show."""


class InvalidText(PersonalError):
    pass


class LimitReached(PersonalError):
    pass


class NotFound(PersonalError):
    pass


@dataclass(frozen=True)
class Reminder:
    id: str
    text: str
    due_at: int | None
    done_at: int | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Note:
    id: str
    body: str
    created_at: int
    updated_at: int


def clean_text(value: str, limit: int, what: str) -> str:
    """Trimmed text of 1..limit characters. Newlines and tabs are allowed in notes only."""
    text = value.strip()
    if not text:
        raise InvalidText(f"The {what} is empty.")
    if len(text) > limit:
        raise InvalidText(f"The {what} can be at most {limit} characters.")
    allowed = "\n\t" if what == "note" else ""
    if any((ord(c) < 32 or ord(c) == 127) and c not in allowed for c in text):
        raise InvalidText(f"The {what} has characters that are not allowed.")
    return text


def _int(row: Row, key: str) -> int:
    value = row[key]
    assert isinstance(value, int)
    return value


def _optional(row: Row, key: str) -> int | None:
    value = row[key]
    return None if value is None else _int(row, key)


def _reminder(row: Row) -> Reminder:
    return Reminder(
        id=str(row["id"]),
        text=str(row["text"]),
        due_at=_optional(row, "due_at"),
        done_at=_optional(row, "done_at"),
        created_at=_int(row, "created_at"),
        updated_at=_int(row, "updated_at"),
    )


def _note(row: Row) -> Note:
    return Note(
        id=str(row["id"]),
        body=str(row["body"]),
        created_at=_int(row, "created_at"),
        updated_at=_int(row, "updated_at"),
    )


class PersonalStore:
    """Timestamps are epoch milliseconds, supplied by the caller so tests need no clock."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # Reminders

    def create_reminder(
        self, user_id: str, text: str, due_at: int | None, now_ms: int | None = None
    ) -> Reminder:
        text = clean_text(text, MAX_REMINDER_TEXT, "reminder")
        now = _now(now_ms)
        reminder_id = uuid4().hex
        with self.database.transaction() as tx:
            tx.lock_user(user_id)
            count = tx.execute("SELECT COUNT(*) AS n FROM reminders WHERE user_id = ?", (user_id,))
            if _int(count[0], "n") >= MAX_REMINDERS:
                raise LimitReached(f"You can keep up to {MAX_REMINDERS} reminders. Delete some.")
            tx.execute(
                "INSERT INTO reminders (id, user_id, text, due_at, done_at, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (reminder_id, user_id, text, due_at, now, now),
            )
            return _reminder(self._reminder_row(tx, user_id, reminder_id))

    def find_recent_duplicate(
        self, user_id: str, text: str, due_at: int | None, since_ms: int
    ) -> Reminder | None:
        text = text.strip()
        with self.database.transaction() as tx:
            if due_at is None:
                rows = tx.execute(
                    "SELECT * FROM reminders WHERE user_id = ? AND text = ? AND due_at IS NULL "
                    "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                    (user_id, text, since_ms),
                )
            else:
                rows = tx.execute(
                    "SELECT * FROM reminders WHERE user_id = ? AND text = ? AND due_at = ? "
                    "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                    (user_id, text, due_at, since_ms),
                )
        return _reminder(rows[0]) if rows else None

    def list_reminders(self, user_id: str) -> list[Reminder]:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM reminders WHERE user_id = ? AND done_at IS NULL "
                "ORDER BY (due_at IS NULL), due_at, created_at",
                (user_id,),
            )
            rows += tx.execute(
                "SELECT * FROM reminders WHERE user_id = ? AND done_at IS NOT NULL "
                "ORDER BY done_at DESC, created_at DESC",
                (user_id,),
            )
        return [_reminder(row) for row in rows]

    def get_reminder(self, user_id: str, reminder_id: str) -> Reminder:
        with self.database.transaction() as tx:
            return _reminder(self._reminder_row(tx, user_id, reminder_id))

    def update_reminder(
        self,
        user_id: str,
        reminder_id: str,
        *,
        text: str | None = None,
        due_at: int | None = None,
        change_due: bool = False,
        now_ms: int | None = None,
    ) -> Reminder:
        new_text = None if text is None else clean_text(text, MAX_REMINDER_TEXT, "reminder")
        with self.database.transaction() as tx:
            current = _reminder(self._reminder_row(tx, user_id, reminder_id))
            tx.execute(
                "UPDATE reminders SET text = ?, due_at = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (
                    current.text if new_text is None else new_text,
                    due_at if change_due else current.due_at,
                    _now(now_ms),
                    reminder_id,
                    user_id,
                ),
            )
            return _reminder(self._reminder_row(tx, user_id, reminder_id))

    def complete_reminder(
        self, user_id: str, reminder_id: str, now_ms: int | None = None
    ) -> Reminder:
        return self._set_done(user_id, reminder_id, _now(now_ms), now_ms)

    def uncomplete_reminder(
        self, user_id: str, reminder_id: str, now_ms: int | None = None
    ) -> Reminder:
        return self._set_done(user_id, reminder_id, None, now_ms)

    def delete_reminder(self, user_id: str, reminder_id: str) -> None:
        with self.database.transaction() as tx:
            self._reminder_row(tx, user_id, reminder_id)
            tx.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))

    def _set_done(
        self, user_id: str, reminder_id: str, done_at: int | None, now_ms: int | None
    ) -> Reminder:
        with self.database.transaction() as tx:
            self._reminder_row(tx, user_id, reminder_id)
            tx.execute(
                "UPDATE reminders SET done_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (done_at, _now(now_ms), reminder_id, user_id),
            )
            return _reminder(self._reminder_row(tx, user_id, reminder_id))

    @staticmethod
    def _reminder_row(tx: Transaction, user_id: str, reminder_id: str) -> Row:
        rows = tx.execute(
            "SELECT * FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
        )
        if not rows:
            raise NotFound("No such reminder.")
        return rows[0]

    # Notes

    def create_note(self, user_id: str, body: str, now_ms: int | None = None) -> Note:
        body = clean_text(body, MAX_NOTE_BODY, "note")
        now = _now(now_ms)
        note_id = uuid4().hex
        with self.database.transaction() as tx:
            tx.lock_user(user_id)
            count = tx.execute("SELECT COUNT(*) AS n FROM notes WHERE user_id = ?", (user_id,))
            if _int(count[0], "n") >= MAX_NOTES:
                raise LimitReached(f"You can keep up to {MAX_NOTES} notes. Delete some.")
            tx.execute(
                "INSERT INTO notes (id, user_id, body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (note_id, user_id, body, now, now),
            )
            return _note(self._note_row(tx, user_id, note_id))

    def list_notes(self, user_id: str) -> list[Note]:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM notes WHERE user_id = ? ORDER BY updated_at DESC, created_at DESC",
                (user_id,),
            )
        return [_note(row) for row in rows]

    def get_note(self, user_id: str, note_id: str) -> Note:
        with self.database.transaction() as tx:
            return _note(self._note_row(tx, user_id, note_id))

    def update_note(self, user_id: str, note_id: str, body: str, now_ms: int | None = None) -> Note:
        body = clean_text(body, MAX_NOTE_BODY, "note")
        with self.database.transaction() as tx:
            self._note_row(tx, user_id, note_id)
            tx.execute(
                "UPDATE notes SET body = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (body, _now(now_ms), note_id, user_id),
            )
            return _note(self._note_row(tx, user_id, note_id))

    def delete_note(self, user_id: str, note_id: str) -> None:
        with self.database.transaction() as tx:
            self._note_row(tx, user_id, note_id)
            tx.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))

    @staticmethod
    def _note_row(tx: Transaction, user_id: str, note_id: str) -> Row:
        rows = tx.execute("SELECT * FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
        if not rows:
            raise NotFound("No such note.")
        return rows[0]


def _now(now_ms: int | None) -> int:
    return int(time.time() * 1000) if now_ms is None else now_ms
