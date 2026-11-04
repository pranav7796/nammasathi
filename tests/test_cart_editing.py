from dataclasses import replace

import pytest

from app.commerce.base import CartLine
from app.commerce.mock import MockInstamart
from app.orchestrator.state_machine import (
    CallCommerce,
    CartEntry,
    ItemsExtracted,
    Say,
    Session,
    State,
    UserCancelled,
    UserUtterance,
    transition,
)

MILK = CartEntry("milk_500ml", "sku_milk_500ml", "Nandini Toned Milk", "500 ml", 2, 26)
RICE = CartEntry("rice_1kg", "sku_rice_1kg", "Sona Masoori Rice", "1 kg", 2, 78)
ATTA = CartEntry("atta_5kg", "sku_atta_5kg", "Aashirvaad Whole Wheat Atta", "5 kg", 1, 299)


def readback_session() -> Session:
    return replace(
        Session(state=State.READBACK),
        cart=(MILK, RICE),
        cart_hash="old-hash",
        total_inr=208,
        cart_version=3,
        confirmed_version=3,
    )


def test_add_edit_invalidates_confirmation_and_reads_back() -> None:
    session, _ = transition(
        readback_session(),
        UserUtterance("add atta", "add"),
    )
    assert session.state is State.COLLECTING
    assert session.confirmed_version is None

    updated, effects = transition(
        session,
        ItemsExtracted(resolved=(ATTA,), operation="add"),
    )
    assert updated.state is State.READBACK
    assert updated.cart == (MILK, RICE, ATTA)
    assert updated.cart_version == 4
    assert updated.confirmed_version is None
    assert any(
        isinstance(effect, CallCommerce) and effect.op == "update_cart" for effect in effects
    )


def test_remove_edit_removes_only_resolved_sku() -> None:
    collecting = replace(readback_session(), state=State.COLLECTING, confirmed_version=None)
    updated, _ = transition(
        collecting,
        ItemsExtracted(resolved=(MILK,), operation="remove"),
    )
    assert updated.state is State.READBACK
    assert updated.cart == (RICE,)
    assert updated.cart_version == 4


def test_change_quantity_replaces_existing_line() -> None:
    collecting = replace(readback_session(), state=State.COLLECTING, confirmed_version=None)
    rice_three = replace(RICE, quantity=3)
    updated, _ = transition(
        collecting,
        ItemsExtracted(resolved=(rice_three,), operation="change_qty"),
    )
    assert updated.state is State.READBACK
    assert updated.cart == (MILK, rice_three)
    assert updated.cart_version == 4


def test_cancel_clears_unsubmitted_cart_and_invalidates_confirmation() -> None:
    updated, effects = transition(readback_session(), UserCancelled())
    assert updated.state is State.COLLECTING
    assert updated.cart == ()
    assert updated.cart_hash == ""
    assert updated.confirmed_version is None
    assert updated.cart_version == 4
    assert any(isinstance(e, CallCommerce) and e.op == "restore_cart" for e in effects)
    assert not any(isinstance(e, Say) for e in effects), "not cancelled until the cart is restored"


def test_completed_order_is_immutable_to_later_edit() -> None:
    completed = replace(readback_session(), state=State.DONE, order_ids=("IM000001",))
    updated, effects = transition(completed, UserUtterance("remove milk", "remove"))
    assert updated == completed
    assert any(
        isinstance(effect, Say) and effect.template == "order_already_placed" for effect in effects
    )


@pytest.mark.asyncio
async def test_mock_cart_edit_methods_preserve_sku_quantities() -> None:
    mock = MockInstamart()
    await mock.update_cart("addr_home", [])
    cart = await mock.add_to_cart(
        "addr_home",
        [CartLine(spin_id="milk_500ml", sku_id="sku_milk_500ml", quantity=2)],
    )
    assert cart.items[0].quantity == 2
    cart = await mock.change_quantity("addr_home", "sku_milk_500ml", 4)
    assert cart.items[0].quantity == 4
    cart = await mock.remove_from_cart("addr_home", {"sku_milk_500ml"})
    assert cart.items == []
