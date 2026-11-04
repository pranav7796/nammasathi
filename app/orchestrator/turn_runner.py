"""Text-turn runtime for the local mock-backed order journey."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.commerce.base import (
    Cart,
    CartLine,
    CommerceAdapter,
    CommerceError,
    OrderSummary,
    PaymentMethod,
    Product,
    SearchResult,
    Variant,
)
from app.llm.extractor import GeminiIntentExtractor, IntentExtractionError
from app.orchestrator.state_machine import (
    CallCommerce,
    CartEntry,
    CartSnapshotReceived,
    CheckoutFailed,
    CheckoutSucceeded,
    Clarification,
    Effect,
    Event,
    ItemsExtracted,
    PlaceOrder,
    ReconcileResult,
    RequestedItem,
    Say,
    Session,
    UserCancelled,
    UserConfirmed,
    UserDone,
    UserRejected,
    UserUtterance,
    cart_hash,
    transition,
)


class TextTurnRunner:
    """Execute state-machine effects against one configured commerce adapter."""

    def __init__(
        self,
        adapter: CommerceAdapter,
        address_id: str,
        session: Session,
        extractor: GeminiIntentExtractor | None = None,
    ) -> None:
        self.adapter = adapter
        self.address_id = address_id
        self.session = session
        self.extractor = extractor
        self.last_search: SearchResult | None = None
        self.last_go_to_items: list[Product] = []
        self._checkout_started_at: datetime | None = None
        self._cart_reconciliation_required = False

    @classmethod
    async def create(
        cls,
        adapter: CommerceAdapter,
        extractor: GeminiIntentExtractor | None = None,
        *,
        address_id: str | None = None,
    ) -> TextTurnRunner:
        addresses = await adapter.get_addresses()
        if not addresses:
            raise CommerceError("No delivery address available")
        if address_id is None:
            if len(addresses) != 1:
                raise CommerceError("Select a delivery address before starting")
            address = addresses[0]
        else:
            address = next((item for item in addresses if item.id == address_id), None)
            if address is None:
                raise CommerceError("Selected delivery address was not found")
        session = replace(
            Session(),
            address_label=address.label,
            delivery_address=address.full_address,
            address_id=address.id,
        )
        return cls(adapter, address.id, session, extractor=extractor)

    async def dispatch(self, event: Event) -> list[Effect]:
        """Apply one event and execute all resulting effects."""

        emitted: list[Effect] = []
        if self._cart_reconciliation_required:
            await self._reconcile_cart(emitted)
        pending: list[Event] = [event]
        while pending:
            current = pending.pop(0)
            self.session, effects = transition(self.session, current)
            emitted.extend(effects)
            for effect in effects:
                if isinstance(effect, Say):
                    continue
                if isinstance(effect, CallCommerce):
                    try:
                        follow_up = await self._execute_commerce(effect)
                    except Exception:
                        if effect.op == "update_cart":
                            try:
                                await self._reconcile_cart(emitted)
                            except Exception:
                                self._cart_reconciliation_required = True
                        raise
                    if follow_up is not None:
                        pending.append(follow_up)
                elif isinstance(effect, PlaceOrder):
                    pending.append(await self._place_order(effect))
        return emitted

    async def dispatch_text(self, text: str) -> list[Effect]:
        """Extract text, then pass only typed events into the state machine."""
        confirmation = (
            _explicit_confirmation(text) if self.session.state.value == "READBACK" else None
        )
        if confirmation is True:
            return await self.dispatch(
                UserConfirmed(self.session.cart_version, self.session.cart_hash)
            )
        if confirmation is False:
            return await self.dispatch(UserRejected())
        if self.extractor is None:
            raise IntentExtractionError("No intent extractor configured")
        try:
            intent = self.extractor.extract(text, language=self.session.language)
        except IntentExtractionError:
            return [Say("intent_error")]
        if intent.needs_clarification:
            question = intent.clarification_question or "Could you clarify that?"
            return await self.dispatch(
                ItemsExtracted(resolved=(), clarification=Clarification("ambiguous_item", question))
            )
        if intent.intent == "add_items":
            return await self.dispatch(
                UserUtterance(
                    text,
                    "add",
                    tuple(
                        RequestedItem(item.name, item.quantity, item.unit) for item in intent.items
                    ),
                )
            )
        if intent.intent == "remove_items":
            return await self.dispatch(
                UserUtterance(
                    text,
                    "remove",
                    tuple(
                        RequestedItem(item.name, item.quantity, item.unit) for item in intent.items
                    ),
                )
            )
        if intent.intent == "change_quantity":
            return await self.dispatch(
                UserUtterance(
                    text,
                    "change_qty",
                    tuple(
                        RequestedItem(item.name, item.quantity, item.unit) for item in intent.items
                    ),
                )
            )
        if intent.intent == "reorder_usual":
            return await self.dispatch(UserUtterance(text, "reorder_usual"))
        if intent.intent == "done":
            return await self.dispatch(UserDone())
        if intent.intent in {"confirm", "reject"}:
            return await self.dispatch(UserUtterance(text, "unclear"))
        if intent.intent == "cancel":
            return await self.dispatch(UserCancelled())
        return await self.dispatch(UserUtterance(text, "unclear"))

    async def _execute_commerce(
        self, effect: CallCommerce
    ) -> ItemsExtracted | CartSnapshotReceived | ReconcileResult | None:
        if effect.op == "search":
            if not effect.query:
                raise CommerceError("Search query is missing")
            if not effect.items:
                self.last_search = await self.adapter.search_products(self.address_id, effect.query)
                return None
            if effect.edit == "remove":
                return self._resolve_removals(effect.items)
            resolved: list[CartEntry] = []
            auto_resolved: list[CartEntry] = []
            for requested in effect.items:
                result = await self.adapter.search_products(self.address_id, requested.name)
                self.last_search = result
                entries, automatic, clarification = self._resolve(requested, result)
                if clarification is not None:
                    return ItemsExtracted(resolved=(), clarification=clarification)
                if effect.edit == "change_qty" and not self._has_single_cart_match(requested.name):
                    return ItemsExtracted(
                        resolved=(),
                        clarification=Clarification(
                            "ambiguous_item",
                            f"{requested.name} is not one clear item in the current cart.",
                        ),
                    )
                resolved.extend(entries)
                if effect.edit == "add":
                    auto_resolved.extend(automatic)
            return ItemsExtracted(tuple(resolved), tuple(auto_resolved), operation=effect.edit)
        elif effect.op == "go_to_items":
            self.last_go_to_items = await self.adapter.go_to_items(self.address_id)
        elif effect.op == "update_cart":
            intended_lines = [
                CartLine(spin_id=line.spin_id, sku_id=line.sku_id, quantity=line.quantity)
                for line in self.session.cart
            ]
            cart = await self.adapter.update_cart(self.address_id, intended_lines)
            expected = sorted((line.spin_id, line.sku_id, line.quantity) for line in intended_lines)
            actual = sorted((item.spin_id, item.sku_id, item.quantity) for item in cart.items)
            if cart.address_id != self.address_id or actual != expected:
                raise CommerceError("Cart update response did not match the requested cart")
            return _cart_snapshot(cart)
        elif effect.op == "get_cart":
            return _cart_snapshot(await self.adapter.get_cart())
        elif effect.op == "get_orders":
            orders = await self.adapter.get_orders(count=20)
            matching = [order for order in orders if self._matches_pending_order(order)]
            return ReconcileResult(
                order_found=bool(matching),
                order_ids=tuple(order.order_id for order in matching),
                cart_hash=self.session.cart_hash,
            )
        else:
            raise CommerceError(f"Unsupported commerce operation: {effect.op}")
        return None

    async def _reconcile_cart(self, emitted: list[Effect]) -> None:
        """Read the authoritative cart before continuing after an uncertain write."""
        cart = await self.adapter.get_cart()
        self.session, effects = transition(self.session, _cart_snapshot(cart))
        emitted.extend(effects)
        self._cart_reconciliation_required = False

    def _resolve(
        self, requested: RequestedItem, result: SearchResult
    ) -> tuple[list[CartEntry], list[CartEntry], Clarification | None]:
        if not result.products:
            return (
                [],
                [],
                Clarification(
                    "ambiguous_item",
                    f"I could not find {requested.name}. What should I add instead?",
                ),
            )
        products = result.products
        if requested.unit is not None:
            compatible = [
                product
                for product in products
                if any(_same_measure(variant.unit, requested.unit) for variant in product.variants)
            ]
            if compatible:
                products = compatible
        if len(products) > 1:
            options = tuple(
                self._entry(product, variant, 1)
                for product in products[:2]
                for variant in product.variants[:1]
                if variant.in_stock
            )
            return (
                [],
                [],
                Clarification(
                    "ambiguous_item",
                    f"Which product matching {requested.name} should I use?",
                    options,
                ),
            )
        product = products[0]
        variants = [variant for variant in product.variants if variant.in_stock]
        if not variants:
            return [], [], Clarification("out_of_stock", f"{product.display_name} is out of stock.")
        if requested.unit is None:
            options = tuple(self._entry(product, variant, 1) for variant in variants[:2])
            return (
                [],
                [],
                Clarification(
                    "pack_size", f"Which pack size of {product.display_name} should I add?", options
                ),
            )
        target = _amount(requested.qty, requested.unit)
        exact = [variant for variant in variants if _variant_amount(variant) == target]
        if exact:
            return [self._entry(product, exact[0], 1)], [], None
        smaller = [variant for variant in variants if 0 < _variant_amount(variant) < target]
        if smaller:
            variant = max(smaller, key=_variant_amount)
            if target % _variant_amount(variant) == 0:
                entry = self._entry(product, variant, int(target / _variant_amount(variant)))
                return [entry], [entry], None
        options = tuple(self._entry(product, variant, 1) for variant in variants[:2])
        return (
            [],
            [],
            Clarification(
                "pack_size", f"Which pack size of {product.display_name} should I use?", options
            ),
        )

    def _resolve_removals(self, requested_items: tuple[RequestedItem, ...]) -> ItemsExtracted:
        resolved: list[CartEntry] = []
        for requested in requested_items:
            matches = [
                entry for entry in self.session.cart if _name_matches(entry.name, requested.name)
            ]
            if len(matches) == 0:
                return ItemsExtracted(
                    resolved=(),
                    clarification=Clarification(
                        "ambiguous_item",
                        f"I could not find {requested.name} in the current cart.",
                    ),
                )
            if len(matches) > 1:
                return ItemsExtracted(
                    resolved=(),
                    clarification=Clarification(
                        "ambiguous_item",
                        f"Which {requested.name} should I remove?",
                        tuple(matches[:2]),
                    ),
                )
            resolved.append(matches[0])
        return ItemsExtracted(tuple(resolved), operation="remove")

    def _has_single_cart_match(self, name: str) -> bool:
        return sum(_name_matches(entry.name, name) for entry in self.session.cart) == 1

    @staticmethod
    def _entry(product: Product, variant: Variant, quantity: int) -> CartEntry:
        return CartEntry(
            spin_id=variant.spin_id,
            sku_id=variant.sku_id,
            name=product.display_name,
            pack_size=variant.pack_size,
            quantity=quantity,
            unit_price_inr=variant.price_inr,
        )

    async def _place_order(
        self, effect: PlaceOrder
    ) -> CheckoutSucceeded | CheckoutFailed | CartSnapshotReceived:
        if effect.cart_hash != self.session.cart_hash:
            return CheckoutFailed("cart hash changed before checkout", terminal=True)
        try:
            live_cart = await self.adapter.get_cart()
            snapshot = _cart_snapshot(live_cart)
            if (
                cart_hash(snapshot.cart, snapshot.total_inr, snapshot.address_id)
                != effect.cart_hash
            ):
                return snapshot
            if snapshot.address_id != self.address_id:
                return snapshot
            if (
                snapshot.address_label != self.session.address_label
                or snapshot.delivery_address != self.session.delivery_address
            ):
                return snapshot
            self._checkout_started_at = datetime.now(UTC)
            result = await self.adapter.checkout(
                self.address_id,
                payment_method=PaymentMethod.COD,
            )
        except TimeoutError as exc:
            return CheckoutFailed(str(exc))
        except CommerceError as exc:
            return CheckoutFailed(str(exc), terminal=True)

        if result.success:
            return CheckoutSucceeded(tuple(result.order_ids))
        return CheckoutFailed(result.message or "Mock checkout failed", terminal=True)

    def _matches_pending_order(self, order: OrderSummary) -> bool:
        if self._checkout_started_at is None or order.created_at is None:
            return False
        created_at = order.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return (
            created_at >= self._checkout_started_at
            and order.delivery_address_id == self.address_id
            and order.total_inr == self.session.total_inr
            and sorted((item.name, item.quantity) for item in order.items)
            == sorted((item.name, item.quantity) for item in self.session.cart)
        )


def _cart_snapshot(cart: Cart) -> CartSnapshotReceived:
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
    )


def _explicit_confirmation(text: str) -> bool | None:
    normalized = " ".join(
        "".join(
            character for character in text.casefold() if character.isalnum() or character.isspace()
        ).split()
    )
    affirmative = {
        "yes",
        "yes please",
        "yes confirm",
        "yes place order",
        "yeah",
        "yep",
        "confirm",
        "confirm order",
        "proceed",
        "place order",
        "haan",
        "han",
        "haan ji",
        "जी हाँ",
        "जी हां",
        "हाँ",
        "हां",
        "कर दीजिए",
        "ಹೌದು",
        "ಹೌದು ಮಾಡಿ",
        "ಹೌದು ಆರ್ಡರ್ ಮಾಡಿ",
        "ಆರ್ಡರ್ ಮಾಡಿ",
    }
    negative = {
        "no",
        "no thanks",
        "cancel",
        "stop",
        "nahi",
        "nahin",
        "नहीं",
        "नही",
        "मत करो",
        "ಬೇಡ",
        "ಬೇಡ ಧನ್ಯವಾದ",
        "ರದ್ದು ಮಾಡಿ",
    }
    if normalized in affirmative:
        return True
    if normalized in negative:
        return False
    return None


def _unit_factor(unit: str) -> tuple[str, float] | None:
    normalized = unit.casefold().strip()
    if normalized in {"kg", "kilogram", "kilograms", "kilo"}:
        return "g", 1000
    if normalized in {"g", "gram", "grams"}:
        return "g", 1
    if normalized in {"l", "litre", "liter", "litres", "liters"}:
        return "ml", 1000
    if normalized in {"ml", "millilitre", "milliliter", "millilitres", "milliliters"}:
        return "ml", 1
    if normalized in {"pc", "piece", "pieces"}:
        return "pc", 1
    return None


def _same_measure(variant_unit: str | None, requested_unit: str) -> bool:
    if variant_unit is None:
        return False
    variant_measure = _unit_factor(variant_unit)
    requested_measure = _unit_factor(requested_unit)
    return (
        variant_measure is not None
        and requested_measure is not None
        and variant_measure[0] == requested_measure[0]
    )


def _name_matches(product_name: str, requested_name: str) -> bool:
    requested_words = requested_name.casefold().split()
    normalized_product = product_name.casefold()
    return bool(requested_words) and all(word in normalized_product for word in requested_words)


def _amount(quantity: float, unit: str) -> int:
    normalized = _unit_factor(unit)
    if normalized is None:
        return -1
    return int(quantity * normalized[1])


def _variant_amount(variant: Variant) -> int:
    if variant.quantity_value is None or variant.unit is None:
        return -1
    normalized = _unit_factor(variant.unit)
    if normalized is None:
        return -1
    return int(variant.quantity_value * normalized[1])
