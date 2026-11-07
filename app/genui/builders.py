"""Deterministic widget builders. Pure: they take the app's own typed data and return validated
Widgets. Prices are formatted from amounts already held; nothing is computed here. Externally
sourced strings go through clean_text."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal, Protocol

from app.dining.state_machine import DiningSession
from app.genui.actions import targeted
from app.genui.models import Widget
from app.genui.sanitize import clean_multiline, clean_text
from app.genui.validate import GenUIError, validate_widget
from app.orchestrator.state_machine import CartEntry, Money


class CartLike(Protocol):
    """Both Session and WorkflowView satisfy this."""

    @property
    def total_inr(self) -> Money: ...
    @property
    def fees_inr(self) -> Money: ...
    @property
    def payment_method(self) -> str: ...
    @property
    def address_label(self) -> str: ...
    @property
    def delivery_address(self) -> str: ...


@dataclass(frozen=True)
class ReminderInput:
    id: str
    text: str
    due: datetime | None = None
    done: bool = False


@dataclass(frozen=True)
class NoteInput:
    id: str
    body: str
    updated: datetime


@dataclass(frozen=True)
class CalendarInput:
    title: str
    start: datetime
    end: datetime
    location: str | None = None
    description: str | None = None


TaskStatus = Literal[
    "pending",
    "running",
    "waiting_for_input",
    "waiting_for_approval",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class TaskInput:
    id: str
    title: str
    status: TaskStatus
    detail: str | None = None
    retriable: bool = False
    cancellable: bool = False
    approvable: bool = False  # adds task.approve:<id>
    viewable: bool = False  # adds task.view:<id>
    editable: bool = False  # adds task.edit:<id>


@dataclass(frozen=True)
class EmailItem:
    id: str  # the mail service's own id, ^[A-Za-z0-9_-]{1,100}$
    sender: str
    subject: str
    snippet: str
    received: datetime
    unread: bool = False
    important: bool = False
    needs_reply: bool = False


@dataclass(frozen=True)
class EventFields:
    title: str
    date: str | date  # "YYYY-MM-DD"
    start: str | time  # "HH:MM"
    minutes: int
    attendees: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventConflict:
    title: str
    start: str | time
    end: str | time


@dataclass(frozen=True)
class PlannerItem:
    id: str  # becomes the suffix of plan.toggle:<id> and friends: ^[a-z0-9_.-]{1,40}$
    kind: Literal["event", "reminder", "email", "task"]
    title: str
    time: str | time | None = None
    done: bool = False
    priority: Literal["low", "normal", "high"] = "normal"
    detail: str | None = None


@dataclass(frozen=True)
class MemoryItem:
    key: str  # suffix of memory.forget:<key>: ^[a-z0-9_.-]{1,40}$
    label: str
    value: str


@dataclass(frozen=True)
class DayEvent:
    id: str
    title: str
    start: str | time
    end: str | time
    detail: str | None = None


@dataclass(frozen=True)
class DayInput:
    date: str | date
    events: tuple[DayEvent, ...] = ()


def inr(amount: Money) -> str:
    """Format an existing amount as a rupee string such as "78.00" (no arithmetic on prices)."""
    return f"{Decimal(str(amount)):.2f}"


def _line_entries(source: object) -> tuple[CartEntry, ...]:
    entries = getattr(source, "lines", None)
    if entries is None:
        entries = getattr(source, "cart", ())
    return tuple(entries)


def _lines_text(entries: Sequence[CartEntry]) -> list[tuple[str, str]]:
    return [
        (
            f"{e.quantity} x {clean_text(e.name, 120)}",
            f"{clean_text(e.pack_size, 40)}, Rs. {inr(e.unit_price_inr)} each".lstrip(", "),
        )
        for e in entries
    ]


def _target(action_id: str, target: str) -> str:
    try:
        return targeted(action_id, target)
    except ValueError as exc:
        raise GenUIError(f"id {target!r} cannot target {action_id}") from exc


def _build(data: dict[str, Any]) -> Widget:
    return validate_widget(data)


def cart_widget(source: Any, *, demo: bool, editable: bool) -> Widget:
    """A cart from a Session or WorkflowView. Totals are the cart's own."""
    entries = _line_entries(source)
    lines = [
        {
            "id": f"line{i}",
            "name": clean_text(e.name, 120) or "Item",
            "quantity": e.quantity,
            "unit_price_inr": inr(e.unit_price_inr),
            "detail": clean_text(e.pack_size, 60) or None,
        }
        for i, e in enumerate(entries)
    ]
    fees = source.fees_inr
    return _build(
        {
            "type": "cart",
            "id": "cart",
            "title": "Your cart",
            "lines": [{k: v for k, v in ln.items() if v is not None} for ln in lines],
            "total_inr": inr(source.total_inr),
            "fees_inr": inr(fees) if fees else None,
            "editable": editable,
            "demo": demo,
        }
    )


