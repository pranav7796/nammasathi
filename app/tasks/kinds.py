"""The only things a plan may contain. A closed catalogue, each with strictly validated arguments.

The language model (or the rule-based fallback) proposes a list of `{kind, args}`. Nothing here is
executable by the model: a kind outside this catalogue, or arguments that fail validation, are
rejected before anything runs. `side_effect` kinds change something outside Saathi (or are shown
for approval for consistency) and can only run after the person approves the exact arguments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date
from datetime import time as Time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Status = Literal[
    "pending",
    "running",
    "waiting_for_input",
    "waiting_for_approval",
    "completed",
    "failed",
    "cancelled",
]
TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]+)+$"
)
_PHONE = re.compile(r"^\+[1-9][0-9]{7,14}$")

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def clean_emails(values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(v.strip().lower() for v in values)
    for value in cleaned:
        if len(value) > 254 or not EMAIL_PATTERN.match(value):
            raise ValueError("not an email address")
    return cleaned


class Nothing(_Args):
    pass


class Limit(_Args):
    limit: int = Field(default=5, ge=1, le=10)


class GmailSearch(_Args):
    query: ShortText
    limit: int = Field(default=5, ge=1, le=10)


class DraftReply(_Args):
    """Who to write to and what to say. The words are the person's, placed in a plain template."""

    to: ShortText  # a contact name or an address: resolved against saved contacts, never guessed
    intent: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    subject: str = Field(default="", max_length=200)
    thread_id: str | None = Field(default=None, max_length=100)
    in_reply_to: str | None = Field(default=None, max_length=300)


class GmailSend(_Args):
    to: tuple[str, ...] = Field(min_length=1, max_length=5)
    subject: ShortText
    body: LongText
    thread_id: str | None = Field(default=None, max_length=100)
    in_reply_to: str | None = Field(default=None, max_length=300)

    _addresses = field_validator("to")(clean_emails)


class Upcoming(_Args):
    days: int = Field(default=3, ge=1, le=14)


class Availability(_Args):
    date: Date
    start: Time
    minutes: int = Field(default=30, ge=5, le=480)


class CalendarCreate(_Args):
    title: ShortText
    date: Date
    start: Time
    minutes: int = Field(default=30, ge=5, le=480)
    attendees: tuple[str, ...] = Field(default=(), max_length=10)

    _addresses = field_validator("attendees")(clean_emails)


class CalendarReschedule(_Args):
    event_id: Annotated[
        str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")
    ]
    date: Date
    start: Time
    minutes: int | None = Field(default=None, ge=5, le=480)


class CalendarCancel(_Args):
    event_id: Annotated[
        str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")
    ]


class CalendarMove(_Args):
    """ "Move my meeting with Rahul to 4 PM": the event is found by words, never by an id."""

    find: ShortText  # words from the event's title or an attendee
    on: Date | None = None  # which day to look on; otherwise the next two weeks
    date: Date | None = None  # the new day; None keeps the event's own day
    start: Time
    minutes: int | None = Field(default=None, ge=5, le=480)


class CalendarRemove(_Args):
    find: ShortText
    on: Date | None = None


class ReminderCreate(_Args):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    when_text: str | None = Field(default=None, max_length=100)


class WhatsAppSend(_Args):
    to: ShortText  # a saved contact's name or a +E.164 number
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


@dataclass(frozen=True)
class KindSpec:
    kind: str
    provider: Literal["gmail", "calendar", "whatsapp", "local"]
    args: type[_Args]
    side_effect: bool
    label: str  # shown on the task board; the executor may add detail
    editable: tuple[str, ...] = ()  # argument names a person may change before approving


CATALOGUE: dict[str, KindSpec] = {
    spec.kind: spec
    for spec in (
        KindSpec("gmail.search", "gmail", GmailSearch, False, "Search email"),
        KindSpec("gmail.important", "gmail", Limit, False, "Find important email"),
        KindSpec("gmail.needs_reply", "gmail", Limit, False, "Find email that needs a reply"),
        KindSpec(
            "gmail.draft_reply",
            "gmail",
            DraftReply,
            True,
            "Draft and send an email",
            ("to", "subject", "body"),
        ),
        KindSpec("gmail.send", "gmail", GmailSend, True, "Send email", ("to", "subject", "body")),
        KindSpec("calendar.today", "calendar", Nothing, False, "Today's schedule"),
        KindSpec("calendar.upcoming", "calendar", Upcoming, False, "Upcoming events"),
        KindSpec("calendar.availability", "calendar", Availability, False, "Check availability"),
        KindSpec(
            "calendar.create",
            "calendar",
            CalendarCreate,
            True,
            "Create calendar event",
            ("title", "date", "start", "minutes", "attendees"),
        ),
        KindSpec(
            "calendar.reschedule",
            "calendar",
            CalendarReschedule,
            True,
            "Move calendar event",
            ("date", "start", "minutes"),
        ),
        KindSpec("calendar.cancel", "calendar", CalendarCancel, True, "Cancel calendar event"),
        KindSpec(
            "calendar.move",
            "calendar",
            CalendarMove,
            True,
            "Move calendar event",
            ("date", "start", "minutes"),
        ),
        KindSpec("calendar.remove", "calendar", CalendarRemove, True, "Cancel calendar event"),
        KindSpec(
            "reminder.create",
            "local",
            ReminderCreate,
            True,
            "Save reminder",
            ("text", "when_text"),
        ),
        KindSpec(
            "whatsapp.send", "whatsapp", WhatsAppSend, True, "Send WhatsApp message", ("to", "body")
        ),
        KindSpec("day.plan", "local", Nothing, False, "Plan my day"),
    )
}


class UnknownKind(ValueError):
    pass


def spec_for(kind: str) -> KindSpec:
    try:
        return CATALOGUE[kind]
    except KeyError as exc:
        raise UnknownKind(f"unknown task kind: {kind[:40]!r}") from exc


def validate_args(kind: str, raw: dict[str, object]) -> _Args:
    """Raises UnknownKind or pydantic.ValidationError. Never returns partly valid arguments."""
    return spec_for(kind).args.model_validate(raw)


def is_phone(value: str) -> bool:
    return bool(_PHONE.match(value))
