from __future__ import annotations

from dataclasses import replace

import pytest

from app.commerce.base import Cart, CartLine, CommerceError
from app.commerce.mock import MockInstamart
from app.orchestrator.state_machine import (
    CallStarted,
    CartEntry,
    ItemsExtracted,
    RequestedItem,
    Session,
    State,
    UserConfirmed,
    UserDone,
    UserRejected,
    UserUtterance,
    transition,
)
from app.orchestrator.turn_runner import TextTurnRunner

HOME = "addr_home"
MILK = CartEntry("milk_500ml", "sku_milk_500ml", "Nandini Toned Milk", "500 ml", 1, 26)
RICE = CartEntry("rice_1kg", "sku_rice_1kg", "Sona Masoori Rice", "1 kg", 1, 78)
ATTA = CartEntry("atta_5kg", "sku_atta_5kg", "Aashirvaad Whole Wheat Atta", "5 kg", 1, 299)


class RecordingCommerce(MockInstamart):
    """In-memory adapter recording full replacements and injectable write failures."""

    def __init__(self) -> None:
        super().__init__()
        self.update_calls: list[tuple[str, tuple[CartLine, ...]]] = []
        self.events: list[str] = []
        self.update_failure: str | None = None
        self.fail_next_reconciliation = False

    async def update_cart(self, address_id: str, items: list[CartLine]) -> Cart:
        self.events.append("update_cart")
        self.update_calls.append((address_id, tuple(items)))
        if self.update_failure == "unavailable":
            self.update_failure = None
            raise CommerceError("Product unavailable")
        if self.update_failure == "max_quantity":
            self.update_failure = None
            if any(item.quantity > 3 for item in items):
                raise CommerceError("Maximum quantity exceeded")
        if self.update_failure == "timeout_partial":
            self.update_failure = None
            await super().update_cart(address_id, items[:1])
            raise TimeoutError("Mutation response timed out")
        if self.update_failure == "partial_success":
            self.update_failure = None
            return await super().update_cart(address_id, items[:1])
        cart = await super().update_cart(address_id, items)
        if self.update_failure == "quantity_changed":
            self.update_failure = None
            self._cart = [
                item.model_copy(update={"quantity": max(item.quantity - 1, 1)})
                for item in self._cart
            ]
            return self._build_cart()
        if self.update_failure == "price_changed":
            self.update_failure = None
            self._cart = [
                item.model_copy(update={"unit_price_inr": item.unit_price_inr + 5})
                for item in self._cart
            ]
            return self._build_cart()
        if self.update_failure == "wrong_address":
            self.update_failure = None
            self._cart_address = "addr_other"
            return self._build_cart()
        return cart

    async def get_cart(self) -> Cart:
        self.events.append("get_cart")
        if self.fail_next_reconciliation:
            self.fail_next_reconciliation = False
            raise CommerceError("Cart read unavailable")
        return await super().get_cart()

    def set_cart_price(self, price: int) -> None:
        self._cart = [item.model_copy(update={"unit_price_inr": price}) for item in self._cart]


async def _runner(
    adapter: RecordingCommerce | None = None,
) -> tuple[TextTurnRunner, RecordingCommerce]:
    commerce = adapter or RecordingCommerce()
    runner = await TextTurnRunner.create(commerce, address_id=HOME)
    await runner.dispatch(CallStarted(user_known=True))
    return runner, commerce


async def _seed(runner: TextTurnRunner, *entries: CartEntry) -> None:
    if entries:
        await runner.dispatch(ItemsExtracted(resolved=tuple(entries)))


def _lines(*entries: CartEntry) -> tuple[CartLine, ...]:
    return tuple(
        CartLine(spin_id=item.spin_id, sku_id=item.sku_id, quantity=item.quantity)
        for item in entries
    )


