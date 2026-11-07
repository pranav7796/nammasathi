"""Turns a plan's stored state into widgets. Rebuilt from the database on every request, so a page
refresh or a restart never loses the board, and the board can only say what was recorded.

Wording rule: a demo result never says "sent", "booked" or "created" on its own. It says what the
practice did, and carries `demo=True`, which the browser shows as a badge.
"""
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# (task views are JSON read back from the database; every value is converted by a typed builder)

from __future__ import annotations

import contextlib
from datetime import datetime, time
from typing import Any

from app.genui import builders as B
from app.genui.models import Widget
from app.integrations.base import MESSAGE_STATUSES, MessagingClient
from app.tasks import kinds as K
from app.tasks.engine import MAX_ATTEMPTS, Snapshot, overlapping
from app.tasks.store import TaskRow

_WHATSAPP_WORDS = {
    "accepted": "Accepted by WhatsApp. Not delivered yet.",
    "sent": "Sent by WhatsApp. Not delivered yet.",
    "delivered": "Delivered.",
    "read": "Delivered and read.",
    "failed": "WhatsApp could not deliver it.",
}


def _time(value: object) -> time | None:
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        return None


async def _whatsapp_detail(task: TaskRow, messaging: MessagingClient | None) -> str:
    if task.result.get("demo"):
        return "Demo only: nothing was sent to anyone."
    outcome = task.result.get("outcome") or {}
    message_id = str(outcome.get("message_id", ""))
    status = str(outcome.get("status", "accepted"))
    if messaging is not None and message_id:
        with contextlib.suppress(Exception):  # show the last status we recorded
            status = (await messaging.status(task.user_id, message_id)).status
    return _WHATSAPP_WORDS.get(status if status in MESSAGE_STATUSES else "accepted", "")


def _state_detail(task: TaskRow, whatsapp: str = "") -> str | None:
    demo = bool(task.result.get("demo"))
    if task.status == "pending":
        return "Waiting its turn."
    if task.status == "running":
        return "Working on it…"
    if task.status == "waiting_for_input":
        return str(task.result.get("needs") or "I need one more detail.")
    if task.status == "waiting_for_approval":
        return "Check the details, then approve. Nothing happens until you do."
    if task.status == "failed":
        return task.error
    if task.status == "cancelled":
        return task.error or "Cancelled."
    if task.status == "completed" and task.side_effect:
        if demo and task.kind != "reminder.create":
            return {
                "gmail.send": "Practice only. No email was sent to anyone.",
                "gmail.draft_reply": "Practice only. No email was sent to anyone.",
                "whatsapp.send": "Practice only. No message was sent to anyone.",
            }.get(task.kind, "Practice only. Nothing was added to a real calendar.")
        return {
            "gmail.send": "Sent. Gmail confirmed it.",
            "gmail.draft_reply": "Sent. Gmail confirmed it.",
            "calendar.create": "Created. I read it back from your calendar.",
            "calendar.reschedule": "Moved. I read it back from your calendar.",
            "calendar.cancel": "Removed. Your calendar no longer shows it.",
            "calendar.move": "Moved. I read it back from your calendar.",
            "calendar.remove": "Removed. Your calendar no longer shows it.",
            "whatsapp.send": whatsapp or None,
            "reminder.create": "Saved to your reminders.",
        }.get(task.kind)
    if task.status == "completed":
        view = task.result.get("view") or {}
        if task.kind == "calendar.availability":
            if view.get("free"):
                return "That time is free."
            names = ", ".join(c["title"] for c in view.get("conflicts", [])[:3])
            return f"That time clashes with: {names}."
        if "emails" in view:
            count = len(view["emails"])
            return f"{count} found." if count else "Nothing found."
        if "days" in view:
            count = sum(len(d["events"]) for d in view["days"])
            return f"{count} event(s)." if count else "Nothing scheduled."
        if "items" in view:
            return f"{len(view['items'])} item(s) for the day."
    return None


def _board(snapshot: Snapshot, whatsapp: dict[str, str]) -> Widget:
    rows = []
    for task in snapshot.tasks:
        spec = K.spec_for(task.kind)
        waiting = task.status in ("waiting_for_approval", "waiting_for_input")
        inline_form = task.kind in (
            "gmail.draft_reply",
            "gmail.send",
            "whatsapp.send",
            "calendar.create",
            "calendar.move",
        )
        rows.append(
            B.TaskInput(
                id=task.id,
                title=spec.label,
                status=task.status,  # type: ignore[arg-type]
                detail=_state_detail(task, whatsapp.get(task.id, "")),
                retriable=task.status == "failed"
                and not task.result.get("outcome_unknown")
                and task.attempts < MAX_ATTEMPTS,
                cancellable=(
                    task.status in ("pending", "running", "waiting_for_input")
                    and not (task.side_effect and task.status == "running")
                )
                or task.status == "waiting_for_approval",
                # Forms carry their own Send/Approve; this button is for kinds with no form.
                approvable=task.status == "waiting_for_approval" and not inline_form,
                viewable=False,
                editable=waiting and inline_form,
            )
        )
    finished = sum(1 for t in snapshot.tasks if t.status in K.TERMINAL)
    together = overlapping(snapshot.tasks)
    parts = [f"{finished} of {len(snapshot.tasks)} finished"]
    if together > 1:
        parts.append(f"{together} ran at the same time")
    if any(t.status == "waiting_for_approval" for t in snapshot.tasks):
        parts.append("nothing is changed until you approve")
    return B.task_board_widget(rows, demo=snapshot.plan.demo, summary=" · ".join(parts))


