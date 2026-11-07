"""Delivery status rows in `whatsapp_messages`. Never holds message text or phone numbers.

Synchronous functions over the Database protocol; call them through `AccountService.run`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.accounts.db import Database
from app.integrations.base import MESSAGE_STATUSES

# accepted < sent < delivered < read. `failed` is terminal and sits outside the order.
_RANK = {status: index for index, status in enumerate(MESSAGE_STATUSES) if status != "failed"}


@dataclass(frozen=True)
class MessageRow:
    status: str
    detail: str
    updated_at_ms: int


def _now() -> int:
    return int(time.time() * 1000)


def record_sent(
    database: Database, user_id: str, message_id: str, recipient_label: str, detail: str = ""
) -> None:
    """Record the API's acceptance. Acceptance is not delivery."""
    now = _now()
    with database.transaction() as tx:
        tx.execute(
            "INSERT INTO whatsapp_messages "
            "(wa_message_id, user_id, recipient_label, status, detail, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, user_id, recipient_label[:80], "accepted", detail[:200], now, now),
        )


def apply_status(database: Database, message_id: str, status: str, detail: str = "") -> bool:
    """Apply a Meta status. True if the row changed. Unknown ids, duplicates, regressions: False."""
    if status not in MESSAGE_STATUSES or status == "accepted":
        return False
    with database.transaction() as tx:
        rows = tx.execute(
            "SELECT status FROM whatsapp_messages WHERE wa_message_id = ?", (message_id,)
        )
        if not rows:
            return False
        current = str(rows[0]["status"])
        if current == "failed" or current == status:
            return False
        if status != "failed" and _RANK.get(status, -1) <= _RANK.get(current, -1):
            return False
        # Compare-and-set, so two racing webhooks can never move a status backwards.
        tx.execute(
            "UPDATE whatsapp_messages SET status = ?, detail = ?, updated_at = ? "
            "WHERE wa_message_id = ? AND status = ?",
            (status, detail[:200], _now(), message_id, current),
        )
        return True


def apply_statuses(database: Database, updates: list[tuple[str, str, str]]) -> int:
    """(message_id, status, detail) triples. Returns how many rows changed."""
    return sum(apply_status(database, mid, status, detail) for mid, status, detail in updates)


def status_for(database: Database, user_id: str, message_id: str) -> MessageRow | None:
    """Only this person's row. Another person's id looks exactly like an unknown one."""
    with database.transaction() as tx:
        rows = tx.execute(
            "SELECT status, detail, updated_at FROM whatsapp_messages "
            "WHERE wa_message_id = ? AND user_id = ?",
            (message_id, user_id),
        )
    if not rows:
        return None
    row = rows[0]
    return MessageRow(str(row["status"]), str(row["detail"]), int(str(row["updated_at"])))
