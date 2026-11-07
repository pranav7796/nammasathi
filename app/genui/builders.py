"""Deterministic widget builders. Pure: they take the app's own typed data and return validated
Widgets. Prices are formatted from amounts already held; nothing is computed here. Externally
sourced strings go through clean_text."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from app.dining.state_machine import DiningSession
from app.genui.actions import targeted
from app.genui.models import Widget
from app.genui.sanitize import clean_text
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


@dataclass(frozen=True)
class TaskInput:
    id: str
    title: str
    status: Literal[
        "pending",
        "running",
        "waiting_for_input",
        "waiting_for_approval",
        "completed",
        "failed",
        "cancelled",
    ]
    detail: str | None = None
    retriable: bool = False
    cancellable: bool = False


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


def task_board_widget(tasks: Sequence[TaskInput]) -> Widget:
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
        rows.append(row)
    return _build({"type": "task_board", "id": "tasks", "title": "Tasks", "tasks": rows})


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