@pytest.mark.asyncio
async def test_add_product_to_empty_cart_sends_complete_cart_and_never_checks_out() -> None:
    runner, adapter = await _runner()

    await _seed(runner, MILK)

    assert adapter.update_calls == [(HOME, _lines(MILK))]
    assert [(item.spin_id, item.sku_id, item.quantity) for item in runner.session.cart] == [
        (MILK.spin_id, MILK.sku_id, MILK.quantity)
    ]
    assert adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_add_preserves_existing_lines_quantities_and_address_in_full_replacement() -> None:
    runner, adapter = await _runner()
    await _seed(runner, MILK, RICE)
    adapter.update_calls.clear()

    await _seed(runner, ATTA)

    assert adapter.update_calls == [(HOME, _lines(MILK, RICE, ATTA))]
    assert [(item.sku_id, item.quantity) for item in runner.session.cart] == [
        (MILK.sku_id, 1),
        (RICE.sku_id, 1),
        (ATTA.sku_id, 1),
    ]
    assert adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_add_same_product_increases_existing_quantity_without_duplicate_line() -> None:
    runner, adapter = await _runner()
    await _seed(runner, replace(MILK, quantity=2))
    adapter.update_calls.clear()

    await _seed(runner, MILK)

    assert adapter.update_calls == [(HOME, _lines(replace(MILK, quantity=3)))]
    assert [(item.sku_id, item.quantity) for item in runner.session.cart] == [(MILK.sku_id, 3)]


@pytest.mark.asyncio
async def test_change_quantity_can_increase_and_decrease_without_changing_other_items() -> None:
    runner, adapter = await _runner()
    await _seed(runner, replace(MILK, quantity=3), RICE)
    adapter.update_calls.clear()

    await runner.dispatch(
        ItemsExtracted(resolved=(replace(MILK, quantity=4),), operation="change_qty")
    )
    assert adapter.update_calls[-1] == (HOME, _lines(replace(MILK, quantity=4), RICE))

    await runner.dispatch(
        ItemsExtracted(resolved=(replace(MILK, quantity=1),), operation="change_qty")
    )
    assert adapter.update_calls[-1] == (HOME, _lines(MILK, RICE))
    assert [(item.sku_id, item.quantity) for item in runner.session.cart] == [
        (MILK.sku_id, 1),
        (RICE.sku_id, 1),
    ]


@pytest.mark.asyncio
async def test_remove_one_item_preserves_unrelated_items() -> None:
    runner, adapter = await _runner()
    await _seed(runner, MILK, RICE, ATTA)
    adapter.update_calls.clear()

    await runner.dispatch(ItemsExtracted(resolved=(RICE,), operation="remove"))

    assert adapter.update_calls == [(HOME, _lines(MILK, ATTA))]
    assert [item.sku_id for item in runner.session.cart] == [MILK.sku_id, ATTA.sku_id]


@pytest.mark.asyncio
async def test_remove_final_item_sends_empty_full_cart() -> None:
    runner, adapter = await _runner()
    await _seed(runner, MILK)
    adapter.update_calls.clear()

    await runner.dispatch(ItemsExtracted(resolved=(MILK,), operation="remove"))

    assert adapter.update_calls == [(HOME, ())]
    assert runner.session.cart == ()
    assert runner.session.address_id == HOME


@pytest.mark.asyncio
async def test_unavailable_search_result_requires_clarification_without_mutation() -> None:
    runner, adapter = await _runner()

    await runner.dispatch(
        UserUtterance("coriander", "add", (RequestedItem("coriander", 100, "g"),))
    )

    assert runner.session.state is State.CLARIFYING
    assert runner.session.pending is not None
    assert runner.session.pending.kind == "out_of_stock"
    assert adapter.update_calls == []


@pytest.mark.asyncio
async def test_quantity_limit_failure_reconciles_and_is_not_reported_as_success() -> None:
    runner, adapter = await _runner()
    await _seed(runner, RICE)
    adapter.events.clear()
    adapter.update_failure = "max_quantity"

    with pytest.raises(CommerceError, match="Maximum quantity exceeded"):
        await runner.dispatch(
            ItemsExtracted(resolved=(replace(RICE, quantity=4),), operation="change_qty")
        )

    assert adapter.events == ["update_cart", "get_cart"]
    assert [(item.sku_id, item.quantity) for item in runner.session.cart] == [(RICE.sku_id, 1)]
    assert runner.session.confirmed_version is None