def _confirm_actions() -> dict[str, Any]:
    return {
        "confirm": {"label": "Yes, confirm", "action_id": "order.confirm", "style": "primary"},
        "reject": {"label": "No, change it", "action_id": "order.reject"},
    }


def confirmation_widget(source: Any, *, demo: bool) -> Widget:
    """The order readback (Session or WorkflowView) with confirm/reject."""
    details = [
        {"label": label, "value": value} for label, value in _lines_text(_line_entries(source))
    ]
    fees = source.fees_inr
    if fees:
        details.append({"label": "Fees", "value": f"Rs. {inr(fees)}"})
    details.append({"label": "Total", "value": f"Rs. {inr(source.total_inr)}"})
    address = clean_text(
        " ".join(p for p in (source.address_label, source.delivery_address) if p), 300
    )
    if address:
        details.append({"label": "Deliver to", "value": address})
    details.append({"label": "Payment", "value": clean_text(source.payment_method, 40) or "COD"})
    consequence = (
        "This is a demo. No real order is placed and nothing is charged."
        if demo
        else "A real order will be placed and you will pay on delivery as shown."
    )
    return _build(
        {
            "type": "confirmation",
            "id": "confirmation",
            "title": "Confirm your order",
            "details": details,
            "consequence": consequence,
            "demo": demo,
            **_confirm_actions(),
        }
    )


def booking_confirmation_widget(session: DiningSession, *, demo: bool) -> Widget:
    """The table-booking readback with confirm/reject."""
    details: list[dict[str, str]] = []
    if session.restaurant is not None:
        details.append({"label": "Restaurant", "value": clean_text(session.restaurant.name, 120)})
        if session.restaurant.area:
            details.append({"label": "Area", "value": clean_text(session.restaurant.area, 120)})
    slot = session.slot
    details.append({"label": "Date", "value": clean_text(slot.date if slot else session.date, 20)})
    time_text = slot.display_time if slot else session.time
    details.append({"label": "Time", "value": clean_text(time_text, 20)})
    details.append({"label": "Guests", "value": str(session.guests)})
    consequence = (
        "This is a demo. No real table is booked."
        if demo
        else "A real table booking will be made for this time."
    )
    return _build(
        {
            "type": "confirmation",
            "id": "booking.confirmation",
            "title": "Confirm your table",
            "details": details,
            "consequence": consequence,
            "demo": demo,
            **_confirm_actions(),
        }
    )


def share_panel_widget(summary: str, calendar: CalendarInput | None = None) -> Widget:
    cal: dict[str, Any] | None = None
    if calendar is not None:
        cal = {
            "title": clean_text(calendar.title, 200),
            "start": calendar.start.isoformat(),
            "end": calendar.end.isoformat(),
        }
        if calendar.location:
            cal["location"] = clean_text(calendar.location, 300)
        if calendar.description:
            cal["description"] = clean_text(calendar.description, 1000)
    return _build(
        {
            "type": "share_panel",
            "id": "share",
            "title": "Share",
            "summary": clean_text(summary, 2000),
            "calendar": cal,
        }
    )


def task_board_widget(
    tasks: Sequence[TaskInput], *, demo: bool, summary: str | None = None
) -> Widget:
    rows: list[dict[str, Any]] = []
    for t in tasks:
        row: dict[str, Any] = {
            "id": t.id,
            "title": clean_text(t.title, 200) or "Task",
            "status": t.status,
        }
        if t.detail:
            row["detail"] = clean_text(t.detail, 500)
        if t.retriable:
            row["retry_action_id"] = _target("task.retry", t.id)
        if t.cancellable:
            row["cancel_action_id"] = _target("task.cancel", t.id)
        if t.approvable:
            row["approve_action_id"] = _target("task.approve", t.id)
        if t.viewable:
            row["view_action_id"] = _target("task.view", t.id)
        if t.editable:
            row["edit_action_id"] = _target("task.edit", t.id)
        rows.append(row)
    data: dict[str, Any] = {
        "type": "task_board",
        "id": "tasks",
        "title": "Tasks",
        "tasks": rows,
        "demo": demo,
    }
    if summary:
        data["summary"] = clean_text(summary, 300)
    return _build(data)


