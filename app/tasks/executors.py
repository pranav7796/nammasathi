"""What each task kind does. Three shapes, kept apart on purpose:

* `read`   runs by itself (read-only, already authorised by the person's own connection).
* `prepare` for kinds that change something: builds the exact thing to approve. It may READ
  (resolve a contact, look for clashes) but never writes anywhere.
* `effect` runs only after the person approved the exact arguments `prepare` produced.

Everything an external service returns (email text, event titles) is untrusted data. It is carried
as plain strings into views and never interpreted, never used to pick a task, argument or memory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from datetime import time as Time
from typing import Annotated, Any, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from app.integrations.base import (
    CalendarClient,
    EmailClient,
    EventDraft,
    MessagingClient,
    OutgoingEmail,
    OutgoingMessage,
)
from app.personal.dates import IST, parse_when
from app.personal.service import PersonalService, from_ms
from app.tasks import kinds as K
from app.tasks.demo import DEMO_CONTACTS


@dataclass(frozen=True)
class ResolvedContact:
    kind: Literal["one", "none", "ambiguous"]
    name: str = ""
    email: str | None = None
    phone: str | None = None
    candidates: tuple[str, ...] = ()


class ContactResolver(Protocol):
    async def resolve(
        self, user_id: str, spoken: str, channel: Literal["email", "phone"]
    ) -> ResolvedContact: ...


@dataclass
class Services:
    """Everything an executor may use for one person. `demo` says which providers are practice."""

    email: EmailClient
    calendar: CalendarClient
    messaging: MessagingClient
    contacts: ContactResolver
    personal: PersonalService
    demo: dict[str, bool]


@dataclass(frozen=True)
class Ctx:
    user_id: str
    services: Services
    now: datetime  # timezone-aware, IST
    language: str


class DemoContacts:
    """A real resolver, plus a few made-up people so a demo walk-through still works.

    The made-up people are used only for a channel that is itself a demo, so an invented address
    can never be put on a real email, and an invented number never on a real WhatsApp message.
    """

    def __init__(self, real: ContactResolver | None, *, demo_email: bool, demo_phone: bool) -> None:
        self._real = real
        self._demo = {"email": demo_email, "phone": demo_phone}

    async def resolve(
        self, user_id: str, spoken: str, channel: Literal["email", "phone"]
    ) -> ResolvedContact:
        if self._real is not None:
            found = await self._real.resolve(user_id, spoken, channel)
            if found.kind != "none":
                return found
        known = DEMO_CONTACTS.get(spoken.strip().casefold()) if self._demo[channel] else None
        if known is None:
            return ResolvedContact("none")
        return ResolvedContact("one", known[0], known[1], known[2])


@dataclass(frozen=True)
class Prepared:
    effect_args: dict[str, Any]  # what `effect` will run with: normalised and resolved
    view: dict[str, Any]  # JSON data the renderer draws
    needs_input: str | None = None  # a plain-language reason the person must supply something


@dataclass(frozen=True)
class Outcome:
    view: dict[str, Any]
    verified: bool
    detail: str = ""


class EffectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmailDraftArgs(EffectArgs):
    to: tuple[str, ...] = Field(min_length=1, max_length=5)
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
    thread_id: str | None = Field(default=None, max_length=100)
    in_reply_to: str | None = Field(default=None, max_length=300)

    @field_validator("to")
    @classmethod
    def _emails(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return K.clean_emails(values)


class MessageArgs(EffectArgs):
    to_label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    to_phone: Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{7,14}$")]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


def args_hash(kind: str, effect_args: dict[str, Any]) -> str:
    """What the person approved. Any change to any argument changes this."""
    canonical = json.dumps([kind, effect_args], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _hhmm(moment: datetime) -> str:
    return moment.astimezone(IST).strftime("%H:%M")


def _slot(day: Date, start: Time, minutes: int) -> tuple[datetime, datetime]:
    begin = datetime.combine(day, start, tzinfo=IST)
    return begin, begin + timedelta(minutes=minutes)


def _email_view(items: list[Any], title: str, demo: bool) -> dict[str, Any]:
    return {
        "title": title,
        "demo": demo,
        "emails": [
            {
                "id": s.id,
                "sender": s.sender_name or s.sender_email,
                "subject": s.subject,
                "snippet": s.snippet,
                "received": s.received.isoformat(),
                "unread": s.unread,
                "important": s.important,
                "needs_reply": s.needs_reply,
            }
            for s in items
        ],
    }


def _days_view(events: list[Any], title: str, demo: bool) -> dict[str, Any]:
    days: dict[str, list[dict[str, str]]] = {}
    for event in events:
        key = event.start.astimezone(IST).date().isoformat()
        days.setdefault(key, []).append(
            {
                "id": event.id,
                "title": event.title,
                "start": _hhmm(event.start),
                "end": _hhmm(event.end),
                "detail": ", ".join(event.attendees[:3]),
            }
        )
    return {
        "title": title,
        "demo": demo,
        "days": [{"date": k, "events": v} for k, v in sorted(days.items())],
    }


# Read-only ---------------------------------------------------------------------------------------


async def read(
    kind: str, ctx: Ctx, args: Any, upstream: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    s = ctx.services
    if kind == "gmail.search":
        found = await s.email.search(ctx.user_id, args.query, args.limit)
        return _email_view(found, "Search results", s.demo["gmail"])
    if kind == "gmail.important":
        found = await s.email.important(ctx.user_id, args.limit)
        return _email_view(found, "Important email", s.demo["gmail"])
    if kind == "gmail.needs_reply":
        found = await s.email.needs_reply(ctx.user_id, args.limit)
        return _email_view(found, "Email that needs a reply", s.demo["gmail"])
    if kind == "calendar.today":
        start = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = await s.calendar.events(ctx.user_id, start, start + timedelta(days=1))
        return _days_view(events, "Today", s.demo["calendar"])
    if kind == "calendar.upcoming":
        start = ctx.now
        events = await s.calendar.events(ctx.user_id, start, start + timedelta(days=args.days))
        return _days_view(events, f"Next {args.days} days", s.demo["calendar"])
    if kind == "calendar.availability":
        begin, end = _slot(args.date, args.start, args.minutes)
        clashes = await s.calendar.conflicts(ctx.user_id, begin, end)
        return {
            "demo": s.demo["calendar"],
            "window": {"date": args.date.isoformat(), "start": _hhmm(begin), "end": _hhmm(end)},
            "free": not clashes,
            "conflicts": [
                {"title": e.title, "start": _hhmm(e.start), "end": _hhmm(e.end)} for e in clashes
            ],
        }
    if kind == "day.plan":
        return await _day_plan(ctx)
    raise K.UnknownKind(kind)


def _item_id(kind: str, ref: str) -> str:
    return kind[0] + hashlib.sha1(f"{kind}:{ref}".encode()).hexdigest()[:12]


async def _day_plan(ctx: Ctx) -> dict[str, Any]:
    s = ctx.services
    day = ctx.now.astimezone(IST).date()
    start = datetime.combine(day, Time(0, 0), tzinfo=IST)
    items: list[dict[str, Any]] = []
    events = await s.calendar.events(ctx.user_id, start, start + timedelta(days=1))
    for event in events:
        items.append(
            {
                "id": _item_id("event", event.id),
                "kind": "event",
                "title": event.title,
                "time": _hhmm(event.start),
                "done": False,
                "priority": "normal",
                "detail": f"until {_hhmm(event.end)}",
                "ref": event.id,
            }
        )
    for reminder in await s.personal.list_reminders(ctx.user_id):
        if reminder.done_at is not None:
            continue
        due = None if reminder.due_at is None else from_ms(reminder.due_at)
        if due is not None and due.date() > day:
            continue
        items.append(
            {
                "id": _item_id("reminder", reminder.id),
                "kind": "reminder",
                "title": reminder.text,
                "time": None if due is None else _hhmm(due),
                "done": False,
                "priority": "high" if due is not None and due < ctx.now else "normal",
                "detail": "overdue" if due is not None and due < ctx.now else "",
                "ref": reminder.id,
            }
        )
    try:
        waiting = await s.email.needs_reply(ctx.user_id, 3)
    except Exception:
        waiting = []
    for mail in waiting:
        items.append(
            {
                "id": _item_id("email", mail.id),
                "kind": "email",
                "title": f"Reply to {mail.sender_name or mail.sender_email}",
                "time": None,
                "done": False,
                "priority": "normal",
                "detail": mail.subject,
                "ref": mail.id,
            }
        )
    items.sort(key=lambda i: (i["time"] is None, i["time"] or "", i["title"]))
    demo = any(s.demo.values())
    return {"date": day.isoformat(), "demo": demo, "items": items[:40]}


# Prepared (waiting for approval) ----------------------------------------------------------------

_NO_ADDRESS = "I don't have an email address saved for {name}. Type it in the To box."
_NO_NUMBER = "I don't have a WhatsApp number saved for {name}. Type it as +91… in the To box."
_AMBIGUOUS = "More than one contact matches {name} ({options}). Type the exact one in the To box."


async def prepare(kind: str, ctx: Ctx, args: Any, upstream: dict[int, dict[str, Any]]) -> Prepared:
    s = ctx.services
    if kind == "gmail.draft_reply":
        recipient: tuple[str, ...] = ()
        reason: str | None = None
        if K.EMAIL_PATTERN.match(args.to.strip()):
            recipient = (args.to.strip().lower(),)
        else:
            found = await s.contacts.resolve(ctx.user_id, args.to, "email")
            if found.kind == "one" and found.email:
                recipient = (found.email.lower(),)
            elif found.kind == "ambiguous":
                reason = _AMBIGUOUS.format(name=args.to, options=", ".join(found.candidates[:4]))
            else:
                reason = _NO_ADDRESS.format(name=args.to)
        greeting = args.to.split("@")[0].split()[0].capitalize() if args.to else "there"
        subject = args.subject or "Re: your message"
        body = f"Hi {greeting},\n\n{args.intent.strip()}\n\nThanks,"
        effect: dict[str, Any] = {
            "to": list(recipient),
            "subject": subject,
            "body": body,
            "thread_id": args.thread_id,
            "in_reply_to": args.in_reply_to,
        }
        view = {
            "channel": "email",
            "to": ", ".join(recipient) or args.to,
            "subject": subject,
            "body": body,
            "demo": s.demo["gmail"],
        }
        return Prepared(effect, view, reason)
    if kind == "gmail.send":
        effect = {
            "to": list(args.to),
            "subject": args.subject,
            "body": args.body,
            "thread_id": args.thread_id,
            "in_reply_to": args.in_reply_to,
        }
        view = {
            "channel": "email",
            "to": ", ".join(args.to),
            "subject": args.subject,
            "body": args.body,
            "demo": s.demo["gmail"],
        }
        return Prepared(effect, view)
    if kind == "calendar.create":
        begin, end = _slot(args.date, args.start, args.minutes)
        known = upstream_conflicts(upstream, args.date, args.start, args.minutes)
        if known is None:
            clashes = await s.calendar.conflicts(ctx.user_id, begin, end)
            known = [
                {"title": e.title, "start": _hhmm(e.start), "end": _hhmm(e.end)} for e in clashes
            ]
        effect = {
            "title": args.title,
            "date": args.date.isoformat(),
            "start": args.start.strftime("%H:%M"),
            "minutes": args.minutes,
            "attendees": list(args.attendees),
        }
        view = {
            "fields": effect,
            "conflicts": known,
            "notify": bool(args.attendees),
            "demo": s.demo["calendar"],
        }
        return Prepared(effect, view)
    if kind == "calendar.reschedule":
        begin, end = _slot(args.date, args.start, args.minutes or 30)
        effect = {
            "event_id": args.event_id,
            "date": args.date.isoformat(),
            "start": args.start.strftime("%H:%M"),
            "minutes": args.minutes or 30,
        }
        clashes = await s.calendar.conflicts(ctx.user_id, begin, end)
        view = {
            "fields": effect,
            "conflicts": [
                {"title": e.title, "start": _hhmm(e.start), "end": _hhmm(e.end)}
                for e in clashes
                if e.id != args.event_id
            ],
            "demo": s.demo["calendar"],
        }
        return Prepared(effect, view)
    if kind == "calendar.cancel":
        effect = {"event_id": args.event_id}
        return Prepared(effect, {"fields": effect, "conflicts": [], "demo": s.demo["calendar"]})
    if kind == "reminder.create":
        text = args.text
        parsed = parse_when(args.when_text, ctx.now) if args.when_text else None
        if parsed is None:
            parsed = parse_when(text, ctx.now)
            if parsed is not None and parsed.text:
                text = parsed.text
        effect = {"text": text, "due": None if parsed is None else parsed.due.isoformat()}
        return Prepared(effect, {"text": text, "due": effect["due"], "demo": False})
    if kind == "whatsapp.send":
        label = args.to.strip()
        phone: str | None = None
        reason = None
        if K.is_phone(label.replace(" ", "").replace("-", "")):
            phone = label.replace(" ", "").replace("-", "")
        else:
            found = await s.contacts.resolve(ctx.user_id, label, "phone")
            if found.kind == "one" and found.phone:
                label, phone = found.name or label, found.phone
            elif found.kind == "ambiguous":
                reason = _AMBIGUOUS.format(name=label, options=", ".join(found.candidates[:4]))
            else:
                reason = _NO_NUMBER.format(name=label)
        effect = {"to_label": label, "to_phone": phone or "", "body": args.body}
        view = {
            "channel": "message",
            "to": phone or label,
            "to_label": label,
            "body": args.body,
            "demo": s.demo["whatsapp"],
        }
        return Prepared(effect, view, reason)
    raise K.UnknownKind(kind)


def upstream_conflicts(
    upstream: dict[int, dict[str, Any]], day: Date, start: Time, minutes: int
) -> list[dict[str, str]] | None:
    """Reuse an availability task's answer when it checked this very slot (dependency in use)."""
    for result in upstream.values():
        window = cast(dict[str, Any], result.get("window") or {})
        if (
            window.get("date") == day.isoformat()
            and window.get("start") == start.strftime("%H:%M")
            and isinstance(result.get("conflicts"), list)
        ):
            return [dict(c) for c in result["conflicts"]]
    return None


