"""Turn one request into a plan: an ordered list of catalogue tasks with dependencies.

The language model (if any) and the rules only PROPOSE. Every proposed task is validated against
`app.tasks.kinds`; anything unknown or invalid is dropped, never repaired. A plan is data: it has
no side effects, and the engine never runs a side-effect task without the person's approval of
the exact arguments.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import ValidationError

from app.observability import log_event
from app.tasks import rules
from app.tasks.kinds import UnknownKind, validate_args
from app.tasks.pending import Pending

MAX_TASKS = 6
LLM_DEADLINE_SECONDS = 20.0


@dataclass(frozen=True)
class PlannedTask:
    kind: str
    args: dict[str, object]
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class Plan:
    tasks: tuple[PlannedTask, ...]
    clarification: str | None = None
    source: str = "rules"
    pending: Pending | None = None  # the question to remember, when one detail is missing


class PlanExtractor(Protocol):
    async def plan_json(
        self, text: str, language: str, now: datetime
    ) -> list[dict[str, object]] | None:
        """The model's proposed tasks as raw JSON objects, or None for "no answer"."""
        ...


def _clean_dependencies(raw: object, index: int, alive: dict[int, int]) -> tuple[int, ...] | None:
    """New-position dependencies, or None if any is invalid (forward, self, unknown, dropped)."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return None
    result: list[int] = []
    for value in raw:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value < 0 or value >= index or value not in alive:
            return None
        mapped = alive[value]
        if mapped not in result:
            result.append(mapped)
    return tuple(result)


_CONTENT = {
    "whatsapp.send": "body",
    "gmail.send": "body",
    "gmail.draft_reply": "intent",
    "reminder.create": "text",
}
_WORD = re.compile(r"[\w\u0900-\u097f\u0c80-\u0cff]+")


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if len(w) > 1}


def _said(content: str, user_text: str) -> bool:
    """Most of the words of a message must be words the person used: the model may not write it."""
    words = _words(content)
    if not words:
        return False
    heard = _words(user_text)
    return len(words & heard) * 2 >= len(words)


def _provenance_ok(kind: str, args: dict[str, object], user_text: str) -> bool:
    """Addresses and message words in a task must be ones the person actually said or typed.

    A model asked to plan "WhatsApp Amma" may write a message for her. That would be words put in the
    person's mouth, so such a task is dropped and the rules ask what to say instead.
    """
    field = _CONTENT.get(kind)
    if field is not None:
        content = args.get(field)
        if isinstance(content, str) and not _said(content, user_text):
            return False
    if kind not in {"gmail.send", "calendar.create"}:
        return True
    key = "to" if kind == "gmail.send" else "attendees"
    values = args.get(key)
    if not isinstance(values, list):
        return True
    lowered = user_text.lower()
    return all(isinstance(v, str) and v.lower() in lowered for v in values)  # pyright: ignore[reportUnknownVariableType]


def _normalise(raw: object, user_text: str) -> list[PlannedTask] | None:
    """Validate a proposed list. None means the whole answer is unusable (not a list, too long)."""
    if not isinstance(raw, list) or len(raw) > MAX_TASKS:  # pyright: ignore[reportUnknownArgumentType]
        return None
    alive: dict[int, int] = {}
    tasks: list[PlannedTask] = []
    for index, item in enumerate(raw):  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        if not isinstance(item, dict) or not set(item) <= {"kind", "args", "depends_on"}:  # pyright: ignore[reportUnknownArgumentType]
            continue
        kind = item.get("kind")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        args = item.get("args", {})  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if args is None:
            args = {}
        if not isinstance(kind, str) or not isinstance(args, dict):
            continue
        deps = _clean_dependencies(item.get("depends_on"), index, alive)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if deps is None:
            continue
        try:
            model = validate_args(kind, {str(k): v for k, v in args.items()})  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        except (UnknownKind, ValidationError):
            continue
        clean = model.model_dump(mode="json")
        if not _provenance_ok(kind, clean, user_text):
            continue
        alive[index] = len(tasks)
        tasks.append(PlannedTask(kind, clean, deps))
    return tasks


def _ensure_availability(tasks: list[PlannedTask]) -> list[PlannedTask]:
    """Every calendar.create depends on an availability check for the same slot."""
    result: list[PlannedTask] = []
    remap: dict[int, int] = {}
    for old_index, task in enumerate(tasks):
        deps = tuple(remap[d] for d in task.depends_on)
        if task.kind == "calendar.create":
            slot = (task.args["date"], task.args["start"])
            existing = next(
                (
                    i
                    for i, earlier in enumerate(result)
                    if earlier.kind == "calendar.availability"
                    and (earlier.args["date"], earlier.args["start"]) == slot
                ),
                None,
            )
            if existing is None:
                result.append(
                    PlannedTask(
                        "calendar.availability",
                        {
                            "date": task.args["date"],
                            "start": task.args["start"],
                            "minutes": task.args["minutes"],
                        },
                    )
                )
                existing = len(result) - 1
            if existing not in deps:
                deps = (*deps, existing)
        remap[old_index] = len(result)
        result.append(PlannedTask(task.kind, task.args, deps))
    return result[:MAX_TASKS]


def _finish(tasks: list[PlannedTask]) -> list[PlannedTask]:
    return _ensure_availability(tasks)


async def _from_extractor(
    extractor: PlanExtractor, text: str, language: str, now: datetime
) -> list[PlannedTask] | None:
    try:
        raw = await asyncio.wait_for(
            extractor.plan_json(text, language, now), timeout=LLM_DEADLINE_SECONDS
        )
    except Exception as exc:  # A broken or slow model must never end the turn.
        log_event("plan", op="llm", outcome=type(exc).__name__)
        return None
    if raw is None:
        log_event("plan", op="llm", outcome="no_answer")
        return None
    tasks = _normalise(raw, text)
    if not tasks:
        log_event("plan", op="llm", outcome="no_valid_tasks")
        return None
    return tasks


async def make_plan(
    text: str,
    language: str,
    now: datetime,
    extractor: PlanExtractor | None = None,
) -> Plan:
    """The plan for one request. The model is tried first when given; the rules are the fallback."""
    if extractor is not None:
        proposed = await _from_extractor(extractor, text, language, now)
        if proposed:
            final = _finish(proposed)
            log_event("plan", op="llm", outcome="ok", items=len(final))
            return Plan(tuple(final), None, "llm")
    result = rules.plan_with_rules(text, now)
    tasks = _normalise(result.tasks[:MAX_TASKS], text) or []
    final = _finish(tasks)
    question = result.clarifications[0] if result.clarifications else None
    pending = result.pending[0] if result.pending and not final else None
    log_event("plan", op="rules", outcome="ok" if final else "empty", items=len(final))
    return Plan(tuple(final), question, "rules", pending)


def complete(tasks: list[PlannedTask]) -> Plan:
    """A plan from tasks that are now complete (an answer filled the missing detail)."""
    return Plan(tuple(_finish(tasks)), None, "rules")


# ---- the cheap check the web layer runs first --------------------------------------------------


def _signals(text: str) -> set[str]:
    return rules.trigger_categories(text)


def looks_like_a_plan(text: str) -> bool:
    """True when the text names email, calendar, WhatsApp, a day plan, or several actions.

    Deterministic, no model and no network. A request whose only recognised action is a single
    reminder is left to the reminder handler, and ordering requests have none of these words.
    """
    if not text or not text.strip():
        return False
    categories = _signals(text)
    if not categories:
        return False
    # "message" and "text" alone are common words: they count only as "message/text <someone>".
    if categories == {"message"} and not _message_to_someone(text):
        return False
    result = rules.plan_with_rules(text, _REFERENCE_NOW)
    kinds = [t["kind"] for t in result.tasks]
    if categories == {"reminder"}:
        return kinds.count("reminder.create") >= 2
    if kinds and set(kinds) == {"reminder.create"}:
        return len(kinds) >= 2
    return True


_REFERENCE_NOW = datetime(2026, 1, 1, 9, 0)
_MESSAGE_TO = re.compile(
    r"(?<![\w])(?:message|msg|text|sms)\s+(?!(?:for|from|in|on|about|is|was|me|you|and|of|that|with)\b)"
    r"(?:to\s+)?(?:my\s+|the\s+)?[^\W\d_]{2,}"
    r"|(?:मैसेज|संदेश|ಮೆಸೇಜ್|ಸಂದೇಶ)",
    re.IGNORECASE,
)


def _message_to_someone(text: str) -> bool:
    return bool(_MESSAGE_TO.search(text))


def task_kinds(plan: Plan) -> Sequence[str]:
    return [task.kind for task in plan.tasks]
