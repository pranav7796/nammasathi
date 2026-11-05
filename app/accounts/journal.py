"""The order journal for one user, in the database.

The same contract as the file journal: an attempt is durably recorded before checkout, atomically
with its guards. One user's unresolved attempt blocks that user's checkouts and nobody else's.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from typing import cast

from app.accounts.db import Database
from app.commerce.order_journal import AttemptStatus, JournalError, OrderAttempt


def _encode(attempt: OrderAttempt) -> str:
    record = asdict(attempt)
    record["status"] = attempt.status.value
    return json.dumps(record, ensure_ascii=False)


def _decode(raw: str) -> OrderAttempt:
    try:
        value = cast(dict[str, object], json.loads(raw))
        return OrderAttempt(
            attempt_id=str(value["attempt_id"]),
            created_at=str(value["created_at"]),
            status=AttemptStatus(str(value["status"])),
            cart_hash=str(value["cart_hash"]),
            total_inr=str(value["total_inr"]),
            baseline_order_ids=tuple(cast(list[str], value["baseline_order_ids"])),
            item_names=tuple(cast(list[str], value["item_names"])),
            order_ids=tuple(cast(list[str], value.get("order_ids", []))),
            order_status=str(value.get("order_status", "")),
            note=str(value.get("note", "")),
            domain=str(value.get("domain", "grocery")),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise JournalError("An order attempt record is corrupted") from exc


_SELECT = "SELECT record FROM order_attempts WHERE user_id = ? ORDER BY created_at, attempt_id"


class DatabaseOrderJournal:
    def __init__(self, database: Database, user_id: str) -> None:
        self.database = database
        self.user_id = user_id

    def attempts(self) -> list[OrderAttempt]:
        try:
            with self.database.transaction() as tx:
                rows = tx.execute(_SELECT, (self.user_id,))
        except JournalError:
            raise
        except Exception as exc:  # any database failure means the journal cannot be trusted
            raise JournalError("The order journal cannot be read") from exc
        return [_decode(cast(str, row["record"])) for row in rows]

    def begin(self, attempt: OrderAttempt, guard: Callable[[list[OrderAttempt]], None]) -> None:
        guard_error: Exception | None = None
        try:
            with self.database.transaction() as tx:
                tx.lock_user(self.user_id)
                rows = tx.execute(_SELECT, (self.user_id,))
                existing = [_decode(cast(str, row["record"])) for row in rows]
                try:
                    guard(existing)
                except Exception as exc:
                    guard_error = exc
                else:
                    tx.execute(
                        "INSERT INTO order_attempts "
                        "(attempt_id, user_id, status, record, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            attempt.attempt_id,
                            self.user_id,
                            attempt.status.value,
                            _encode(attempt),
                            attempt.created_at,
                        ),
                    )
        except JournalError:
            raise
        except Exception as exc:
            raise JournalError("The order journal cannot be written") from exc
        if guard_error is not None:
            raise guard_error

    def update(self, attempt: OrderAttempt) -> None:
        try:
            with self.database.transaction() as tx:
                tx.execute(
                    "UPDATE order_attempts SET status = ?, record = ? "
                    "WHERE attempt_id = ? AND user_id = ?",
                    (attempt.status.value, _encode(attempt), attempt.attempt_id, self.user_id),
                )
        except Exception as exc:
            raise JournalError("The order journal cannot be written") from exc
