"""Durable record of every checkout attempt, written BEFORE checkout is called.

Checkout has no idempotency key and cannot be cancelled by API, so the only
protection against a duplicate order across retries, new sessions, and process
restarts is remembering each attempt. The journal stores no address, phone
number, or token: only hashes, totals, product names, and order IDs.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4


class AttemptStatus(StrEnum):
    PENDING = "pending"  # checkout may have been sent; outcome not known
    PLACED = "placed"
    NOT_PLACED = "not_placed"  # refused, and order history showed no new order
    UNKNOWN = "unknown"  # reconciliation could not settle it


UNRESOLVED = frozenset({AttemptStatus.PENDING, AttemptStatus.UNKNOWN})


class JournalError(RuntimeError):
    """The journal cannot be trusted. Checkout must not proceed."""


@dataclass(frozen=True)
class OrderAttempt:
    attempt_id: str
    created_at: str
    status: AttemptStatus
    cart_hash: str
    total_inr: str
    baseline_order_ids: tuple[str, ...]
    item_names: tuple[str, ...]
    order_ids: tuple[str, ...] = ()
    order_status: str = ""
    note: str = ""


def new_attempt(
    cart_hash: str,
    total_inr: str,
    baseline_order_ids: tuple[str, ...],
    item_names: tuple[str, ...],
) -> OrderAttempt:
    return OrderAttempt(
        attempt_id=uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        status=AttemptStatus.PENDING,
        cart_hash=cart_hash,
        total_inr=total_inr,
        baseline_order_ids=baseline_order_ids,
        item_names=item_names,
    )


class OrderJournal(Protocol):
    def attempts(self) -> list[OrderAttempt]:
        """Latest record of every attempt, oldest first."""
        ...

    def begin(self, attempt: OrderAttempt, guard: Callable[[list[OrderAttempt]], None]) -> None:
        """Atomically run `guard` over existing attempts, then record `attempt`."""
        ...

    def update(self, attempt: OrderAttempt) -> None: ...


def unresolved_attempts(journal: OrderJournal) -> list[OrderAttempt]:
    return [attempt for attempt in journal.attempts() if attempt.status in UNRESOLVED]


def resolve_attempt(
    journal: OrderJournal,
    attempt: OrderAttempt,
    status: AttemptStatus,
    *,
    order_ids: tuple[str, ...] = (),
    order_status: str = "",
    note: str = "",
) -> OrderAttempt:
    updated = replace(
        attempt,
        status=status,
        order_ids=order_ids or attempt.order_ids,
        order_status=order_status or attempt.order_status,
        note=note or attempt.note,
    )
    journal.update(updated)
    return updated


class InMemoryOrderJournal:
    """Per-runner journal for mock mode and tests. Mock orders are not real."""

    def __init__(self) -> None:
        self._attempts: dict[str, OrderAttempt] = {}

    def attempts(self) -> list[OrderAttempt]:
        return list(self._attempts.values())

    def begin(self, attempt: OrderAttempt, guard: Callable[[list[OrderAttempt]], None]) -> None:
        guard(self.attempts())
        self._attempts[attempt.attempt_id] = attempt

    def update(self, attempt: OrderAttempt) -> None:
        self._attempts[attempt.attempt_id] = attempt


class FileOrderJournal:
    """Append-only JSON lines, fsynced before checkout, safe across processes via flock."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _locked(self) -> Generator[int]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            raise JournalError("The order journal cannot be opened") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        except OSError as exc:
            raise JournalError("The order journal cannot be written") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _parse(raw: bytes) -> list[OrderAttempt]:
        latest: dict[str, OrderAttempt] = {}
        for line in raw.decode("utf-8", errors="strict").splitlines():
            if not line.strip():
                continue
            try:
                value = cast(dict[str, object], json.loads(line))
                attempt = OrderAttempt(
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
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise JournalError("The order journal is corrupted") from exc
            latest[attempt.attempt_id] = attempt
        return list(latest.values())

    @staticmethod
    def _read(fd: int) -> list[OrderAttempt]:
        size = os.fstat(fd).st_size
        try:
            return FileOrderJournal._parse(os.pread(fd, size, 0))
        except UnicodeDecodeError as exc:
            raise JournalError("The order journal is corrupted") from exc

    @staticmethod
    def _append(fd: int, attempt: OrderAttempt) -> None:
        record = asdict(attempt)
        record["status"] = attempt.status.value
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
        os.fsync(fd)

    def attempts(self) -> list[OrderAttempt]:
        with self._locked() as fd:
            return self._read(fd)

    def begin(self, attempt: OrderAttempt, guard: Callable[[list[OrderAttempt]], None]) -> None:
        with self._locked() as fd:
            guard(self._read(fd))
            self._append(fd, attempt)

    def update(self, attempt: OrderAttempt) -> None:
        with self._locked() as fd:
            self._append(fd, attempt)
