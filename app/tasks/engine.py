"""The task engine: runs a plan, keeps the person in control, and tells the truth about it.

What it guarantees (each has a test):

* Independent read-only tasks run at the same time (bounded), and the board says so only from
  timestamps it actually recorded. Tasks that depend on each other run in order.
* A task that changes something outside Saathi never runs by itself. It stops at
  `waiting_for_approval` with an exact preview, and runs only after the person approves that
  preview (or sends the values they edited). Approval is a compare-and-set in the database, so a
  double click, a second tab, or a retry cannot run it twice.
* Whatever an adapter returns is checked: an effect that cannot be confirmed is reported as
  unconfirmed and blocks retry, instead of being called a success.
* An effect interrupted by a restart is marked "outcome unknown" and is never retried blindly.
* Everything is per person. Every query filters by the signed-in user, never by a client id alone.
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from pydantic import ValidationError

from app.integrations.base import (
    Email,
    IntegrationError,
    MessagingClient,
    NotConnected,
    RateLimited,
    ReauthRequired,
)
from app.observability import log_event
from app.personal.dates import IST
from app.tasks import executors as ex
from app.tasks import kinds as K
from app.tasks.pending import Pending
from app.tasks.planner import Plan, PlannedTask
from app.tasks.store import NewTask, PlanRow, TaskRow, TaskStore

READ_CONCURRENCY = 4
READ_TIMEOUT_SECONDS = 25.0
EFFECT_TIMEOUT_SECONDS = 40.0
MAX_ATTEMPTS = 3
PLANS_PER_HOUR = 20
# Kinds whose whole preview is the board line itself, so the Approve button needs no form.
APPROVE_WITHOUT_VALUES = frozenset({"reminder.create", "calendar.cancel"})

ServiceProvider = Callable[[str], Awaitable[ex.Services]]
Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(IST)


def _ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class VoicePending:
    """What Saathi read aloud for approval, so only that exact content can be confirmed."""

    task_id: str
    hash: str
    expires: float


@dataclass
class PendingState:
    """A question Saathi asked this person, and how many times they have not answered it."""

    pending: Pending
    expires: float
    tries: int = 0


@dataclass(frozen=True)
class Snapshot:
    plan: PlanRow
    tasks: list[TaskRow]

    @property
    def active(self) -> bool:
        return any(t.status in ("pending", "running") for t in self.tasks)


class _Limiter:
    def __init__(self, limit: int, window: float) -> None:
        self._limit, self._window = limit, window
        self._events: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        kept = [t for t in self._events.get(key, []) if now - t < self._window]
        if len(kept) >= self._limit:
            self._events[key] = kept
            return False
        kept.append(now)
        self._events[key] = kept
        if len(self._events) > 5000:
            self._events.clear()
        return True


class TaskEngine:
    def __init__(
        self,
        store: TaskStore,
        services: ServiceProvider,
        *,
        run_blocking: Callable[..., Awaitable[Any]],
        clock: Clock = _now,
    ) -> None:
        """`run_blocking(call, *args)` runs a store call off the event loop (AccountService.run)."""
        self.store = store
        self._services = services
        self._run = run_blocking
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._limiter = _Limiter(PLANS_PER_HOUR, 3600)
        self._read_slots = asyncio.Semaphore(READ_CONCURRENCY)
        self.voice_pending: dict[str, VoicePending] = {}
        self.questions: dict[str, PendingState] = {}

    async def _db(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """A store call off the event loop. AccountService.run takes positional arguments only."""
        return await self._run(functools.partial(call, *args, **kwargs))

    def now(self) -> datetime:
        return self._clock()

    async def messaging(self, user_id: str) -> MessagingClient:
        return (await self._services(user_id)).messaging

    # Lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        recovered = await self._db(self.store.recover_interrupted)
        if recovered:
            log_event("tasks", op="recover", outcome="interrupted", count=int(recovered))

    async def close(self) -> None:
        for job in list(self._background):
            job.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    async def settle(self, plan_id: str | None = None) -> None:
        """For tests: wait until background work has finished."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # Starting a plan -----------------------------------------------------------------------------

    async def submit(self, user_id: str, text: str, language: str, plan: Plan) -> Snapshot | str:
        """A snapshot of the new plan, or a plain sentence saying why not."""
        if not plan.tasks:
            return "empty"
        if not self._limiter.allow(user_id):
            return "busy"
        services = await self._services(user_id)
        new = [
            NewTask(
                t.kind,
                dict(K.validate_args(t.kind, t.args).model_dump(mode="json")),
                t.depends_on,
                K.spec_for(t.kind).side_effect,
            )
            for t in plan.tasks
        ]
        providers = {K.spec_for(t.kind).provider for t in plan.tasks} - {"local"}
        demo = any(services.demo.get(p, True) for p in providers)
        created = await self._db(self.store.create_plan, user_id, text[:500], language, demo, new)
        if created is None:
            return "too_many"
        row, tasks = created
        log_event("tasks", op="plan", outcome="created", count=len(tasks), demo=demo)
        self._spawn(user_id, row.id)
        return Snapshot(row, tasks)

    def _spawn(self, user_id: str, plan_id: str) -> None:
        job = asyncio.get_running_loop().create_task(self.advance(user_id, plan_id))
        self._background.add(job)
        job.add_done_callback(self._background.discard)

    # Reading -------------------------------------------------------------------------------------

    async def snapshot(self, user_id: str, plan_id: str) -> Snapshot | None:
        plan = await self._db(self.store.plan, user_id, plan_id)
        if plan is None:
            return None
        tasks = await self._db(self.store.tasks, user_id, plan_id)
        if any(t.status == "pending" for t in tasks) and plan_id not in self._locks_busy():
            self._spawn(user_id, plan_id)  # pending work left over from a restart
        return Snapshot(plan, tasks)

    def _locks_busy(self) -> set[str]:
        return {pid for pid, lock in self._locks.items() if lock.locked()}

    async def latest(self, user_id: str) -> Snapshot | None:
        plan_id = await self._db(self.store.latest_plan_id, user_id)
        return None if plan_id is None else await self.snapshot(user_id, plan_id)

    # Advancing -----------------------------------------------------------------------------------

    async def advance(self, user_id: str, plan_id: str) -> None:
        lock = self._locks.setdefault(plan_id, asyncio.Lock())
        async with lock:
            try:
                await self._advance(user_id, plan_id)
            except Exception:
                log_event("tasks", op="advance", outcome="error")
                raise

    async def _advance(self, user_id: str, plan_id: str) -> None:
        plan = await self._db(self.store.plan, user_id, plan_id)
        if plan is None:
            return
        while True:
            tasks = await self._db(self.store.tasks, user_id, plan_id)
            by_position = {t.position: t for t in tasks}
            ready: list[TaskRow] = []
            changed = False
            for task in tasks:
                if task.status != "pending":
                    continue
                dependencies = [by_position[i] for i in task.depends_on if i in by_position]
                if any(d.status in ("failed", "cancelled") for d in dependencies):
                    await self._db(
                        self.store.move,
                        user_id,
                        task.id,
                        ("pending",),
                        "cancelled",
                        error="An earlier step didn't finish, so I didn't do this one.",
                    )
                    changed = True
                elif all(d.status == "completed" for d in dependencies):
                    ready.append(task)
            if not ready:
                if changed:
                    continue
                return
            await asyncio.gather(*(self._start(plan, task, by_position) for task in ready))

    async def _start(self, plan: PlanRow, task: TaskRow, by_position: dict[int, TaskRow]) -> None:
        user_id = task.user_id
        upstream = {
            i: dict(by_position[i].result.get("view") or {})
            for i in task.depends_on
            if i in by_position
        }
        if not await self._db(
            self.store.move, user_id, task.id, ("pending",), "running", count_attempt=True
        ):
            return  # someone else (a cancel) got there first
        ctx = ex.Ctx(user_id, await self._services(user_id), self._clock(), plan.language)
        started = _ms()
        try:
            args = K.validate_args(task.kind, task.args)
            if task.side_effect:
                async with self._read_slots:
                    prepared = await asyncio.wait_for(
                        ex.prepare(task.kind, ctx, args, upstream), READ_TIMEOUT_SECONDS
                    )
                await self._park(task, prepared, ctx, started)
            else:
                async with self._read_slots:
                    view = await asyncio.wait_for(
                        ex.read(task.kind, ctx, args, upstream), READ_TIMEOUT_SECONDS
                    )
                result = {"view": view, "demo": bool(view.get("demo")), "timing": _timing(started)}
                await self._db(
                    self.store.move, user_id, task.id, ("running",), "completed", result=result
                )
        except Exception as exc:
            await self._fail(task, exc, started, effect=False)

    async def _park(self, task: TaskRow, prepared: ex.Prepared, ctx: ex.Ctx, started: int) -> None:
        result: dict[str, Any] = {
            "view": prepared.view,
            "effect_args": prepared.effect_args,
            "hash": ex.args_hash(task.kind, prepared.effect_args),
            "demo": bool(prepared.view.get("demo")),
            "timing": _timing(started),
        }
        if prepared.needs_input:
            result["needs"] = prepared.needs_input
            status = "waiting_for_input"
        else:
            status = "waiting_for_approval"
        await self._db(self.store.move, task.user_id, task.id, ("running",), status, result=result)

    async def _fail(self, task: TaskRow, exc: Exception, started: int, *, effect: bool) -> None:
        unknown = False
        if isinstance(exc, ReauthRequired):
            message = "That account needs to be connected again."
        elif isinstance(exc, NotConnected):
            message = "That service isn't connected yet."
        elif isinstance(exc, RateLimited):
            message = "That service is busy. Please try again in a moment."
        elif isinstance(exc, TimeoutError):
            message = "That took too long."
            unknown = effect
        elif isinstance(exc, IntegrationError):
            message = str(exc)[:200] or "That service couldn't do it."
            unknown = effect  # may or may not have happened: never retry blindly
        elif isinstance(exc, (ValidationError, K.UnknownKind)):
            message = "Those details weren't valid."
        else:
            message = "Something went wrong."
            unknown = effect
        if unknown:
            message += " I can't tell whether it went through. Please check before trying again."
        previous = await self._db(self.store.task, task.user_id, task.id)
        result = dict(previous.result) if previous else {}
        result["timing"] = _timing(started)
        if unknown:
            result["outcome_unknown"] = True
        log_event("tasks", op=task.kind, outcome="failed", kind=type(exc).__name__)
        await self._db(
            self.store.move,
            task.user_id,
            task.id,
            ("running",),
            "failed",
            result=result,
            error=message,
        )

    # Person's actions ---------------------------------------------------------------------------

    async def approve(
        self,
        user_id: str,
        task_id: str,
        *,
        values: dict[str, Any] | None = None,
        shown_hash: str | None = None,
    ) -> str:
        """'started', or why not: 'not_found', 'not_waiting', 'needs_input', 'stale', or a message.

        With `values` (the person pressed Send on a form they edited) those values are the thing
        approved. Without them the approval must carry the hash of the preview they were shown.
        """
        task = await self._db(self.store.task, user_id, task_id)
        if task is None or not task.side_effect:
            return "not_found"
        if task.status != "waiting_for_approval":
            return "needs_input" if task.status == "waiting_for_input" else "not_waiting"
        if values:
            edited = await self.edit(user_id, task_id, values)
            if edited != "ok":
                return edited
            task = await self._db(self.store.task, user_id, task_id)
            if task is None or task.status != "waiting_for_approval":
                return "needs_input"
        elif shown_hash is not None:
            if not hmac.compare_digest(shown_hash, str(task.result.get("hash", ""))):
                return "stale"
        elif task.kind not in APPROVE_WITHOUT_VALUES:
            # A form always sends the values it shows. Anything else must prove what it saw.
            return "stale"
        approved = str(task.result.get("hash", ""))
        started = _ms()
        # Compare-and-set: exactly one request gets past this line for a given waiting task.
        if not await self._db(
            self.store.move,
            user_id,
            task_id,
            ("waiting_for_approval",),
            "running",
            approved_hash=approved,
            count_attempt=True,
        ):
            return "not_waiting"
        plan = await self._db(self.store.plan, user_id, task.plan_id)
        ctx = ex.Ctx(
            user_id,
            await self._services(user_id),
            self._clock(),
            plan.language if plan else "en-IN",
        )
        try:
            outcome = await asyncio.wait_for(
                ex.effect(task.kind, ctx, dict(task.result["effect_args"]), task.id),
                EFFECT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            await self._fail(task, exc, started, effect=True)
            await self.advance(user_id, task.plan_id)
            return "started"
        result = dict(task.result)
        result["outcome"] = outcome.view
        result["timing"] = _timing(started)
        if outcome.verified:
            await self._db(
                self.store.move, user_id, task_id, ("running",), "completed", result=result
            )
            log_event("tasks", op=task.kind, outcome="completed")
        else:
            result["outcome_unknown"] = True
            await self._db(
                self.store.move,
                user_id,
                task_id,
                ("running",),
                "failed",
                result=result,
                error="It may have gone through, but I couldn't confirm it. Please check before "
                "trying again.",
            )
            log_event("tasks", op=task.kind, outcome="unverified")
        await self.advance(user_id, task.plan_id)
        return "started"

    async def edit(self, user_id: str, task_id: str, changes: dict[str, Any]) -> str:
        """'ok', 'not_found', 'not_editable', or a sentence about what to fix."""
        task = await self._db(self.store.task, user_id, task_id)
        if task is None or not task.side_effect:
            return "not_found"
        if task.status not in ("waiting_for_approval", "waiting_for_input"):
            return "not_editable"
        plan = await self._db(self.store.plan, user_id, task.plan_id)
        ctx = ex.Ctx(
            user_id,
            await self._services(user_id),
            self._clock(),
            plan.language if plan else "en-IN",
        )
        try:
            effect_args, prepared = await ex.apply_edit(
                task.kind, ctx, dict(task.result.get("effect_args", {})), changes
            )
        except ex.EditRejected as exc:
            return str(exc)
        except Exception:
            return "Please check what you typed."
        result = dict(task.result)
        result.update(
            view=prepared.view, effect_args=effect_args, hash=ex.args_hash(task.kind, effect_args)
        )
        result.pop("needs", None)
        status = "waiting_for_input" if prepared.needs_input else "waiting_for_approval"
        if prepared.needs_input:
            result["needs"] = prepared.needs_input
        won = await self._db(
            self.store.rewrite,
            user_id,
            task_id,
            ("waiting_for_approval", "waiting_for_input"),
            args=task.args,
            result=cast(dict[str, object], result),
            status=status,
        )
        return "ok" if won else "not_editable"

    async def cancel(self, user_id: str, task_id: str) -> str:
        task = await self._db(self.store.task, user_id, task_id)
        if task is None:
            return "not_found"
        if task.status == "running" and task.side_effect and task.approved_hash:
            return "in_flight"  # an approved action is already under way: it cannot be recalled
        won = await self._db(
            self.store.move,
            user_id,
            task_id,
            ("pending", "running", "waiting_for_input", "waiting_for_approval"),
            "cancelled",
            error="Cancelled.",
        )
        if won:
            self._spawn(user_id, task.plan_id)  # dependents are cancelled too
        return "cancelled" if won else "not_cancellable"

    async def retry(self, user_id: str, task_id: str) -> str:
        task = await self._db(self.store.task, user_id, task_id)
        if task is None:
            return "not_found"
        if task.status != "failed":
            return "not_failed"
        if task.result.get("outcome_unknown"):
            return "check_first"
        if task.attempts >= MAX_ATTEMPTS:
            return "too_many"
        won = await self._db(self.store.move, user_id, task_id, ("failed",), "pending")
        if won:
            self._spawn(user_id, task.plan_id)
        return "retrying" if won else "not_failed"

    async def plan_action(
        self, user_id: str, task_id: str, action: str, target: str, values: dict[str, Any]
    ) -> bool:
        """Changes to a day plan: toggle, priority, move, add. Saved, so they survive a reload.
        Ticking a reminder also completes the real reminder. Nothing touches Gmail or Calendar."""
        task = await self._db(self.store.task, user_id, task_id)
        if task is None or task.kind != "day.plan" or task.status != "completed":
            return False
        view = dict(task.result.get("view") or {})
        items = [dict(i) for i in view.get("items", [])]
        index = next((n for n, i in enumerate(items) if i["id"] == target), -1)
        if action == "plan.add":
            title = str(values.get("title", "")).strip()[:200]
            if not title or len(items) >= 40:
                return False
            moment = str(values.get("time") or "").strip() or None
            items.append(
                {
                    "id": "t" + uuid4().hex[:12],
                    "kind": "task",
                    "title": title,
                    "time": moment,
                    "done": False,
                    "priority": "normal",
                    "detail": "",
                }
            )
        elif index < 0:
            return False
        elif action == "plan.toggle":
            items[index]["done"] = not items[index]["done"]
            ref = items[index].get("ref")
            if items[index]["kind"] == "reminder" and ref:
                services = await self._services(user_id)
                try:
                    if items[index]["done"]:
                        await services.personal.complete_reminder(user_id, str(ref))
                    else:
                        await services.personal.uncomplete_reminder(user_id, str(ref))
                except Exception:
                    log_event("tasks", op="plan.toggle", outcome="reminder_not_updated")
        elif action == "plan.priority":
            priority = str(values.get("priority", ""))
            if priority not in ("low", "normal", "high"):
                return False
            items[index]["priority"] = priority
        elif action == "plan.move":
            step = -1 if values.get("direction") == "up" else 1
            other = index + step
            if not 0 <= other < len(items):
                return False
            items[index], items[other] = items[other], items[index]
        else:
            return False
        view["items"] = items
        result = dict(task.result)
        result["view"] = view
        await self._db(self.store.save_result, user_id, task_id, result)
        return True

    async def open_email(self, user_id: str, message_id: str) -> Email | None:
        """One email for the person to read. Its text is untrusted and is only ever displayed."""
        try:
            return await (await self._services(user_id)).email.get(user_id, message_id)
        except Exception:
            return None

    async def reply_to_email(self, user_id: str, message_id: str, language: str) -> Snapshot | str:
        """Starts a draft reply to an email the person opened. They write and approve it."""
        mail = await self.open_email(user_id, message_id)
        if mail is None:
            return "not_found"
        summary = mail.summary
        subject = (
            summary.subject
            if summary.subject.lower().startswith("re:")
            else (f"Re: {summary.subject}")
        )
        plan = Plan(
            (
                PlannedTask(
                    "gmail.draft_reply",
                    {
                        "to": summary.sender_email,
                        "intent": "Thanks for your email. I'll reply properly soon.",
                        "subject": subject[:200],
                        "thread_id": summary.thread_id or None,
                        "in_reply_to": mail.message_id_header or None,
                    },
                ),
            )
        )
        return await self.submit(user_id, f"Reply to {summary.sender_name}"[:200], language, plan)

    async def refine_time(self, user_id: str, new_time_text: str) -> str | None:
        """ "Make the meeting 4 PM instead": changes the time of the newest event waiting for
        approval, in place. None if there is no such event or no time in the text."""
        from app.personal.dates import parse_when

        task = await self._db(self.store.waiting_task, user_id, "calendar.create")
        if task is None:
            return None
        parsed = parse_when(new_time_text, self._clock())
        if parsed is None:
            return None
        outcome = await self.edit(
            user_id,
            task.id,
            {"date": parsed.due.date().isoformat(), "start": parsed.due.strftime("%H:%M")}
            if "date" in new_time_text.casefold() or _has_day_word(new_time_text)
            else {"start": parsed.due.strftime("%H:%M")},
        )
        return task.id if outcome == "ok" else None


def _has_day_word(text: str) -> bool:
    lowered = text.casefold()
    return any(w in lowered for w in ("today", "tomorrow", "kal", "आज", "कल", "ನಾಳೆ", "ಇಂದು"))


def _timing(started: int) -> dict[str, int]:
    return {"start_ms": started, "end_ms": _ms()}


def overlapping(tasks: list[TaskRow]) -> int:
    """The most tasks that were really running at the same moment, from recorded timestamps."""
    spans = [
        (int(t.result["timing"]["start_ms"]), int(t.result["timing"]["end_ms"]))
        for t in tasks
        if isinstance(t.result.get("timing"), dict) and not t.side_effect
    ]
    best = 0
    for begin, _ in spans:
        best = max(best, sum(1 for s, e in spans if s <= begin < e or s == begin))
    return best