def _date_text(value: str | date) -> str:
    return value.isoformat()[:10] if isinstance(value, date) else value


def _time_text(value: str | time) -> str:
    return value.strftime("%H:%M") if isinstance(value, time) else value


def _capped(count: int, limit: int, what: str) -> None:
    if count > limit:
        raise GenUIError(f"at most {limit} {what}")


def email_list_widget(
    items: Sequence[EmailItem], *, title: str = "Your emails", demo: bool
) -> Widget:
    """Per item the browser derives email.open:<id> and email.reply:<id>."""
    _capped(len(items), 10, "emails")
    rows: list[dict[str, Any]] = []
    for e in items:
        _target("email.open", e.id)  # an id that cannot be an action suffix fails closed
        rows.append(
            {
                "id": e.id,
                "sender": clean_text(e.sender, 200) or "Unknown sender",
                "subject": clean_text(e.subject, 300) or "(no subject)",
                "snippet": clean_text(e.snippet, 500),
                "received": e.received.isoformat(),
                "unread": e.unread,
                "important": e.important,
                "needs_reply": e.needs_reply,
            }
        )
    return _build(
        {
            "type": "email_list",
            "id": "emails",
            "title": clean_text(title, 200) or "Your emails",
            "items": rows,
            "empty_text": "No emails to show.",
            "demo": demo,
        }
    )


def event_preview_widget(
    task_id: str,
    *,
    fields: EventFields,
    conflicts: Sequence[EventConflict] = (),
    state: Literal["draft", "created", "failed", "cancelled"],
    detail: str | None = None,
    demo: bool,
    editable: bool = False,
    heading: str = "New event",
) -> Widget:
    """Draft shows Approve (task.approve:<task_id>) and Cancel (task.cancel:<task_id>)."""
    _capped(len(fields.attendees), 10, "attendees")
    _capped(len(conflicts), 10, "conflicts")
    attendees = [a for a in (clean_text(x, 320) for x in fields.attendees) if a]
    data: dict[str, Any] = {
        "type": "event_preview",
        "id": f"event.{task_id}",
        "title": clean_text(heading, 100) or "Event",
        "fields": {
            "title": clean_text(fields.title, 200) or "Event",
            "date": _date_text(fields.date),
            "start": _time_text(fields.start),
            "minutes": fields.minutes,
            "attendees": attendees,
        },
        "conflicts": [
            {
                "title": clean_text(c.title, 200) or "Busy",
                "start": _time_text(c.start),
                "end": _time_text(c.end),
            }
            for c in conflicts
        ],
        "state": state,
        "approve": {
            "label": "Approve",
            "action_id": _target("task.approve", task_id),
            "style": "primary",
        },
        "cancel": {"label": "Cancel", "action_id": _target("task.cancel", task_id)},
        "demo": demo,
        "editable": editable,
    }
    if detail:
        data["detail"] = clean_text(detail, 500)
    return _build(data)


def day_planner_widget(
    *, date: str | date, items: Sequence[PlannerItem], demo: bool, editable: bool = False
) -> Widget:
    """The browser derives plan.toggle:<id>, plan.priority:<id>, plan.move:<id> and plan.add."""
    _capped(len(items), 40, "plan items")
    rows: list[dict[str, Any]] = []
    for it in items:
        _target("plan.toggle", it.id)
        row: dict[str, Any] = {
            "id": it.id,
            "kind": it.kind,
            "title": clean_text(it.title, 200) or "Item",
            "done": it.done,
            "priority": it.priority,
        }
        if it.time is not None:
            row["time"] = _time_text(it.time)
        if it.detail:
            row["detail"] = clean_text(it.detail, 300)
        rows.append(row)
    return _build(
        {
            "type": "day_planner",
            "id": "planner",
            "title": "Your day",
            "date": _date_text(date),
            "items": rows,
            "demo": demo,
            "editable": editable,
        }
    )


