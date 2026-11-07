"""HTTP for the task board: poll a plan, and send an action from a widget.

Every route needs a signed-in person and acts only on that person's own tasks. A widget action id
is a request, never a command: the engine checks ownership, status and the approved content again.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.accounts.service import UNAVAILABLE_MESSAGE, AccountService, AccountsUnavailable
from app.accounts.store import User
from app.genui.models import Widget
from app.genui.sanitize import clean_text
from app.genui.validate import GenUIError, to_wire, validate_widget
from app.tasks import render
from app.tasks.engine import Snapshot, TaskEngine

_MESSAGES = {
    "not_found": "I couldn't find that task.",
    "not_waiting": "That's no longer waiting for approval.",
    "needs_input": "I still need a detail first. Fill it in, then send.",
    "stale": "That changed since you looked. Check it again, then approve.",
    "not_editable": "That can't be changed any more.",
    "in_flight": "That's already being done and can't be recalled.",
    "not_cancellable": "That's already finished.",
    "not_failed": "That didn't fail.",
    "check_first": "It may have gone through. Check the service before trying again.",
    "too_many": "That has been tried enough times.",
}


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1, max_length=120)
    widget_id: str | None = Field(default=None, max_length=120)
    values: dict[str, Any] = Field(default_factory=dict[str, Any])


def tasks_router(accounts: AccountService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/tasks")

    def engine_of(request: Request) -> TaskEngine:
        engine = cast(TaskEngine | None, getattr(request.app.state, "tasks", None))
        if engine is None:
            raise HTTPException(status_code=404, detail="Tasks are not available here.")
        return engine

    async def me(request: Request) -> User:
        service = accounts or cast(
            AccountService | None, getattr(request.app.state, "accounts", None)
        )
        if service is None:
            raise HTTPException(status_code=404, detail="Tasks are not available here.")
        try:
            await service.require_ready()
            user = await service.current_user(request)
        except AccountsUnavailable as exc:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from exc
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        return user

    async def view(engine: TaskEngine, user: User, snapshot: Snapshot | None) -> dict[str, object]:
        if snapshot is None:
            return {"plan": None, "ui": []}
        widgets = await render.widgets_for(snapshot, await engine.messaging(user.id))
        return {
            "plan": {"id": snapshot.plan.id, "active": snapshot.active, "demo": snapshot.plan.demo},
            "ui": to_wire(widgets),
        }

    @router.get("/current")
    async def current(request: Request) -> dict[str, object]:
        user = await me(request)
        engine = engine_of(request)
        return await view(engine, user, await engine.latest(user.id))

    @router.get("/plans/{plan_id}")
    async def plan(plan_id: str, request: Request) -> dict[str, object]:
        user = await me(request)
        engine = engine_of(request)
        snapshot = await engine.snapshot(user.id, plan_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="I couldn't find that plan.")
        return await view(engine, user, snapshot)

    @router.post("/action")
    async def action(request: Request) -> dict[str, object]:
        user = await me(request)
        engine = engine_of(request)
        try:
            body = ActionBody.model_validate_json(await request.body())
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="That request was not understood.") from exc
        verb, _, target = body.action_id.partition(":")
        values = body.values
        outcome = "ok"
        extra: list[Widget] = []
        language = "en-IN"
        latest = await engine.latest(user.id)
        if latest is not None:
            language = latest.plan.language

        if verb == "task.approve":
            outcome = await engine.approve(user.id, target, values=values or None)
        elif verb == "task.edit":
            outcome = await engine.edit(user.id, target, values)
        elif verb == "task.cancel":
            outcome = await engine.cancel(user.id, target)
        elif verb == "task.retry":
            outcome = await engine.retry(user.id, target)
        elif verb.startswith("plan."):
            day = next((t for t in (latest.tasks if latest else []) if t.kind == "day.plan"), None)
            changed = day is not None and await engine.plan_action(
                user.id, day.id, verb, target, values
            )
            outcome = "ok" if changed else "not_found"
        elif verb == "email.open":
            mail = await engine.open_email(user.id, target)
            if mail is None:
                outcome = "not_found"
            else:
                sender = mail.summary.sender_name or mail.summary.sender_email
                try:
                    extra.append(
                        validate_widget(
                            {
                                "type": "card",
                                "id": "email.open",
                                "title": clean_text(mail.summary.subject, 200) or "(no subject)",
                                "body": clean_text(
                                    f"From {sender}. " + mail.body_text,
                                    1800,
                                ),
                                "actions": [
                                    {
                                        "label": "Reply",
                                        "action_id": f"email.reply:{target}",
                                        "style": "primary",
                                    }
                                ],
                            }
                        )
                    )
                except GenUIError:
                    outcome = "not_found"
        elif verb == "email.reply":
            started = await engine.reply_to_email(user.id, target, language)
            outcome = started if isinstance(started, str) else "ok"
            if not isinstance(started, str):
                latest = started
        else:
            raise HTTPException(status_code=400, detail="That action is not supported.")

        snapshot = await engine.latest(user.id) if latest is None else latest
        if outcome in ("ok", "started", "retrying", "cancelled") and snapshot is not None:
            snapshot = await engine.snapshot(user.id, snapshot.plan.id)
        payload = await view(engine, user, snapshot)
        if extra:
            payload["ui"] = [*cast(list[object], payload["ui"]), *to_wire(extra)]
        ok = outcome in ("ok", "started", "retrying", "cancelled")
        payload["result"] = "ok" if ok else outcome
        if not ok:
            payload["message"] = _MESSAGES.get(outcome, clean_text(outcome, 200))
        return payload

    return router
