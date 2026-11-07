"""Pydantic models for every component in docs/genui-spec.md.

Field names and limits follow the spec exactly. Everything is frozen, strict and rejects unknown
properties. All strings are checked for markup and "javascript:" as defence in depth (the browser
renders with textContent only).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.genui.actions import is_known_action

MAX_WIDGETS = 12
MAX_DEPTH = 3
MAX_CHILDREN = 40
MAX_TEXT = 2000
MAX_ITEMS = 100  # not in the spec; a sanity bound on every other collection
MAX_TABLE_ROWS = 50

_HTML = re.compile(r"<[A-Za-z/]")
_STRIP = re.compile(r"[\s\x00-\x1f\x7f]")


def _safe(value: str) -> str:
    if _HTML.search(value):
        raise ValueError("markup is not allowed in text")
    if "javascript:" in _STRIP.sub("", value).lower():
        raise ValueError("javascript: is not allowed")
    return value


def _action_id(value: str) -> str:
    if not is_known_action(value):
        raise ValueError("unknown action_id")
    return value


def _iso(value: str) -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("not an ISO-8601 string") from exc
    return value


def _iso_offset(value: str) -> str:
    if datetime.fromisoformat(_iso(value)).tzinfo is None:
        raise ValueError("ISO-8601 with offset required")
    return value


def _date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    return _iso(value)


def _hhmm(value: str) -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("expected HH:MM")
    return value


Text = Annotated[str, StringConstraints(max_length=MAX_TEXT), AfterValidator(_safe)]
Title = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT), AfterValidator(_safe)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=60), AfterValidator(_safe)]
Id = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.:-]{1,64}$")]
ActionId = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9_.:-]{1,64}$"), AfterValidator(_action_id)
]
Money = Annotated[str, StringConstraints(pattern=r"^\d{1,9}\.\d{2}$")]
IsoStr = Annotated[str, AfterValidator(_safe), AfterValidator(_iso)]
IsoOffset = Annotated[str, AfterValidator(_safe), AfterValidator(_iso_offset)]
DateStr = Annotated[str, AfterValidator(_date)]
HhMm = Annotated[str, AfterValidator(_hhmm)]
Status = Literal[
    "pending",
    "running",
    "waiting_for_input",
    "waiting_for_approval",
    "completed",
    "failed",
    "cancelled",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# ---------------------------------------------------------------- common types
class Action(_Model):
    label: Label
    action_id: ActionId
    style: Literal["primary", "secondary", "danger"] = "secondary"
    disabled: bool = False


class Option(_Model):
    value: Text
    label: Text


class Field_(_Model):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    label: Title
    kind: Literal["text", "textarea", "select", "checkbox", "date", "time", "number"]
    required: bool = False
    value: Text | Annotated[float, Field(allow_inf_nan=False)] | bool | None = None
    options: Annotated[list[Option], Field(min_length=1, max_length=30)] | None = None
    min: Annotated[float, Field(allow_inf_nan=False)] | None = None
    max: Annotated[float, Field(allow_inf_nan=False)] | None = None
    max_length: Annotated[int, Field(ge=1, le=MAX_TEXT)] | None = 500
    help: Text | None = None

    @model_validator(mode="after")
    def _check(self) -> Field_:
        if (self.kind == "select") != (self.options is not None):
            raise ValueError("options are required for select and only allowed there")
        return self


# ------------------------------------------------------------------ components
class _Widget(_Model):
    id: Id


class Heading(_Widget):
    type: Literal["heading"] = "heading"
    text: Title
    level: Annotated[int, Field(ge=1, le=3)]


class TextWidget(_Widget):
    type: Literal["text"] = "text"
    text: Text
    tone: Literal["normal", "muted", "warning"] = "normal"


class Card(_Widget):
    type: Literal["card"] = "card"
    title: Title
    body: Text | None = None
    children: Annotated[list[Widget], Field(max_length=MAX_CHILDREN)] = []
    actions: Annotated[list[Action], Field(max_length=8)] | None = None

    @model_validator(mode="after")
    def _depth(self) -> Card:
        if widget_depth(self) > MAX_DEPTH:
            raise ValueError(f"nesting deeper than {MAX_DEPTH}")
        return self


class ListItem(_Model):
    id: Id
    title: Title
    detail: Text | None = None
    badge: Text | None = None


class ListWidget(_Widget):
    type: Literal["list"] = "list"
    title: Text | None = None
    items: Annotated[list[ListItem], Field(max_length=MAX_ITEMS)]
    empty_text: Text


class ActionPanel(_Widget):
    type: Literal["action_panel"] = "action_panel"
    title: Title
    actions: Annotated[list[Action], Field(min_length=1, max_length=8)]


class Form(_Widget):
    type: Literal["form"] = "form"
    title: Title
    fields: Annotated[list[Field_], Field(min_length=1, max_length=12)]
    submit: Action

    @model_validator(mode="after")
    def _unique(self) -> Form:
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("duplicate field names")
        return self


class Task(_Model):
    id: Id
    title: Title
    status: Status
    detail: Text | None = None
    retry_action_id: ActionId | None = None
    cancel_action_id: ActionId | None = None


class TaskBoard(_Widget):
    type: Literal["task_board"] = "task_board"
    title: Title
    tasks: Annotated[list[Task], Field(max_length=MAX_ITEMS)]


class Progress(_Widget):
    type: Literal["progress"] = "progress"
    label: Title
    value: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class CartLine(_Model):
    id: Id
    name: Title
    quantity: Annotated[int, Field(ge=0, le=99)]
    unit_price_inr: Money
    detail: Text | None = None


class Cart(_Widget):
    type: Literal["cart"] = "cart"
    title: Title
    lines: Annotated[list[CartLine], Field(max_length=MAX_ITEMS)]
    total_inr: Money
    fees_inr: Money | None = None
    editable: bool
    demo: bool


class Table(_Widget):
    type: Literal["table"] = "table"
    title: Text | None = None
    columns: Annotated[list[Title], Field(min_length=1, max_length=8)]
    rows: Annotated[list[list[Text]], Field(max_length=MAX_TABLE_ROWS)]


class Detail(_Model):
    label: Title
    value: Text


class Confirmation(_Widget):
    type: Literal["confirmation"] = "confirmation"
    title: Title
    details: Annotated[list[Detail], Field(max_length=MAX_ITEMS)]
    consequence: Text
    confirm: Action
    reject: Action
    demo: bool


class Composer(_Widget):
    type: Literal["composer"] = "composer"
    channel: Literal["email", "message"]
    to: Text | None = None
    subject: Text | None = None
    body: Text
    send: Action
    copy_enabled: bool


class CalendarEvent(_Model):
    id: Id
    title: Title
    start: HhMm
    end: HhMm
    detail: Text | None = None


class CalendarDay(_Model):
    date: DateStr
    events: Annotated[list[CalendarEvent], Field(max_length=MAX_ITEMS)]


class CalendarView(_Widget):
    type: Literal["calendar_view"] = "calendar_view"
    title: Title
    days: Annotated[list[CalendarDay], Field(max_length=MAX_ITEMS)]


class ReminderItem(_Model):
    id: Id
    text: Title
    due: IsoStr | None = None  # to_wire omits null; absent means no due time
    done: bool


class ReminderList(_Widget):
    type: Literal["reminder_list"] = "reminder_list"
    title: Title
    items: Annotated[list[ReminderItem], Field(max_length=MAX_ITEMS)]


class NoteItem(_Model):
    id: Id
    body: Text
    updated: IsoStr


class NoteList(_Widget):
    type: Literal["note_list"] = "note_list"
    title: Title
    items: Annotated[list[NoteItem], Field(max_length=MAX_ITEMS)]


class ShareCalendar(_Model):
    title: Title
    start: IsoOffset
    end: IsoOffset
    location: Text | None = None
    description: Text | None = None


class SharePanel(_Widget):
    type: Literal["share_panel"] = "share_panel"
    title: Title
    summary: Text
    calendar: ShareCalendar | None = None


class StatusWidget(_Widget):
    type: Literal["status"] = "status"
    kind: Literal["loading", "success", "empty", "error"]
    message: Text
    retry: Action | None = None


Widget = Annotated[
    Heading
    | TextWidget
    | Card
    | ListWidget
    | ActionPanel
    | Form
    | TaskBoard
    | Progress
    | Cart
    | Table
    | Confirmation
    | Composer
    | CalendarView
    | ReminderList
    | NoteList
    | SharePanel
    | StatusWidget,
    Field(discriminator="type"),
]


def widget_depth(widget: object) -> int:
    """1 for a leaf; a card is 1 + its deepest child."""
    if isinstance(widget, Card):
        return 1 + max((widget_depth(c) for c in widget.children), default=0)
    return 1


Card.model_rebuild()
