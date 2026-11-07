"""Plans and tasks in the database, one person's at a time.

Every query filters by `user_id`. Every status change is a compare-and-set (`UPDATE ... WHERE
status IN (...) RETURNING id`), so two requests racing to approve, run, or cancel the same task
cannot both win. That single rule is what stops a duplicate email, event, or message.

Business conditions come back as values (a False from `move`), never as exceptions, because
`AccountService.run` turns any exception into "accounts are resting".
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from app.accounts.db import Database

PLANS_KEPT = 30
MAX_ACTIVE_PLANS = 3
ACTIVE = ("pending", "running", "waiting_for_input", "waiting_for_approval")


@dataclass(frozen=True)
class NewTask:
    kind: str
    args: dict[str, object]
    depends_on: tuple[int, ...]
    side_effect: bool


@dataclass(frozen=True)
class PlanRow:
    id: str
    user_id: str
    request_text: str
    language: str
    demo: bool
    created_at: int


@dataclass(frozen=True)
class TaskRow:
    id: str
    plan_id: str
    user_id: str
    position: int
    kind: str
    args: dict[str, Any]
    depends_on: tuple[int, ...]
    status: str
    side_effect: bool
    approved_hash: str | None
    result: dict[str, Any]
    error: str | None
    attempts: int
    created_at: int
    updated_at: int


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _task(row: Any) -> TaskRow:
    return TaskRow(
        id=str(row["id"]),
        plan_id=str(row["plan_id"]),
        user_id=str(row["user_id"]),
        position=int(row["position"]),
        kind=str(row["kind"]),
        args=cast(dict[str, Any], json.loads(str(row["args"]))),
        depends_on=tuple(int(i) for i in json.loads(str(row["depends_on"]))),
        status=str(row["status"]),
        side_effect=bool(row["side_effect"]),
        approved_hash=None if row["approved_hash"] is None else str(row["approved_hash"]),
        result=cast(dict[str, Any], json.loads(str(row["result"]))) if row["result"] else {},
        error=None if row["error"] is None else str(row["error"]),
        attempts=int(row["attempts"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _plan(row: Any) -> PlanRow:
    return PlanRow(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        request_text=str(row["request_text"]),
        language=str(row["language"]),
        demo=bool(row["demo"]),
        created_at=int(row["created_at"]),
    )


class TaskStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    # Plans ----------------------------------------------------------------------------------

    def create_plan(
        self,
        user_id: str,
        request_text: str,
        language: str,
        demo: bool,
        tasks: Sequence[NewTask],
        now_ms: int | None = None,
    ) -> tuple[PlanRow, list[TaskRow]] | None:
        """None when the person already has too many unfinished plans."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        plan_id = uuid4().hex
        with self.database.transaction() as tx:
            tx.lock_user(user_id)
            active = tx.execute(
                "SELECT COUNT(DISTINCT t.plan_id) AS n FROM tasks t "
                "WHERE t.user_id = ? AND t.status IN (?, ?, ?, ?)",
                (user_id, *ACTIVE),
            )
            if int(str(active[0]["n"])) >= MAX_ACTIVE_PLANS:
                return None
            tx.execute(
                "INSERT INTO plans (id, user_id, request_text, language, demo, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, user_id, request_text, language, 1 if demo else 0, now),
            )
            for position, task in enumerate(tasks):
                tx.execute(
                    "INSERT INTO tasks (id, plan_id, user_id, position, kind, args, depends_on, "
                    "status, side_effect, approved_hash, result, error, attempts, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?)",
                    (
                        uuid4().hex,
                        plan_id,
                        user_id,
                        position,
                        task.kind,
                        _dump(task.args),
                        _dump(list(task.depends_on)),
                        "pending",
                        1 if task.side_effect else 0,
                        now,
                        now,
                    ),
                )
            stale = tx.execute(
                "SELECT id FROM plans WHERE user_id = ? ORDER BY created_at DESC, id DESC",
                (user_id,),
            )
            for row in stale[PLANS_KEPT:]:
                tx.execute("DELETE FROM plans WHERE id = ? AND user_id = ?", (row["id"], user_id))
            plan_rows = tx.execute(
                "SELECT * FROM plans WHERE id = ? AND user_id = ?", (plan_id, user_id)
            )
            task_rows = tx.execute(
                "SELECT * FROM tasks WHERE plan_id = ? AND user_id = ? ORDER BY position",
                (plan_id, user_id),
            )
        return _plan(plan_rows[0]), [_task(row) for row in task_rows]

    def plan(self, user_id: str, plan_id: str) -> PlanRow | None:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM plans WHERE id = ? AND user_id = ?", (plan_id, user_id)
            )
        return _plan(rows[0]) if rows else None

    def latest_plan_id(self, user_id: str) -> str | None:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT id FROM plans WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (user_id,),
            )
        return str(rows[0]["id"]) if rows else None

    def tasks(self, user_id: str, plan_id: str) -> list[TaskRow]:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM tasks WHERE plan_id = ? AND user_id = ? ORDER BY position",
                (plan_id, user_id),
            )
        return [_task(row) for row in rows]

    def task(self, user_id: str, task_id: str) -> TaskRow | None:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            )
        return _task(rows[0]) if rows else None

    def recent(self, user_id: str, limit: int = 8) -> list[TaskRow]:
        """The person's newest tasks across plans, for the activity list."""
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
                (user_id, limit),
            )
        return [_task(row) for row in rows]

    def waiting_task(self, user_id: str, kind: str) -> TaskRow | None:
        """The person's newest task of this kind that is waiting for approval."""
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND kind = ? AND status = ? "
                "ORDER BY created_at DESC, position DESC LIMIT 1",
                (user_id, kind, "waiting_for_approval"),
            )
        return _task(rows[0]) if rows else None

    # Changes (compare-and-set) ----------------------------------------------------------------

    def move(
        self,
        user_id: str,
        task_id: str,
        expected: Sequence[str],
        status: str,
        *,
        result: dict[str, object] | None = None,
        error: str | None = None,
        approved_hash: str | None = None,
        count_attempt: bool = False,
        now_ms: int | None = None,
    ) -> bool:
        """Change status only if it is still one of `expected`. True if this call won."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        sets = ["status = ?", "updated_at = ?"]
        params: list[object] = [status, now]
        if result is not None:
            sets.append("result = ?")
            params.append(_dump(result))
        sets.append("error = ?")
        params.append(error)
        if approved_hash is not None:
            sets.append("approved_hash = ?")
            params.append(approved_hash)
        if count_attempt:
            sets.append("attempts = attempts + 1")
        marks = ", ".join("?" for _ in expected)
        params += [task_id, user_id, *expected]
        with self.database.transaction() as tx:
            rows = tx.execute(
                f"UPDATE tasks SET {', '.join(sets)} "
                f"WHERE id = ? AND user_id = ? AND status IN ({marks}) RETURNING id",
                tuple(params),
            )
        return bool(rows)

    def rewrite(
        self,
        user_id: str,
        task_id: str,
        expected: Sequence[str],
        *,
        args: dict[str, object],
        result: dict[str, object],
        status: str,
        now_ms: int | None = None,
    ) -> bool:
        """Replace arguments and result together (an edit), clearing any earlier approval."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        marks = ", ".join("?" for _ in expected)
        with self.database.transaction() as tx:
            rows = tx.execute(
                "UPDATE tasks SET args = ?, result = ?, status = ?, error = NULL, "
                "approved_hash = NULL, updated_at = ? "
                f"WHERE id = ? AND user_id = ? AND status IN ({marks}) RETURNING id",
                (_dump(args), _dump(result), status, now, task_id, user_id, *expected),
            )
        return bool(rows)

    def save_result(
        self, user_id: str, task_id: str, result: dict[str, object], now_ms: int | None = None
    ) -> None:
        """Update stored view data without touching status (a day planner edit, a status read)."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        with self.database.transaction() as tx:
            tx.execute(
                "UPDATE tasks SET result = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (_dump(result), now, task_id, user_id),
            )

    def recover_interrupted(self, now_ms: int | None = None) -> int:
        """After a restart nothing is running. A task left `running` fails, and a side effect that
        may or may not have happened is flagged so it is never retried blindly."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        with self.database.transaction() as tx:
            rows = tx.execute("SELECT * FROM tasks WHERE status = ?", ("running",))
            for row in rows:
                task = _task(row)
                result = dict(task.result)
                if task.side_effect:
                    result["outcome_unknown"] = True
                    message = (
                        "Saathi restarted while this was in progress, so I can't say whether it "
                        "went through. Check the service before trying again."
                    )
                else:
                    message = "Saathi restarted while this was running. Try again."
                tx.execute(
                    "UPDATE tasks SET status = ?, result = ?, error = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    ("failed", _dump(result), message, now, task.id, "running"),
                )
        return len(rows)
