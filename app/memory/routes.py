"""Memory and contact endpoints. The user always comes from the signed-in session."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from app.accounts.service import UNAVAILABLE_MESSAGE, AccountService, AccountsUnavailable
from app.accounts.store import User
from app.memory.service import MemoryService, WriteLimited
from app.memory.store import (
    MEMORY_KEYS,
    Contact,
    DuplicateName,
    InvalidValue,
    LimitReached,
    MemoryError_,
    MemoryItem,
    NotFound,
)

try:  # The panel answers 501 if the widget builder is missing.
    from app.genui.builders import MemoryItem as _WidgetItem
    from app.genui.builders import memory_list_widget as _widget
except ImportError:  # pragma: no cover
    _widget = None
    _WidgetItem = None

CONSENT = (
    "Saathi keeps only what you ask it to keep. You can see, change or delete any of it "
    "here at any time. It is never used for anything else."
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueIn(_Strict):
    value: StrictStr = Field(max_length=1000)


class ContactIn(_Strict):
    name: StrictStr = Field(max_length=400)
    email: StrictStr | None = Field(default=None, max_length=400)
    phone: StrictStr | None = Field(default=None, max_length=100)


class ContactPatch(_Strict):
    name: StrictStr | None = Field(default=None, max_length=400)
    email: StrictStr | None = Field(default=None, max_length=400)
    phone: StrictStr | None = Field(default=None, max_length=100)


def item_json(item: MemoryItem) -> dict[str, object]:
    label, limit = MEMORY_KEYS[item.key]
    return {
        "key": item.key,
        "label": label,
        "value": item.value,
        "max_length": limit,
        "updated_at": item.updated_at,
    }


def contact_json(item: Contact) -> dict[str, object]:
    return {
        "id": item.id,
        "name": item.name,
        "email": item.email,
        "phone": item.phone,
        "created_at": item.created_at,
    }


def _fail(exc: MemoryError_) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, WriteLimited):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, (LimitReached, DuplicateName)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidValue):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def memory_router(accounts: AccountService | None = None) -> APIRouter:
    """With no argument, uses the accounts service the app started with."""
    router = APIRouter(prefix="/api")
    services: dict[int, MemoryService] = {}

    async def me(request: Request) -> tuple[User, MemoryService]:
        found = accounts or cast(
            AccountService | None, getattr(request.app.state, "accounts", None)
        )
        if found is None:
            raise HTTPException(status_code=404, detail="Memory is not available here.")
        try:
            await found.require_ready()
            user = await found.current_user(request)
        except AccountsUnavailable as exc:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from exc
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        service = services.setdefault(id(found), MemoryService(found))
        return user, service

    async def body[M: BaseModel](request: Request, model: type[M]) -> M:
        try:
            return model.model_validate_json(await request.body())
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="That request was not understood.") from exc

    @router.get("/memory")
    async def list_memory(request: Request) -> dict[str, object]:
        user, service = await me(request)
        items = await service.list_items(user.id)
        return {
            "consent": CONSENT,
            "items": [item_json(i) for i in items],
            "allowed": [
                {"key": k, "label": label, "max_length": n} for k, (label, n) in MEMORY_KEYS.items()
            ],
        }

    @router.get("/memory/panel")
    async def panel(request: Request) -> dict[str, object]:
        user, service = await me(request)
        if _widget is None or _WidgetItem is None:
            raise HTTPException(status_code=501, detail="The memory panel is not available yet.")
        items = await service.list_items(user.id)
        from app.genui.validate import GenUIError, to_wire

        try:
            return {
                "ui": to_wire(
                    [
                        _widget(
                            [_WidgetItem(i.key, MEMORY_KEYS[i.key][0], i.value) for i in items],
                            consent_text=CONSENT,
                        )
                    ]
                )
            }
        except (GenUIError, TypeError) as exc:
            raise HTTPException(status_code=500, detail="The panel could not be built.") from exc

    @router.put("/memory/{key}")
    async def put_memory(request: Request, key: str) -> dict[str, object]:
        user, service = await me(request)
        data = await body(request, ValueIn)
        try:
            return item_json(await service.set_item(user.id, key, data.value))
        except MemoryError_ as exc:
            raise _fail(exc) from exc

    @router.delete("/memory/{key}")
    async def delete_memory(request: Request, key: str) -> dict[str, object]:
        user, service = await me(request)
        try:
            await service.forget(user.id, key)
        except MemoryError_ as exc:
            raise _fail(exc) from exc
        return {"deleted": True}

    @router.delete("/memory")
    async def forget_all(request: Request) -> dict[str, object]:
        user, service = await me(request)
        try:
            await service.forget_all(user.id)
        except MemoryError_ as exc:
            raise _fail(exc) from exc
        return {"deleted": True}

    @router.get("/contacts")
    async def list_contacts(request: Request) -> dict[str, object]:
        user, service = await me(request)
        return {"contacts": [contact_json(c) for c in await service.list_contacts(user.id)]}

    @router.post("/contacts")
    async def create_contact(request: Request) -> dict[str, object]:
        user, service = await me(request)
        data = await body(request, ContactIn)
        try:
            return contact_json(
                await service.create_contact(user.id, data.name, data.email, data.phone)
            )
        except MemoryError_ as exc:
            raise _fail(exc) from exc

    @router.patch("/contacts/{contact_id}")
    async def edit_contact(request: Request, contact_id: str) -> dict[str, object]:
        user, service = await me(request)
        data = await body(request, ContactPatch)
        try:
            return contact_json(
                await service.update_contact(
                    user.id,
                    contact_id,
                    name=data.name,
                    email=data.email,
                    phone=data.phone,
                    change_email="email" in data.model_fields_set,
                    change_phone="phone" in data.model_fields_set,
                )
            )
        except MemoryError_ as exc:
            raise _fail(exc) from exc

    @router.delete("/contacts/{contact_id}")
    async def delete_contact(request: Request, contact_id: str) -> dict[str, object]:
        user, service = await me(request)
        try:
            await service.delete_contact(user.id, contact_id)
        except MemoryError_ as exc:
            raise _fail(exc) from exc
        return {"deleted": True}

    return router
