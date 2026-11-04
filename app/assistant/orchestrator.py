"""One assistant over several capabilities: routes each turn and keeps tasks separate.

It holds no commerce logic. Replies go to the active task first, without the LLM, so
approval stays deterministic. The LLM's interpretation only chooses where a new request goes.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date as Date
from typing import Protocol

from app.assistant.router import DOMAINS, Domain, route
from app.assistant.view import WorkflowView
from app.commerce.base import CommerceError
from app.commerce.order_journal import JournalError, OrderJournal, unresolved_attempts
from app.llm.extractor import ClarificationContext, GroceryIntent, IntentExtractionError
from app.observability import log_event
from app.orchestrator.state_machine import Effect, Say
from app.orchestrator.turn_runner import (
    INTENT_TURN_TIMEOUT_SECONDS,
    ExistingCartConflict,
    IntentExtractor,
)


class Workflow(Protocol):
    domain: str

    @property
    def finished(self) -> bool: ...

    @property
    def outcome_unknown(self) -> bool: ...

    @property
    def has_open_task(self) -> bool: ...

    async def open(self) -> list[Effect]: ...

    async def direct(
        self, text: str, heard: tuple[int, str] | None = None
    ) -> list[Effect] | None: ...

    async def apply(self, text: str, intent: GroceryIntent) -> list[Effect]: ...

    async def confirm(self, version: int, digest: str) -> list[Effect]: ...

    async def choose(self, index: int) -> list[Effect]: ...

    async def cancel(self) -> list[Effect]: ...

    def context(self) -> ClarificationContext | None: ...

    def view(self) -> WorkflowView: ...


WorkflowFactory = Callable[[Domain], Awaitable[Workflow]]


@dataclass(frozen=True)
class Reply:
    effects: list[Effect]
    domain: str = ""


class Assistant:
    def __init__(
        self,
        *,
        language: str,
        extractor: IntentExtractor | None,
        factory: WorkflowFactory,
        journal: OrderJournal,
        address_label: str = "",
        delivery_address: str = "",
        today: Date | None = None,
    ) -> None:
        self.language = language
        self.extractor = extractor
        self.factory = factory
        self.journal = journal
        self.address_label = address_label
        self.delivery_address = delivery_address
        self.today = today
        self.workflows: dict[Domain, Workflow] = {}
        self.active: Domain | None = None
        self._blocked_at_start = False

    @property
    def blocked(self) -> bool:
        """An unresolved real-world attempt stops everything until a person settles it."""
        return self._blocked_at_start or any(
            workflow.outcome_unknown for workflow in self.workflows.values()
        )

    async def start(self) -> Reply:
        try:
            unresolved = unresolved_attempts(self.journal)
        except JournalError:
            self._blocked_at_start = True
            return Reply([Say("order_unknown")])
        for domain in {attempt.domain for attempt in unresolved}:
            if domain in DOMAINS and domain != "dining":
                # Opening a capability checks its order history and settles what it can.
                with contextlib.suppress(CommerceError):
                    await self.workflow(domain)  # type: ignore[arg-type]
        try:
            still_open = bool(unresolved_attempts(self.journal))
        except JournalError:
            still_open = True
        if still_open:
            self._blocked_at_start = True
            return Reply([Say("order_unknown")])
        return Reply([Say("greeting")])

    async def workflow(self, domain: Domain) -> tuple[Workflow, list[Effect]]:
        """The capability's workflow, plus anything it must say when first opened."""
        existing = self.workflows.get(domain)
        if existing is not None:
            return existing, []
        created = await self.factory(domain)
        opening = await created.open()
        self.workflows[domain] = created
        return created, opening

    async def turn(self, text: str, heard: tuple[int, str] | None = None) -> Reply:
        if self.blocked:
            return Reply([Say("order_unknown")], self.active or "")
        current = self.workflows.get(self.active) if self.active else None
        if current is not None:
            direct = await current.direct(text, heard)
            if direct is not None:
                return Reply(direct, self.active or "")
        try:
            intent = await self._understand(text, current)
        except (IntentExtractionError, TimeoutError):
            return Reply([Say("intent_error")], self.active or "")
        domain = route(
            suggested=intent.domain,
            intent=intent.intent,
            text=text,
            active=self.active,
            has_items=bool(intent.items),
            needs_clarification=intent.needs_clarification,
        )
        log_event("route", to_state=domain or "none", from_state=self.active or "none")
        if domain is None:
            return Reply([Say("ask_domain")])
        return await self._handle(domain, text, intent, current)

    async def _handle(
        self, domain: Domain, text: str, intent: GroceryIntent, current: Workflow | None
    ) -> Reply:
        spoken: list[Effect] = []
        if domain != self.active and current is not None and current.has_open_task:
            spoken.append(Say("switched_domain", data={"from": current.domain}))
        previous = self.workflows.get(domain)
        if previous is not None and previous.finished:
            # A new request after a finished order starts a new task. Limits still hold.
            del self.workflows[domain]
        try:
            workflow, opening = await self.workflow(domain)
        except ExistingCartConflict:
            return Reply([*spoken, Say("cart_other_address")], domain)
        except CommerceError:
            return Reply([*spoken, Say("commerce_error")], domain)
        self.active = domain
        effects = await workflow.apply(text, intent)
        return Reply([*spoken, *opening, *effects], domain)

    async def _understand(self, text: str, current: Workflow | None) -> GroceryIntent:
        if self.extractor is None:
            raise IntentExtractionError("No intent extractor configured")
        context = (current.context() if current else None) or ClarificationContext()
        context = replace(
            context,
            active_domain=self.active or "",
            today=self.today.isoformat() if self.today else "",
        )
        call = asyncio.to_thread(
            self.extractor.extract, text, language=self.language, context=context
        )
        return await asyncio.wait_for(call, INTENT_TURN_TIMEOUT_SECONDS)

    def _current(self) -> Workflow | None:
        return self.workflows.get(self.active) if self.active else None

    async def confirm(self, version: int, digest: str) -> Reply:
        current = self._current()
        if current is None or self.blocked:
            return Reply([Say("order_unknown" if self.blocked else "ask_domain")])
        return Reply(await current.confirm(version, digest), current.domain)

    async def choose(self, index: int) -> Reply:
        current = self._current()
        if current is None or self.blocked:
            return Reply([Say("order_unknown" if self.blocked else "ask_domain")])
        return Reply(await current.choose(index), current.domain)

    async def cancel(self) -> Reply:
        current = self._current()
        if current is None or self.blocked:
            return Reply([Say("order_unknown" if self.blocked else "ask_domain")])
        return Reply(await current.cancel(), current.domain)

    def view(self) -> WorkflowView:
        current = self._current()
        if current is not None:
            view = current.view()
            if self.blocked and not view.order_ids:
                return replace(view, state="ORDER_UNKNOWN", confirmable=False)
            return view
        return WorkflowView(
            state="ORDER_UNKNOWN" if self.blocked else "COLLECTING",
            address_label=self.address_label,
            delivery_address=self.delivery_address,
        )
