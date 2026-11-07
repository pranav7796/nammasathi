"""Reminder and note endpoints. The user always comes from the signed-in session."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from app.accounts.service import UNAVAILABLE_MESSAGE, AccountService, AccountsUnavailable
from app.accounts.store import User
from app.genui.builders import NoteInput, new_note_form, new_reminder_form, note_list_widget
from app.genui.validate import GenUIError, to_wire
from app.personal.dates import IST
from app.personal.service import PersonalService, WriteLimited, from_ms
from app.personal.store import (
    MAX_NOTE_BODY,
    MAX_REMINDER_TEXT,
    InvalidText,
    LimitReached,
    Note,
    NotFound,
    PersonalError,
    Reminder,
)
from app.personal.voice import reminders_ui


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReminderIn(_Strict):
    text: StrictStr = Field(max_length=MAX_REMINDER_TEXT * 2)
    due: str | None = Field(default=None, max_length=64)
    when_text: str | None = Field(default=None, max_length=MAX_REMINDER_TEXT * 2)


class ReminderPatch(_Strict):
    text: StrictStr | None = Field(default=None, max_length=MAX_REMINDER_TEXT * 2)
    due: str | None = Field(default=None, max_length=64)


class NoteIn(_Strict):
    body: StrictStr = Field(max_length=MAX_NOTE_BODY * 2)


def _iso(value: int | None) -> str | None:
    return None if value is None else from_ms(value).astimezone(IST).isoformat(timespec="seconds")


def reminder_json(item: Reminder) -> dict[str, object]:
    return {
        "id": item.id,
        "text": item.text,
        "due": _iso(item.due_at),
        "done": item.done_at is not None,
        "done_at": _iso(item.done_at),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def note_json(item: Note) -> dict[str, object]:
    return {
        "id": item.id,
        "body": item.body,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _due(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Use a date like 2026-11-08T19:00:00+05:30."
        ) from exc
    return moment.replace(tzinfo=IST) if moment.tzinfo is None else moment


def _fail(exc: PersonalError) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, WriteLimited):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, LimitReached):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidText):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def personal_router(accounts: AccountService | None = None) -> APIRouter:
    """With no argument, uses the accounts service the app started with."""
    router = APIRouter(prefix="/api/personal")
    services: dict[int, PersonalService] = {}

    async def me(request: Request) -> tuple[User, PersonalService]:
        found = accounts or cast(
            AccountService | None, getattr(request.app.state, "accounts", None)
        )
        if found is None:
            raise HTTPException(status_code=404, detail="Reminders are not available here.")
        try:
            await found.require_ready()
            user = await found.current_user(request)
        except AccountsUnavailable as exc:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from exc
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        service = services.setdefault(id(found), PersonalService(found))
        return user, service

    async def body[M: BaseModel](request: Request, model: type[M]) -> M:
        try:
            return model.model_validate_json(await request.body())
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="That request was not understood.") from exc

    @router.get("/reminders")
    async def list_reminders(request: Request) -> dict[str, object]:
        user, service = await me(request)
        items = await service.list_reminders(user.id)
        return {"reminders": [reminder_json(i) for i in items]}

    @router.post("/reminders")
    async def create_reminder(request: Request) -> dict[str, object]:
        user, service = await me(request)
        data = await body(request, ReminderIn)
        try:
            item = await service.create_reminder(
                user.id, data.text, _due(data.due), when_text=data.when_text
            )
        except PersonalError as exc:
            raise _fail(exc) from exc
        return reminder_json(item)

    @router.patch("/reminders/{reminder_id}")
    async def edit_reminder(request: Request, reminder_id: str) -> dict[str, object]:
        user, service = await me(request)
        data = await body(request, ReminderPatch)
        try:
            item = await service.edit_reminder(
                user.id,
                reminder_id,
                text=data.text,
                due=_due(data.due),
                change_due="due" in data.model_fields_set,
            )
        except PersonalError as exc:
            raise _fail(exc) from exc
        return reminder_json(item)

    async def mark(
        request: Request, reminder_id: str, action: Literal["done", "undo"]
    ) -> dict[str, object]:
        user, service = await me(request)
        try:
            if action == "done":
                item = await service.complete_reminder(user.id, reminder_id)
            else:
                item = await service.uncomplete_reminder(user.id, reminder_id)
        except PersonalError as exc:
            raise _fail(exc) from exc
        return reminder_json(item)

    @router.post("/reminders/{reminder_id}/done")
    async def done(request: Request, reminder_id: str) -> dict[str, object]:
        return await mark(request, reminder_id, "done")

    @router.post("/reminders/{reminder_id}/undo")
    async def undo(request: Request, reminder_id: str) -> dict[str, object]:
        return await mark(request, reminder_id, "undo")

    @router.delete("/reminders/{reminder_id}")
    async def delete_reminder(request: Request, reminder_id: str) -> dict[str, object]:
        user, service = await me(request)
        try:
            await service.delete_reminder(user.id, reminder_id)
        except PersonalError as exc:
            raise _fail(exc) from exc
        return {"deleted": True}

    @router.get("/panel")
    async def panel(request: Request) -> dict[str, object]:
        """The reminders and notes screen, built here so the browser only has to draw it."""
        user, service = await me(request)
        reminders = await service.list_reminders(user.id)
        notes = await service.list_notes(user.id)
        try:
            widgets = [
                new_reminder_form(),
                *reminders_ui(reminders[:50]),
                new_note_form(),
                note_list_widget(
                    [
                        NoteInput(id=n.id, body=n.body, updated=from_ms(n.updated_at))
                        for n in notes[:50]
                    ]
                ),
            ]
            return {"ui": to_wire(widgets)}
        except GenUIError as exc:
            raise HTTPException(status_code=500, detail="The panel could not be built.") from exc

    @router.get("/notes")
    async def list_notes(request: Request) -> dict[str, object]:
        user, service = await me(request)
        return {"notes": [note_json(i) for i in await service.list_notes(user.id)]}

    @router.post("/notes")
    async def create_note(request: Request) -> dict[str, object]:
        user, service = await me(request)
        data = await body(request, NoteIn)
        try:
            return note_json(await service.create_note(user.id, data.body))
        except PersonalError as exc:
            raise _fail(exc) from exc

    @router.patch("/notes/{note_id}")
    async def edit_note(request: Request, note_id: str) -> dict[str, object]:
        user, service = await me(request)
        data = await body(request, NoteIn)
        try:
            return note_json(await service.edit_note(user.id, note_id, data.body))
        except PersonalError as exc:
            raise _fail(exc) from exc

    @router.delete("/notes/{note_id}")
    async def delete_note(request: Request, note_id: str) -> dict[str, object]:
        user, service = await me(request)
        try:
            await service.delete_note(user.id, note_id)
        except PersonalError as exc:
            raise _fail(exc) from exc
        return {"deleted": True}

    return router