def _composer(task: TaskRow, detail: str | None) -> Widget | None:
    view = task.result.get("view")
    if not isinstance(view, dict) or view.get("channel") not in ("email", "message"):
        return None
    waiting = task.status in ("waiting_for_approval", "waiting_for_input")
    state: Any
    if task.status == "completed":
        state = "sent"
    elif task.status in ("failed", "cancelled"):
        state = "failed"
    else:
        state = "draft"
    return B.composer_widget(
        task.id,
        channel=view["channel"],
        to=str(view.get("to") or ""),
        subject=str(view["subject"]) if view.get("subject") is not None else None,
        body=str(view.get("body") or ""),
        state=state,
        detail=detail,
        demo=bool(view.get("demo")),
        editable=waiting,
    )


def _event(task: TaskRow, detail: str | None) -> Widget | None:
    view = task.result.get("view")
    if not isinstance(view, dict) or not isinstance(view.get("fields"), dict):
        return None
    fields = view["fields"]
    if "title" not in fields:
        return None
    state: Any = {
        "completed": "created",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(task.status, "draft")
    operation = view.get("operation")
    heading = {"move": "Move event", "remove": "Cancel this event?"}.get(
        str(operation), "New event"
    )
    return B.event_preview_widget(
        task.id,
        heading=heading,
        fields=B.EventFields(
            title=str(fields["title"]),
            date=str(fields["date"]),
            start=str(fields["start"]),
            minutes=int(fields["minutes"]),
            attendees=tuple(str(a) for a in fields.get("attendees", [])),
        ),
        conflicts=[
            B.EventConflict(str(c["title"]), str(c["start"]), str(c["end"]))
            for c in view.get("conflicts", [])
        ],
        state=state,
        detail=(
            f"Now {view['was']}. {detail or ''}".strip()
            if operation == "move" and task.status == "waiting_for_approval"
            else detail
            if task.status != "waiting_for_approval"
            else (
                "Invitations will be emailed to the people listed."
                if view.get("notify") and not view.get("demo")
                else detail
            )
        ),
        demo=bool(view.get("demo")),
        editable=operation != "remove"
        and task.status in ("waiting_for_approval", "waiting_for_input"),
    )


def view_widgets(task: TaskRow, detail: str | None) -> list[Widget]:
    """The widget(s) a task contributes, by what it is and where it has got to."""
    view = task.result.get("view")
    if not isinstance(view, dict):
        return []
    if task.status == "completed" and not task.side_effect:
        if "emails" in view:
            items = [
                B.EmailItem(
                    id=str(e["id"]),
                    sender=str(e["sender"]),
                    subject=str(e["subject"]),
                    snippet=str(e["snippet"]),
                    received=datetime.fromisoformat(str(e["received"])),
                    unread=bool(e["unread"]),
                    important=bool(e["important"]),
                    needs_reply=bool(e["needs_reply"]),
                )
                for e in view["emails"]
            ]
            return [B.email_list_widget(items, title=str(view["title"]), demo=bool(view["demo"]))]
        if "days" in view:
            days = tuple(
                B.DayInput(
                    d["date"],
                    tuple(
                        B.DayEvent(
                            e["id"],
                            e["title"],
                            _time(e["start"]) or "00:00",
                            _time(e["end"]) or "00:00",
                            e.get("detail"),
                        )
                        for e in d["events"]
                    ),
                )
                for d in view["days"]
            )
            return [B.calendar_view_widget(days, title=str(view["title"]), demo=bool(view["demo"]))]
        if "items" in view:
            return [planner_widget(view, editable=True)]
        return []
    if task.kind in ("gmail.draft_reply", "gmail.send", "whatsapp.send"):
        widget = _composer(task, detail)
        return [widget] if widget else []
    if task.kind in ("calendar.create", "calendar.move", "calendar.remove"):
        widget = _event(task, detail)
        return [widget] if widget else []
    return []


def planner_widget(view: dict[str, Any], *, editable: bool) -> Widget:
    return B.day_planner_widget(
        date=str(view["date"]),
        items=[
            B.PlannerItem(
                id=str(i["id"]),
                kind=i["kind"],
                title=str(i["title"]),
                time=_time(i["time"]) if i.get("time") else None,
                done=bool(i["done"]),
                priority=i["priority"],
                detail=str(i["detail"]) if i.get("detail") else None,
            )
            for i in view["items"]
        ],
        demo=bool(view.get("demo")),
        editable=editable,
    )


async def widgets_for(snapshot: Snapshot, messaging: MessagingClient | None) -> list[Widget]:
    whatsapp: dict[str, str] = {}
    for task in snapshot.tasks:
        if task.kind == "whatsapp.send" and task.status == "completed":
            whatsapp[task.id] = await _whatsapp_detail(task, messaging)
    widgets = [_board(snapshot, whatsapp)]
    for task in snapshot.tasks:
        widgets.extend(view_widgets(task, _state_detail(task, whatsapp.get(task.id, ""))))
    return widgets[:12]