# Edits (before approval) -------------------------------------------------------------------------


class EditRejected(ValueError):
    """The change was not allowed or did not validate. The message is safe to show."""


def _lines(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in cast(list[object], list(cast(Any, value)))]
        return [v for v in parts if v]
    return [p.strip() for p in str(value).replace(",", "\n").splitlines() if p.strip()]


async def apply_edit(
    kind: str, ctx: Ctx, effect: dict[str, Any], changes: dict[str, Any]
) -> tuple[dict[str, Any], Prepared]:
    """Merge `changes` into the effect arguments, validate, and rebuild the preview."""
    spec = K.spec_for(kind)
    allowed = {"gmail.draft_reply": ("to", "subject", "body"), "whatsapp.send": ("to", "body")}.get(
        kind, spec.editable
    )
    unknown = set(changes) - set(allowed)
    if unknown:
        raise EditRejected("That can't be changed here.")
    merged = dict(effect)
    try:
        if kind in ("gmail.draft_reply", "gmail.send"):
            if "to" in changes:
                merged["to"] = _lines(changes["to"])
            for key in ("subject", "body"):
                if key in changes:
                    merged[key] = str(changes[key])
            EmailDraftArgs.model_validate(merged)
            view = {
                "channel": "email",
                "to": ", ".join(merged["to"]),
                "subject": merged["subject"].strip(),
                "body": merged["body"].strip(),
                "demo": ctx.services.demo["gmail"],
            }
            clean = EmailDraftArgs.model_validate(merged).model_dump(mode="json")
            return clean, Prepared(clean, view)
        if kind == "whatsapp.send":
            if "body" in changes:
                merged["body"] = str(changes["body"])
            if "to" in changes:
                spoken = str(changes["to"]).strip()
                squeezed = spoken.replace(" ", "").replace("-", "")
                if K.is_phone(squeezed):
                    merged["to_phone"], merged["to_label"] = squeezed, squeezed
                else:
                    found = await ctx.services.contacts.resolve(ctx.user_id, spoken, "phone")
                    if found.kind != "one" or not found.phone:
                        raise EditRejected("I couldn't find a number for that. Type it as +91…")
                    merged["to_phone"], merged["to_label"] = found.phone, found.name or spoken
            clean = MessageArgs.model_validate(merged).model_dump(mode="json")
            view = {
                "channel": "message",
                "to": clean["to_phone"],
                "to_label": clean["to_label"],
                "body": clean["body"],
                "demo": ctx.services.demo["whatsapp"],
            }
            return clean, Prepared(clean, view)
        if kind == "calendar.create":
            for key in ("title", "date", "start", "minutes"):
                if key in changes:
                    merged[key] = changes[key]
            if "attendees" in changes:
                merged["attendees"] = _lines(changes["attendees"])
            checked = K.CalendarCreate.model_validate(merged)
            prepared = await prepare("calendar.create", ctx, checked, {})
            return prepared.effect_args, prepared
        if kind == "calendar.reschedule":
            for key in ("date", "start", "minutes"):
                if key in changes:
                    merged[key] = changes[key]
            checked_r = K.CalendarReschedule.model_validate(merged)
            prepared = await prepare("calendar.reschedule", ctx, checked_r, {})
            return prepared.effect_args, prepared
        if kind == "reminder.create":
            if "text" in changes:
                merged["text"] = str(changes["text"])
            when = changes.get("when_text")
            checked_m = K.ReminderCreate.model_validate(
                {"text": merged["text"], "when_text": when if when else None}
            )
            prepared = await prepare("reminder.create", ctx, checked_m, {})
            return prepared.effect_args, prepared
    except ValidationError as exc:
        raise EditRejected(_first_error(exc)) from exc
    raise EditRejected("That can't be changed here.")


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"]) or "value"
    return f"Please check {where}: {first['msg']}"


