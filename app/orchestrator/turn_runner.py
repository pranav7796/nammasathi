"""Turn runtime: executes state-machine effects against one commerce adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Protocol

from app.commerce.base import (
    Cart,
    CartIssue,
    CartLine,
    CommerceAdapter,
    CommerceAuthRequired,
    CommerceError,
    CommerceRejected,
    CommerceWriteDisabled,
    RejectionKind,
    same_address_id,
)
from app.commerce.order_journal import InMemoryOrderJournal, JournalError, OrderJournal
from app.commerce.units import is_pack_count_unit, parse_pack_size, requested_amount
from app.commerce.vocabulary import catalogue_term
from app.llm.extractor import (
    ClarificationContext,
    GroceryIntent,
    IntentExtractionError,
)
from app.observability import log_event
from app.orchestrator.checkout import CheckoutCoordinator, CheckoutPolicy
from app.orchestrator.confirmation import explicit_confirmation, option_choice
from app.orchestrator.item_resolution import MAX_OPTIONS, resolve_item
from app.orchestrator.state_machine import (
    CallCommerce,
    CallStarted,
    CartEntry,
    CartSnapshotReceived,
    CartWriteFailed,
    CheckoutAborted,
    CheckoutFailed,
    CheckoutSucceeded,
    Clarification,
    Effect,
    Event,
    ItemsExtracted,
    OptionSelected,
    PlaceOrder,
    PriorAttemptUnresolved,
    RequestedItem,
    Say,
    Session,
    State,
    UserCancelled,
    UserConfirmed,
    UserDone,
    UserRejected,
    UserUtterance,
    can_confirm,
    cart_hash,
    transition,
)

INTENT_TURN_TIMEOUT_SECONDS = 45.0


class IntentExtractor(Protocol):
    def extract(
        self,
        text: str,
        *,
        language: str = "kn-IN",
        context: ClarificationContext | None = None,
    ) -> GroceryIntent: ...


class ExistingCartConflict(CommerceError):
    """The account's cart belongs to another address; switching would risk losing it."""