@pytest.mark.asyncio
async def test_price_change_from_cart_response_replaces_search_price_in_readback() -> None:
    runner, adapter = await _runner()
    adapter.update_failure = "price_changed"

    await runner.dispatch(
        UserUtterance(
            "Nandini Toned 500 ml",
            "add",
            (RequestedItem("Nandini Toned", 500, "ml"),),
        )
    )

    assert runner.session.cart[0].unit_price_inr == MILK.unit_price_inr + 5
    assert runner.session.total_inr == MILK.unit_price_inr + 5 + 25
    assert runner.session.cart_hash != ""
    assert adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_product_disappearing_during_mutation_is_reconciled_and_propagated() -> None:
    runner, adapter = await _runner()
    await _seed(runner, MILK)
    adapter.events.clear()
    adapter.update_failure = "unavailable"

    with pytest.raises(CommerceError, match="Product unavailable"):
        await runner.dispatch(ItemsExtracted(resolved=(ATTA,)))

    assert adapter.events == ["update_cart", "get_cart"]
    assert [(item.sku_id, item.quantity) for item in runner.session.cart] == [(MILK.sku_id, 1)]
    assert adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_timeout_reconciles_partial_mutation_before_any_later_mutation() -> None:
    runner, adapter = await _runner()
    await _seed(runner, MILK, RICE)
    adapter.events.clear()
    adapter.update_failure = "timeout_partial"

    with pytest.raises(TimeoutError, match="response timed out"):
        await runner.dispatch(ItemsExtracted(resolved=(ATTA,)))

    assert adapter.events == ["update_cart", "get_cart"]
    assert len(adapter.update_calls) == 2  # one seed call, one failed call; no automatic retry
    assert [item.sku_id for item in runner.session.cart] == [MILK.sku_id]

    adapter.events.clear()
    await runner.dispatch(ItemsExtracted(resolved=(ATTA,)))

    assert adapter.events[0] == "update_cart"  # prior reconciliation succeeded before this call
    assert adapter.events == ["update_cart"]
    assert [item.sku_id for item in runner.session.cart] == [MILK.sku_id, ATTA.sku_id]


@pytest.mark.asyncio
async def test_mutation_is_blocked_until_failed_reconciliation_succeeds() -> None:
    runner, adapter = await _runner()
    await _seed(runner, MILK, RICE)
    adapter.events.clear()
    adapter.fail_next_reconciliation = True
    adapter.update_failure = "timeout_partial"

    with pytest.raises(TimeoutError, match="response timed out"):
        await runner.dispatch(ItemsExtracted(resolved=(ATTA,)))

    assert adapter.events == ["update_cart", "get_cart"]
    assert len(adapter.update_calls) == 2

    await runner.dispatch(ItemsExtracted(resolved=(ATTA,)))

    assert adapter.events == ["update_cart", "get_cart", "get_cart", "update_cart"]
    assert len(adapter.update_calls) == 3
    assert [item.sku_id for item in runner.session.cart] == [MILK.sku_id, ATTA.sku_id]


@pytest.mark.asyncio
async def test_partial_success_response_is_reconciled_and_reported_as_failure() -> None:
    runner, adapter = await _runner()
    await _seed(runner, MILK, RICE)
    adapter.events.clear()
    adapter.update_failure = "partial_success"

    with pytest.raises(CommerceError, match="did not match the requested cart"):
        await runner.dispatch(ItemsExtracted(resolved=(ATTA,)))

    assert adapter.events == ["update_cart", "get_cart"]
    assert [item.sku_id for item in runner.session.cart] == [MILK.sku_id]
    assert runner.session.confirmed_version is None
    assert adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_quantity_mismatch_in_actual_cart_is_reconciled_and_reported() -> None:
    runner, adapter = await _runner()
    await _seed(runner, replace(RICE, quantity=2))
    adapter.events.clear()
    adapter.update_failure = "quantity_changed"

    with pytest.raises(CommerceError, match="did not match the requested cart"):
        await runner.dispatch(
            ItemsExtracted(resolved=(replace(RICE, quantity=3),), operation="change_qty")
        )

    assert adapter.events == ["update_cart", "get_cart"]
    assert [(item.sku_id, item.quantity) for item in runner.session.cart] == [(RICE.sku_id, 2)]
    assert runner.session.confirmed_version is None