# Effects (after approval) ------------------------------------------------------------------------


async def effect(kind: str, ctx: Ctx, effect_args: dict[str, Any], key: str) -> Outcome:
    s = ctx.services
    if kind in ("gmail.draft_reply", "gmail.send"):
        draft = EmailDraftArgs.model_validate(effect_args)
        sent = await s.email.send(
            ctx.user_id,
            OutgoingEmail(
                to=draft.to,
                subject=draft.subject,
                body=draft.body,
                thread_id=draft.thread_id,
                in_reply_to=draft.in_reply_to,
            ),
            key,
        )
        return Outcome({"sent_id": sent.id}, sent.verified, "" if sent.verified else "unverified")
    if kind == "whatsapp.send":
        message = MessageArgs.model_validate(effect_args)
        sent_message = await s.messaging.send(
            ctx.user_id, OutgoingMessage(message.to_phone, message.to_label, message.body), key
        )
        return Outcome(
            {"message_id": sent_message.id, "status": sent_message.status},
            sent_message.status != "failed",
            sent_message.detail,
        )
    if kind == "calendar.create":
        checked = K.CalendarCreate.model_validate(effect_args)
        begin, end = _slot(checked.date, checked.start, checked.minutes)
        created = await s.calendar.create(
            ctx.user_id,
            EventDraft(
                checked.title,
                begin,
                end,
                checked.attendees,
                notify_attendees=bool(checked.attendees),
            ),
            key,
        )
        return Outcome({"event_id": created.event.id}, created.verified)
    if kind == "calendar.reschedule":
        moved = K.CalendarReschedule.model_validate(effect_args)
        begin, end = _slot(moved.date, moved.start, moved.minutes or 30)
        rescheduled = await s.calendar.reschedule(ctx.user_id, moved.event_id, begin, end)
        return Outcome({"event_id": rescheduled.event.id}, rescheduled.verified)
    if kind == "calendar.cancel":
        cancelled = K.CalendarCancel.model_validate(effect_args)
        gone = await s.calendar.cancel(ctx.user_id, cancelled.event_id)
        return Outcome({"event_id": cancelled.event_id}, gone)
    if kind == "reminder.create":
        parsed = K.ReminderCreate.model_validate({"text": effect_args["text"]})
        due = (
            None if not effect_args.get("due") else datetime.fromisoformat(str(effect_args["due"]))
        )
        saved = await s.personal.create_reminder(ctx.user_id, parsed.text, due)
        return Outcome({"reminder_id": saved.id}, True)
    raise K.UnknownKind(kind)