def memory_list_widget(items: Sequence[MemoryItem], *, consent_text: str) -> Widget:
    """The browser derives memory.forget:<key> and a form posting memory.save {key, value}."""
    _capped(len(items), 30, "memories")
    rows: list[dict[str, str]] = []
    for m in items:
        _target("memory.forget", m.key)
        rows.append(
            {
                "key": m.key,
                "label": clean_text(m.label, 100) or "Memory",
                "value": clean_text(m.value, 500),
            }
        )
    return _build(
        {
            "type": "memory_list",
            "id": "memory",
            "title": "What I remember",
            "items": rows,
            "empty_text": "I am not remembering anything about you yet.",
            "consent_text": clean_text(consent_text, 500) or "Kept only to help you.",
        }
    )


def calendar_view_widget(
    days: Sequence[DayInput], *, title: str = "Your calendar", demo: bool
) -> Widget:
    out = [
        {
            "date": _date_text(d.date),
            "events": [
                {
                    "id": ev.id,
                    "title": clean_text(ev.title, 200) or "Event",
                    "start": _time_text(ev.start),
                    "end": _time_text(ev.end),
                    **({"detail": clean_text(ev.detail, 300)} if ev.detail else {}),
                }
                for ev in d.events
            ],
        }
        for d in days
    ]
    return _build(
        {
            "type": "calendar_view",
            "id": "calendar",
            "title": clean_text(title, 200) or "Your calendar",
            "days": out,
            "demo": demo,
        }
    )


def composer_widget(
    task_id: str,
    *,
    channel: Literal["email", "message"],
    to: str | None = None,
    subject: str | None = None,
    body: str,
    state: Literal["draft", "sent", "copied", "failed"],
    detail: str | None = None,
    demo: bool,
    editable: bool = False,
) -> Widget:
    """Send is task.approve:<task_id>; the browser posts edits as task.edit:<task_id>."""
    data: dict[str, Any] = {
        "type": "composer",
        "id": f"composer.{task_id}",
        "channel": channel,
        "body": clean_multiline(body, 2000),
        "send": {
            "label": "Send",
            "action_id": _target("task.approve", task_id),
            "style": "primary",
        },
        "copy_enabled": True,
        "state": state,
        "demo": demo,
        "editable": editable,
    }
    if to is not None:
        data["to"] = clean_text(to, 500)
    if subject is not None:
        data["subject"] = clean_text(subject, 500)
    if detail:
        data["detail"] = clean_text(detail, 500)
    return _build(data)


def reminder_list_widget(reminders: Sequence[ReminderInput]) -> Widget:
    """Item actions are reminder.done:<id>, reminder.undo:<id>, reminder.delete:<id>."""
    items = [
        {
            "id": r.id,
            "text": clean_text(r.text, 500) or "Reminder",
            "due": r.due.isoformat() if r.due else None,
            "done": r.done,
        }
        for r in reminders
    ]
    return _build(
        {"type": "reminder_list", "id": "reminders", "title": "Reminders", "items": items}
    )


def note_list_widget(notes: Sequence[NoteInput]) -> Widget:
    """Item action is note.delete:<id>."""
    items = [
        {"id": n.id, "body": clean_text(n.body, 2000), "updated": n.updated.isoformat()}
        for n in notes
    ]
    return _build({"type": "note_list", "id": "notes", "title": "Notes", "items": items})


def status_widget(
    kind: Literal["loading", "success", "empty", "error"],
    message: str,
    retry_action_id: str | None = None,
) -> Widget:
    data: dict[str, Any] = {
        "type": "status",
        "id": "status",
        "kind": kind,
        "message": clean_text(message, 500),
        "retry": None,
    }
    if retry_action_id is not None:
        data["retry"] = {"label": "Try again", "action_id": retry_action_id}
    return _build(data)


def new_reminder_form() -> Widget:
    return _build(
        {
            "type": "form",
            "id": "form.reminder",
            "title": "New reminder",
            "fields": [
                {
                    "name": "text",
                    "label": "Reminder",
                    "kind": "text",
                    "required": True,
                    "max_length": 500,
                },
                {"name": "date", "label": "Date", "kind": "date"},
                {"name": "time", "label": "Time", "kind": "time"},
            ],
            "submit": {"label": "Add reminder", "action_id": "reminder.create", "style": "primary"},
        }
    )


def new_note_form() -> Widget:
    return _build(
        {
            "type": "form",
            "id": "form.note",
            "title": "New note",
            "fields": [
                {
                    "name": "body",
                    "label": "Note",
                    "kind": "textarea",
                    "required": True,
                    "max_length": 2000,
                }
            ],
            "submit": {"label": "Save note", "action_id": "note.create", "style": "primary"},
        }
    )