@pytest.mark.asyncio
async def test_wrong_address_response_is_reconciled_and_never_authorizes_checkout() -> None:
    runner, adapter = await _runner()
    adapter.update_failure = "wrong_address"

    with pytest.raises(CommerceError, match="did not match the requested cart"):
        await runner.dispatch(ItemsExtracted(resolved=(MILK,)))

    assert adapter.events == ["update_cart", "get_cart"]
    assert runner.session.address_id == "addr_other"
    assert adapter.checkout_calls == 0

    await runner.dispatch(UserConfirmed(runner.session.cart_version, runner.session.cart_hash))

    assert runner.session.state is State.READBACK
    assert adapter.checkout_calls == 0


@pytest.mark.asyncio
async def test_price_and_total_changes_from_get_cart_are_reflected_before_confirmation() -> None:
    runner, adapter = await _runner()
    await _seed(runner, MILK)
    await runner.dispatch(UserRejected())
    adapter.set_cart_price(31)

    await runner.dispatch(UserDone())

    assert adapter.events[-1] == "get_cart"
    assert runner.session.cart[0].unit_price_inr == 31
    assert runner.session.total_inr == 56
    assert runner.session.confirmed_version is None


def test_checkout_transition_requires_confirmation_bound_to_cart_address_and_total() -> None:
    from app.orchestrator.state_machine import cart_hash

    entry = MILK
    session = Session(
        state=State.READBACK,
        cart=(entry,),
        total_inr=51,
        address_id=HOME,
        cart_version=4,
    )
    session = replace(session, cart_hash=cart_hash(session.cart, 51, HOME))

    stale, effects = transition(
        session,
        UserConfirmed(cart_version=4, cart_hash=cart_hash(session.cart, 50, HOME)),
    )
    assert stale.state is State.READBACK
    assert not any(effect.__class__.__name__ == "PlaceOrder" for effect in effects)

    wrong_address, effects = transition(
        session,
        UserConfirmed(cart_version=4, cart_hash=cart_hash(session.cart, 51, "another-address")),
    )
    assert wrong_address.state is State.READBACK
    assert not any(effect.__class__.__name__ == "PlaceOrder" for effect in effects)

    accepted, effects = transition(
        session,
        UserConfirmed(cart_version=4, cart_hash=session.cart_hash),
    )
    assert accepted.state is State.PLACING
    assert any(effect.__class__.__name__ == "PlaceOrder" for effect in effects)


@pytest.mark.asyncio
async def test_llm_classified_confirmation_cannot_emit_checkout_effect() -> None:
    from types import SimpleNamespace

    class ConfirmingExtractor:
        def extract(self, text: str, *, language: str) -> SimpleNamespace:
            del text, language
            return SimpleNamespace(
                needs_clarification=False,
                clarification_question=None,
                intent="confirm",
                items=[],
            )

    session = replace(
        # A raw model classification is represented as a user utterance by the
        # runner, and the state machine treats it as unclear at READBACK.
        Session(),
        state=State.READBACK,
        cart=(MILK,),
        total_inr=51,
        address_id=HOME,
        cart_version=1,
    )
    runner = TextTurnRunner(RecordingCommerce(), HOME, session, ConfirmingExtractor())  # type: ignore[arg-type]

    effects = await runner.dispatch_text("maybe proceed")

    assert runner.session.state is State.READBACK
    assert runner.adapter.checkout_calls == 0  # type: ignore[attr-defined]
    assert not any(effect.__class__.__name__ == "PlaceOrder" for effect in effects)
