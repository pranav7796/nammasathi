"""Groceries and food share one order engine; only the provider differs."""

from __future__ import annotations

from app.assistant.view import WorkflowView
from app.commerce.base import CommerceAdapter
from app.commerce.order_journal import OrderJournal
from app.llm.extractor import ClarificationContext, GroceryIntent
from app.orchestrator.checkout import CheckoutPolicy
from app.orchestrator.state_machine import (
    CallStarted,
    Effect,
    OptionSelected,
    Say,
    State,
    UserCancelled,
    UserConfirmed,
    UserDone,
)
from app.orchestrator.turn_runner import IntentExtractor, TextTurnRunner


class OrderWorkflow:
    """A delivered order: search, cart, readback, explicit yes, checkout, verification."""

    def __init__(
        self,
        domain: str,
        adapter: CommerceAdapter,
        *,
        address_id: str,
        language: str,
        extractor: IntentExtractor | None,
        journal: OrderJournal,
        policy: CheckoutPolicy,
    ) -> None:
        self.domain = domain
        self.adapter = adapter
        self.address_id = address_id
        self.language = language
        self.extractor = extractor
        self.journal = journal
        self.policy = policy
        self.runner: TextTurnRunner | None = None

    @property
    def _runner(self) -> TextTurnRunner:
        if self.runner is None:
            raise RuntimeError("Workflow has not been opened")
        return self.runner

    @property
    def finished(self) -> bool:
        return self._runner.session.state is State.DONE

    @property
    def outcome_unknown(self) -> bool:
        return self._runner.session.state is State.ORDER_UNKNOWN

    @property
    def has_open_task(self) -> bool:
        session = self._runner.session
        return session.state is not State.DONE and bool(session.cart or session.pending)

    async def open(self) -> list[Effect]:
        """Reads the provider's cart first, so anything already in it is kept and spoken."""
        self.runner = await TextTurnRunner.create(
            self.adapter,
            self.extractor,
            address_id=self.address_id,
            journal=self.journal,
            policy=self.policy,
            domain=self.domain,
        )
        effects = await self.runner.dispatch(CallStarted(user_known=True, language=self.language))
        return [e for e in effects if not (isinstance(e, Say) and e.template == "greeting")]

    async def direct(self, text: str, heard: tuple[int, str] | None = None) -> list[Effect] | None:
        return await self._runner.dispatch_direct(text, heard=heard)

    async def apply(self, text: str, intent: GroceryIntent) -> list[Effect]:
        return await self._runner.dispatch_intent(text, intent)

    async def confirm(self, version: int, digest: str) -> list[Effect]:
        return await self._runner.dispatch(UserConfirmed(version, digest))

    async def choose(self, index: int) -> list[Effect]:
        return await self._runner.dispatch(OptionSelected(index))

    async def cancel(self) -> list[Effect]:
        return await self._runner.dispatch(UserCancelled())

    async def resume(self) -> list[Effect]:
        return await self._runner.dispatch(UserDone())

    def context(self) -> ClarificationContext | None:
        return self._runner.clarification_context()

    def view(self) -> WorkflowView:
        runner = self._runner
        session = runner.session
        return WorkflowView(
            domain=self.domain,
            state=session.state.value,
            lines=session.cart,
            cart_version=session.cart_version,
            cart_hash=session.cart_hash,
            readback_version=session.readback_version,
            readback_hash=session.readback_hash,
            confirmable=runner.confirmable,
            total_inr=session.total_inr,
            fees_inr=session.fees_inr,
            savings_inr=session.savings_inr,
            store_count=session.store_count,
            issues=session.issues,
            payment_method=session.payment_method,
            address_label=session.address_label,
            delivery_address=session.delivery_address,
            options=session.pending.options if session.pending else (),
            order_ids=session.order_ids,
            order_status=session.order_status,
            order_verified=session.order_verified,
            order_partial=session.order_partial,
        )