class TextTurnRunner:
    """Execute state-machine effects against one configured commerce adapter."""

    def __init__(
        self,
        adapter: CommerceAdapter,
        address_id: str,
        session: Session,
        extractor: IntentExtractor | None = None,
        *,
        coordinator: CheckoutCoordinator | None = None,
    ) -> None:
        self.adapter = adapter
        self.address_id = address_id
        self.session = session
        self.extractor = extractor
        self.coordinator = coordinator or CheckoutCoordinator(adapter, InMemoryOrderJournal())
        self._cart_reconciliation_required = False
        self._start_cart: Cart | None = None
        self._unresolved_at_start = False
        self._synced_lines: list[tuple[str, str, int]] | None = None
        self._baseline_lines: list[CartLine] = []
        self._lat: float | None = None
        self._lng: float | None = None

    @classmethod
    async def create(
        cls,
        adapter: CommerceAdapter,
        extractor: IntentExtractor | None = None,
        *,
        address_id: str | None = None,
        journal: OrderJournal | None = None,
        policy: CheckoutPolicy | None = None,
    ) -> TextTurnRunner:
        addresses = await adapter.get_addresses()
        if not addresses:
            raise CommerceError("No delivery address available")
        if address_id is None:
            if len(addresses) != 1:
                raise CommerceError("Select a delivery address before starting")
            address = addresses[0]
        else:
            address = next(
                (item for item in addresses if same_address_id(item.id, address_id)), None
            )
            if address is None:
                raise CommerceError("Selected delivery address was not found")
        coordinator = CheckoutCoordinator(adapter, journal or InMemoryOrderJournal(), policy)
        try:
            unresolved = bool(await coordinator.recover_unresolved())
        except JournalError:
            unresolved = True  # an unreadable journal cannot prove the last attempt was settled
        # Read before any write: the account may already hold a cart we must not overwrite.
        cart = await adapter.get_cart()
        if cart.items and cart.address_id and not same_address_id(cart.address_id, address.id):
            raise ExistingCartConflict("The current cart belongs to another delivery address")
        session = replace(
            Session(),
            address_label=address.label,
            delivery_address=address.full_address,
            address_id=address.id,
        )
        runner = cls(adapter, address.id, session, extractor=extractor, coordinator=coordinator)
        runner._unresolved_at_start = unresolved
        runner._start_cart = cart if cart.items else None
        runner._synced_lines = _server_lines(cart)
        runner._baseline_lines = [
            CartLine(spin_id=item.spin_id, sku_id=item.sku_id, quantity=item.quantity)
            for item in cart.items
        ]
        return runner

    @property
    def confirmable(self) -> bool:
        return not self._cart_reconciliation_required and can_confirm(self.session)

    def _apply(self, event: Event) -> list[Effect]:
        before = self.session.state
        self.session, effects = transition(self.session, event)
        log_event(
            "state_changed",
            from_state=before.value,
            to_state=self.session.state.value,
            turn_event=type(event).__name__,
        )
        return effects

    async def dispatch(self, event: Event) -> list[Effect]:
        """Apply one event and execute all resulting effects. Provider failures become speech."""
        emitted: list[Effect] = []
        if self._cart_reconciliation_required:
            try:
                await self._reconcile_cart(emitted)
            except (CommerceError, TimeoutError) as exc:
                # Nothing may proceed against a cart whose real contents are unknown.
                emitted.append(_failure_say(exc))
                return emitted
        pending: list[Event] = [event]
        while pending:
            current = pending.pop(0)
            effects = self._apply(current)
            emitted.extend(effects)
            if isinstance(current, CallStarted):
                pending.extend(await self._after_start(emitted))
            for effect in effects:
                if isinstance(effect, Say):
                    continue
                if isinstance(effect, CallCommerce):
                    follow_up = await self._run_commerce(effect, emitted)
                    if follow_up is not None:
                        pending.append(follow_up)
                else:
                    pending.append(await self._place_order(effect))
        return emitted

    async def _after_start(self, emitted: list[Effect]) -> list[Event]:
        if self._unresolved_at_start:
            self._unresolved_at_start = False
            return [PriorAttemptUnresolved()]
        if self._start_cart is None:
            return []
        cart, self._start_cart = self._start_cart, None
        emitted.append(Say("existing_cart"))
        try:
            return [await self._snapshot(cart)]
        except (CommerceError, TimeoutError) as exc:
            emitted.append(_failure_say(exc))
            return []

    async def _run_commerce(self, effect: CallCommerce, emitted: list[Effect]) -> Event | None:
        try:
            return await self._execute_commerce(effect)
        except (CommerceError, TimeoutError) as exc:
            emitted.append(_failure_say(exc))
            log_event("commerce_failed", op=effect.op, kind=type(exc).__name__)
            if effect.op in {"update_cart", "restore_cart"}:
                emitted.extend(self._apply(CartWriteFailed()))
                try:
                    await self._reconcile_cart(emitted)
                except (CommerceError, TimeoutError):
                    self._cart_reconciliation_required = True
            return None

    async def dispatch_text(
        self, text: str, *, heard: tuple[int, str] | None = None
    ) -> list[Effect]:
        """Interpret one user turn. Only typed events reach the state machine.

        `heard` is the (cart_version, cart_hash) of the readback the client actually
        received. A spoken yes confirms that readback, not whatever the cart is now.
        """
        state = self.session.state
        if state is State.READBACK:
            confirmation = explicit_confirmation(text)
            if confirmation is True:
                version, digest = heard or (
                    self.session.readback_version
                    if self.session.readback_version is not None
                    else -1,
                    self.session.readback_hash,
                )
                return await self.dispatch(UserConfirmed(version, digest))
            if confirmation is False:
                return await self.dispatch(UserRejected())
        if state is State.CLARIFYING:
            pending = self.session.pending
            if pending is not None and pending.options:
                choice = option_choice(text, len(pending.options))
                if choice is not None:
                    return await self.dispatch(OptionSelected(choice))
            if explicit_confirmation(text) is False:
                return await self.dispatch(UserRejected())
        if state in (State.ORDER_UNKNOWN, State.PLACING, State.VERIFYING, State.FAILED):
            return await self.dispatch(UserUtterance(text, "unclear"))
        if self.extractor is None:
            raise IntentExtractionError("No intent extractor configured")
        try:
            intent = await self._extract(text)
        except (IntentExtractionError, TimeoutError):
            return [Say("intent_error")]
        return await self.dispatch(self._event_from_intent(text, intent))

    async def _extract(self, text: str) -> GroceryIntent:
        assert self.extractor is not None
        extractor = self.extractor
        language = self.session.language
        pending = self.session.pending
        if pending is not None and self.session.state is State.CLARIFYING:
            context = ClarificationContext(
                item=pending.requested.name if pending.requested else "",
                options=tuple(f"{line.name} {line.pack_size}" for line in pending.options),
            )
            call = asyncio.to_thread(extractor.extract, text, language=language, context=context)
        else:
            call = asyncio.to_thread(extractor.extract, text, language=language)
        return await asyncio.wait_for(call, INTENT_TURN_TIMEOUT_SECONDS)

    def _event_from_intent(self, text: str, intent: GroceryIntent) -> Event:
        state = self.session.state
        if intent.intent == "select_option" and state is State.CLARIFYING:
            number = intent.option_number
            return (
                OptionSelected(number - 1) if number is not None else UserUtterance(text, "unclear")
            )
        if intent.needs_clarification:
            if state is State.COLLECTING:
                # The model's own wording is not spoken: replies come only from app templates.
                return ItemsExtracted(
                    resolved=(),
                    clarification=Clarification(
                        "need_details", intent.clarification_question or ""
                    ),
                )
            return UserUtterance(text, "unclear")
        requested = tuple(
            RequestedItem(item.name, item.quantity, item.unit, item.search_term)
            for item in intent.items
        )
        if intent.intent == "add_items":
            return UserUtterance(text, "add", requested)
        if intent.intent == "remove_items":
            return UserUtterance(text, "remove", requested)
        if intent.intent == "change_quantity":
            return UserUtterance(text, "change_qty", requested)
        if intent.intent == "reorder_usual":
            return UserUtterance(text, "reorder_usual")
        if intent.intent == "done":
            return UserDone()
        if intent.intent == "cancel":
            return UserCancelled()
        if intent.intent == "reject" and state is State.CLARIFYING:
            return UserRejected()
        # An LLM "confirm" is never a yes; only the deterministic matcher can confirm.
        return UserUtterance(text, "unclear")

    async def _execute_commerce(self, effect: CallCommerce) -> Event | None:
        if effect.op == "search":
            return await self._lookup(effect)
        if effect.op == "go_to_items":
            return await self._usual_items()
        if effect.op == "update_cart":
            return await self._write_cart()
        if effect.op == "restore_cart":
            cart = await self.adapter.update_cart(self.address_id, self._baseline_lines)
            return await self._snapshot(cart)
        if effect.op == "get_cart":
            return await self._snapshot(await self.adapter.get_cart())
        if effect.op == "get_orders":
            return await self.coordinator.reconcile(self.session)
        return await self.coordinator.verify(self.session, lat=self._lat, lng=self._lng)

    async def _lookup(self, effect: CallCommerce) -> ItemsExtracted:
        resolved: list[CartEntry] = []
        automatic: list[CartEntry] = []
        for index, raw in enumerate(effect.items):
            requested = replace(raw, search_term=raw.search_term or catalogue_term(raw.name))
            if effect.edit == "remove":
                entry, clarification = self._resolve_removal(requested)
            elif effect.edit == "change_qty":
                entry, clarification = await self._resolve_quantity_change(requested)
            else:
                outcome = resolve_item(
                    requested,
                    await self.adapter.search_products(
                        self.address_id, requested.search_term or ""
                    ),
                )
                entry, clarification = outcome.entry, outcome.clarification
                if entry is not None and outcome.auto_resolved:
                    automatic.append(entry)
            if clarification is not None:
                return ItemsExtracted(
                    tuple(resolved),
                    tuple(automatic),
                    clarification=replace(clarification, operation=effect.edit),
                    operation=effect.edit,
                    remaining=effect.items[index + 1 :],
                )
            if entry is not None:
                resolved.append(entry)
        return ItemsExtracted(tuple(resolved), tuple(automatic), operation=effect.edit)

    async def _usual_items(self) -> ItemsExtracted:
        products = await self.adapter.go_to_items(self.address_id)
        options = tuple(
            CartEntry(
                spin_id=variant.spin_id,
                sku_id=variant.sku_id,
                name=product.display_name,
                pack_size=variant.pack_size,
                quantity=1,
                unit_price_inr=variant.price_inr,
            )
            for product in products
            for variant in product.variants[:1]
            if variant.in_stock
        )[:MAX_OPTIONS]
        if not options:
            return ItemsExtracted(resolved=())
        return ItemsExtracted(
            resolved=(),
            clarification=Clarification(
                "usual_items",
                "Which of your usual items should I add?",
                options,
                requested=RequestedItem("", 1),
            ),
        )

    async def _write_cart(self) -> CartSnapshotReceived:
        """Read, compare, then write: the cart is shared with the user's Swiggy app."""
        if self._synced_lines is not None:
            current = await self.adapter.get_cart()
            if _server_lines(current) != self._synced_lines:
                # Someone changed the cart elsewhere. Do not overwrite it with a stale view.
                return replace(await self._snapshot(current), notice="cart_changed")
        intended = [
            CartLine(spin_id=line.spin_id, sku_id=line.sku_id, quantity=line.quantity)
            for line in self.session.cart
        ]
        cart = await self.adapter.update_cart(self.address_id, intended)
        snapshot = await self._snapshot(cart)
        expected = sorted((line.spin_id, line.sku_id, line.quantity) for line in intended)
        if _server_lines(cart) != expected:
            # Swiggy capped a quantity or removed an item; the user must hear the real cart.
            log_event("cart_differs_from_request", op="update_cart")
            return replace(snapshot, notice="cart_changed")
        return snapshot

    async def _snapshot(self, cart: Cart) -> CartSnapshotReceived:
        """Cart plus live payment facts. Any doubt becomes an issue that blocks confirmation."""
        self._synced_lines = _server_lines(cart)
        issues = [issue.value for issue in cart.issues]
        if cart.items:
            self._lat, self._lng = cart.lat, cart.lng
            if not same_address_id(cart.address_id, self.address_id):
                issues.append(CartIssue.ADDRESS_MISMATCH.value)
            try:
                options = await self.adapter.get_payment_options()
            except (CommerceError, TimeoutError):
                issues.append(CartIssue.PAYMENT_UNAVAILABLE.value)
            else:
                if not options.cod_available:
                    issues.append(CartIssue.PAYMENT_UNAVAILABLE.value)
        return CartSnapshotReceived(
            cart=tuple(
                CartEntry(
                    spin_id=item.spin_id,
                    sku_id=item.sku_id,
                    name=item.name,
                    pack_size=item.pack_size,
                    quantity=item.quantity,
                    unit_price_inr=item.unit_price_inr,
                )
                for item in cart.items
            ),
            total_inr=cart.grand_total_inr,
            address_id=cart.address_id,
            address_label=cart.delivery_label or "",
            delivery_address=cart.delivery_address or "",
            issues=tuple(issues),
            fees_inr=cart.delivery_fee_inr,
            store_count=cart.store_count,
        )

    async def _reconcile_cart(self, emitted: list[Effect]) -> None:
        """Read the authoritative cart before continuing after an uncertain write."""
        cart = await self.adapter.get_cart()
        self._cart_reconciliation_required = False
        emitted.extend(self._apply(await self._snapshot(cart)))

    def _cart_matches(self, requested: RequestedItem) -> list[CartEntry]:
        term = requested.search_term or requested.name
        return [entry for entry in self.session.cart if _name_matches(entry.name, term)]

    def _which_line(self, requested: RequestedItem) -> Clarification:
        matches = self._cart_matches(requested) or list(self.session.cart)
        return Clarification(
            "which_line",
            f"Which item do you mean by {requested.name}?",
            tuple(matches[:MAX_OPTIONS]),
            requested=requested,
        )

    def _resolve_removal(
        self, requested: RequestedItem
    ) -> tuple[CartEntry | None, Clarification | None]:
        matches = self._cart_matches(requested)
        if len(matches) == 1:
            return matches[0], None
        return None, self._which_line(requested)

    async def _resolve_quantity_change(
        self, requested: RequestedItem
    ) -> tuple[CartEntry | None, Clarification | None]:
        matches = self._cart_matches(requested)
        if len(matches) != 1:
            return None, self._which_line(requested)
        line = matches[0]
        amount = Decimal(str(requested.qty))
        if requested.unit is None or is_pack_count_unit(requested.unit):
            if amount != amount.to_integral_value() or amount < 1:
                return None, self._which_line(requested)
            return replace(line, quantity=int(amount)), None
        target = requested_amount(requested.qty, requested.unit)
        pack = parse_pack_size(line.pack_size)
        if (
            target is not None
            and pack is not None
            and target.measure == pack.measure
            and target.value % pack.value == 0
        ):
            return replace(line, quantity=int(target.value / pack.value)), None
        # The current pack cannot make this amount: resolve it again, keeping the same product.
        outcome = resolve_item(
            requested,
            await self.adapter.search_products(self.address_id, requested.search_term or line.name),
        )
        if outcome.entry is not None and outcome.entry.name == line.name:
            return outcome.entry, None
        if outcome.clarification is not None:
            same_product = tuple(
                option for option in outcome.clarification.options if option.name == line.name
            )
            if same_product:
                return None, replace(outcome.clarification, options=same_product)
        return None, self._which_line(requested)

    async def _place_order(
        self, effect: PlaceOrder
    ) -> CheckoutSucceeded | CheckoutFailed | CheckoutAborted | CartSnapshotReceived:
        if effect.cart_hash != self.session.cart_hash:
            return CheckoutAborted("preflight")
        try:
            snapshot = await self._snapshot(await self.adapter.get_cart())
        except (CommerceAuthRequired, CommerceError, TimeoutError):
            return CheckoutAborted("preflight")
        live_hash = cart_hash(
            snapshot.cart, snapshot.total_inr, snapshot.address_id, snapshot.payment_method
        )
        if (
            live_hash != effect.cart_hash
            or snapshot.issues
            or not same_address_id(snapshot.address_id, self.address_id)
            or snapshot.address_label not in ("", self.session.address_label)
            or snapshot.delivery_address not in ("", self.session.delivery_address)
        ):
            return snapshot
        return await self.coordinator.place(self.session, self.address_id)


def _server_lines(cart: Cart) -> list[tuple[str, str, int]]:
    return sorted((item.spin_id, item.sku_id, item.quantity) for item in cart.items)


def _failure_say(error: BaseException) -> Say:
    if isinstance(error, CommerceAuthRequired):
        return Say("swiggy_login_expired")
    if isinstance(error, CommerceWriteDisabled):
        return Say("cart_writes_disabled")
    if isinstance(error, CommerceRejected):
        if error.kind is RejectionKind.NOT_SERVICEABLE:
            return Say("not_serviceable")
        if error.kind is RejectionKind.OUT_OF_STOCK:
            return Say("item_unavailable")
    return Say("commerce_error")


def _name_matches(product_name: str, requested_name: str) -> bool:
    requested_words = requested_name.casefold().split()
    normalized_product = product_name.casefold()
    return bool(requested_words) and all(word in normalized_product for word in requested_words)
