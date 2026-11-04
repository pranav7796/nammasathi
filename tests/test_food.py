"""Swiggy Food: the adapter against documented and live shapes, then the shared order engine."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from app.commerce.base import (
    CartLine,
    CommerceError,
    CommerceNotAttempted,
    CommerceRejected,
    CommerceWriteDisabled,
    PaymentMethod,
)
from app.commerce.swiggy import OAUTH_REDIRECT_URI, KeyringTokenStorage
from app.food.mock import MockFood
from app.food.swiggy import SWIGGY_FOOD_URL, SwiggyFood
from app.orchestrator.state_machine import (
    CallStarted,
    Effect,
    OptionSelected,
    Say,
    State,
    UserConfirmed,
    UserUtterance,
)
from app.orchestrator.turn_runner import TextTurnRunner
from tests.test_swiggy_adapter import (
    FakeKeyring,
    FakeMcpResult,
    _no_callback,
    _no_redirect,
    _patch_mcp,
    _scripted,
)  # pyright: ignore[reportPrivateUsage]

ADDRESS = "addr__token"


def _adapter(*, writes: bool = True, checkout: bool = True) -> SwiggyFood:
    return SwiggyFood(
        address_id=ADDRESS,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(FakeKeyring()),
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
        allow_real_checkout=checkout,
        allow_cart_writes=writes,
    )


def _dish(**overrides: object) -> dict[str, object]:
    return {
        "menu_item_id": "m1",
        "name": "Aloo Dum Biryani",
        "price": 330,
        "inStock": 1,
        "restaurant_id": "r1",
        "restaurant_name": "Meghana Foods",
        **overrides,
    }


def _menu(*items: dict[str, object]) -> FakeMcpResult:
    return FakeMcpResult({"items": list(items), "totalItems": len(items), "hasMore": False})


def _cart(*items: dict[str, object], **pricing: object) -> FakeMcpResult:
    return FakeMcpResult(
        {
            "addressId": ADDRESS,
            "successful": True,
            "data": {
                "restaurant": {"id": "r1", "name": "Meghana Foods"},
                "items": list(items),
                "pricing": {
                    "item_total": 330,
                    "delivery_charge": 30,
                    "taxes_and_charges": 20,
                    "to_pay": 380,
                    **pricing,
                },
                "offers": {"coupon_discount": 0},
            },
        }
    )


def _line(**overrides: object) -> dict[str, object]:
    return {
        "menu_item_id": "m1",
        "name": "Aloo Dum Biryani",
        "quantity": 1,
        "total": 330,
        "final_price": 330,
        "in_stock": True,
        **overrides,
    }


EMPTY_CART = FakeMcpResult(
    {"addressId": ADDRESS, "data": None, "statusCode": 0, "statusMessage": "", "successful": True}
)


def test_empty_cart_as_observed_live_reads_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(monkeypatch, get_food_cart=EMPTY_CART)

    cart = asyncio.run(_adapter().get_cart())

    assert cart.items == []
    assert cart.address_id == ADDRESS
    assert calls[0]["arguments"] == {"addressId": ADDRESS}


def test_summary_results_are_drilled_for_their_real_customizations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(
        monkeypatch,
        search_menu=_menu(_dish(hasAddons=True, addons=[{"groupId": "g", "minAddons": 0}])),
    )

    result = asyncio.run(_adapter().search_products(ADDRESS, "biryani"))

    assert [product.display_name for product in result.products] == [
        "Aloo Dum Biryani from Meghana Foods"
    ]
    assert calls[1]["arguments"]["restaurantIdOfAddedItem"] == "r1"  # type: ignore[index]


def test_dish_requiring_addons_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(
        monkeypatch,
        search_menu=_menu(_dish(hasAddons=True, addons=[{"groupId": "g", "minAddons": 1}])),
    )

    assert asyncio.run(_adapter().search_products(ADDRESS, "biryani")).products == []


def test_dish_with_several_choice_groups_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    group = {"groupId": "g", "variations": [{"id": "v", "name": "Half"}]}
    _scripted(monkeypatch, search_menu=_menu(_dish(hasVariants=True, variantsV2=[group, group])))

    assert asyncio.run(_adapter().search_products(ADDRESS, "biryani")).products == []


def test_sizes_become_choices_with_their_own_format_and_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v2 = {
        "groupId": "g1",
        "variations": [
            {"id": "half", "name": "Half", "price": 220, "inStock": 1},
            {"id": "full", "name": "Full", "price": 380, "inStock": 0},
        ],
    }
    calls = _scripted(
        monkeypatch,
        search_menu=_menu(_dish(hasVariants=True, variantsV2=[v2])),
        update_food_cart=FakeMcpResult({"statusCode": 0}),
        get_food_cart=_cart(_line(variants=[{"name": "Half"}], total=220)),
    )
    adapter = _adapter()

    async def scenario() -> Any:
        found = await adapter.search_products(ADDRESS, "biryani")
        half, full = found.products[0].variants
        assert (half.pack_size, half.price_inr, half.in_stock) == ("Half", 220, True)
        assert (full.pack_size, full.in_stock) == ("Full", False)
        return await adapter.update_cart(
            ADDRESS, [CartLine(spin_id=half.spin_id, sku_id=half.sku_id, quantity=2)]
        )

    cart = asyncio.run(scenario())

    update = next(call for call in calls if call["name"] == "update_food_cart")
    assert update["arguments"] == {
        "addressId": ADDRESS,
        "restaurantId": "r1",
        "restaurantName": "Meghana Foods",
        "cartItems": [
            {
                "menu_item_id": "m1",
                "quantity": 2,
                "variantsV2": [{"group_id": "g1", "variation_id": "half"}],
            }
        ],
    }
    assert cart.items[0].sku_id == "m1:half"


def test_search_stays_inside_the_carts_restaurant(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(monkeypatch, get_food_cart=_cart(_line()), search_menu=_menu(_dish()))
    adapter = _adapter()

    async def scenario() -> None:
        await adapter.get_cart()
        await adapter.search_products(ADDRESS, "raita")

    asyncio.run(scenario())

    search = next(call for call in calls if call["name"] == "search_menu")
    assert search["arguments"]["restaurantIdOfAddedItem"] == "r1"  # type: ignore[index]


def test_two_restaurants_are_refused_rather_than_flushing_the_cart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(
        monkeypatch,
        search_menu=_menu(_dish(), _dish(menu_item_id="m2", restaurant_id="r2")),
    )
    adapter = _adapter()

    async def scenario() -> None:
        found = await adapter.search_products(ADDRESS, "biryani")
        lines = [
            CartLine(spin_id=p.variants[0].spin_id, sku_id=p.variants[0].sku_id, quantity=1)
            for p in found.products
        ]
        await adapter.update_cart(ADDRESS, lines)

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(scenario())
    assert "update_food_cart" not in [call["name"] for call in calls]


def test_line_with_addons_cannot_be_rewritten_blindly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(
        monkeypatch, get_food_cart=_cart(_line(addons=[{"name": "Raita", "id": "a"}]))
    )
    adapter = _adapter()

    async def scenario() -> None:
        cart = await adapter.get_cart()
        item = cart.items[0]
        assert item.pack_size == "with Raita"
        await adapter.update_cart(
            ADDRESS, [CartLine(spin_id=item.spin_id, sku_id=item.sku_id, quantity=2)]
        )

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(scenario())
    assert "update_food_cart" not in [call["name"] for call in calls]


def test_cart_writes_need_their_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult({}))

    for lines in ([], [CartLine(spin_id="m", sku_id="k", quantity=1)]):
        with pytest.raises(CommerceWriteDisabled):
            asyncio.run(_adapter(writes=False).update_cart(ADDRESS, lines))
    assert calls == []


def test_emptying_uses_flush_and_proves_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(
        monkeypatch, flush_food_cart=FakeMcpResult({"statusCode": 0}), get_food_cart=EMPTY_CART
    )

    assert asyncio.run(_adapter().update_cart(ADDRESS, [])).items == []
    assert [call["name"] for call in calls] == ["flush_food_cart", "get_food_cart"]


def test_cart_maps_fees_and_ignores_a_merely_suggested_coupon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scripted(monkeypatch, get_food_cart=_cart(_line(quantity=2, total=330, final_price=165)))

    cart = asyncio.run(_adapter().get_cart())

    assert cart.grand_total_inr == 380
    assert cart.delivery_fee_inr == 50
    assert cart.savings_inr == 0
    assert cart.items[0].unit_price_inr == Decimal("165")
    assert cart.items[0].name == "Aloo Dum Biryani from Meghana Foods"


def _ready_checkout(
    monkeypatch: pytest.MonkeyPatch, placed: dict[str, object], *, cod: bool = True
):  # type: ignore[no-untyped-def]
    calls = _scripted(
        monkeypatch,
        get_payment_options=FakeMcpResult(
            {"cod": {"available": cod, "id": "COD"}, "allMethods": []}
        ),
        place_food_order=FakeMcpResult({"success": True, "data": placed}),
    )
    adapter = _adapter()

    async def scenario() -> Any:
        await adapter.get_payment_options()
        return await adapter.checkout(ADDRESS, PaymentMethod.COD)

    return scenario, calls


def test_checkout_sends_cash_as_the_live_tool_requires(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario, calls = _ready_checkout(
        monkeypatch, {"orderId": "F1", "status": "CONFIRMED", "totalAmount": 380}
    )

    result = asyncio.run(scenario())

    assert result.success is True
    assert result.order_ids == ["F1"]
    assert calls[-1]["arguments"] == {"addressId": ADDRESS, "paymentMethod": "Cash"}


@pytest.mark.parametrize(
    "placed",
    [
        {"orderId": "F1", "status": "PENDING_PAYMENT", "normalizedStatus": "pending"},
        {"orderId": None, "status": "CONFIRMED"},
    ],
)
def test_pending_or_missing_order_is_not_success(
    monkeypatch: pytest.MonkeyPatch, placed: dict[str, object]
) -> None:
    scenario, _ = _ready_checkout(monkeypatch, placed)

    assert asyncio.run(scenario()).success is False


def test_checkout_refused_without_switch_or_cod(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario, calls = _ready_checkout(monkeypatch, {}, cod=False)
    with pytest.raises(CommerceNotAttempted):
        asyncio.run(scenario())
    assert "place_food_order" not in [call["name"] for call in calls]

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(_adapter(checkout=False).checkout(ADDRESS))


def test_closed_restaurant_is_a_clear_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mcp(
        monkeypatch, FakeMcpResult({"success": False, "error": {"message": "Restaurant closed"}})
    )

    with pytest.raises(CommerceRejected):
        asyncio.run(_adapter().get_payment_options())


def test_order_history_and_tracking_map(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(
        monkeypatch,
        get_food_orders=FakeMcpResult(
            {
                "orders": [
                    {
                        "orderId": "F1",
                        "orderTotal": "₹380",
                        "orderStatus": "Delivered",
                        "orderedItems": "Aloo Dum Biryani x 1",
                    }
                ]
            }
        ),
        track_food_order=FakeMcpResult(
            {"orders": [{"orderId": "F1", "orderStatus": "On the way"}]}
        ),
    )
    adapter = _adapter()

    async def scenario() -> tuple[Any, Any]:
        return await adapter.get_orders(count=20), await adapter.track_order("F1", 0.0, 0.0)

    orders, tracking = asyncio.run(scenario())

    assert orders[0].total_inr == 380
    assert orders[0].status == "Delivered"
    assert tracking.status_message == "On the way"
    assert calls[0]["arguments"] == {"addressId": ADDRESS, "orderCount": 15}


def test_order_without_id_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(monkeypatch, get_food_orders=FakeMcpResult({"orders": [{"orderTotal": "1"}]}))

    with pytest.raises(CommerceError):
        asyncio.run(_adapter().get_orders())


@pytest.mark.parametrize("tool", ["checkout", "update_cart", "book_table", "apply_food_coupon"])
def test_only_food_tools_can_be_called(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult({}))

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(_adapter()._call(tool, {}))  # pyright: ignore[reportPrivateUsage]
    assert calls == []


def test_endpoint_is_limited_to_the_food_server() -> None:
    assert SWIGGY_FOOD_URL == "https://mcp.swiggy.com/food"
    with pytest.raises(ValueError, match="endpoint"):
        SwiggyFood(
            address_id=ADDRESS,
            endpoint="https://mcp.swiggy.com/im",
            redirect_uri=OAUTH_REDIRECT_URI,
            storage=KeyringTokenStorage(FakeKeyring()),
            redirect_handler=_no_redirect,
            callback_handler=_no_callback,
        )


# ------------------------------------------------- the shared engine over the mock
def _says(effects: list[Effect]) -> list[str]:
    return [effect.template for effect in effects if isinstance(effect, Say)]


async def _food_runner() -> tuple[TextTurnRunner, MockFood]:
    shop = MockFood()
    runner = await TextTurnRunner.create(shop, address_id="addr_home", domain="food")
    await runner.dispatch(CallStarted(user_known=True, language="en-IN"))
    return runner, shop


async def test_same_dish_at_two_restaurants_asks_which() -> None:
    from app.orchestrator.state_machine import RequestedItem

    runner, shop = await _food_runner()

    effects = await runner.dispatch(
        UserUtterance("chicken biryani", "add", (RequestedItem("chicken biryani", 1, None),))
    )

    assert runner.session.state is State.CLARIFYING
    assert "clarification" in _says(effects)
    assert {o.name for o in runner.session.pending.options} == {  # type: ignore[union-attr]
        "Chicken Biryani from Meghana Foods",
        "Chicken Biryani from Paradise",
    }
    assert shop._cart == []  # pyright: ignore[reportPrivateUsage]


async def test_full_food_order_with_readback_and_verification() -> None:
    from app.orchestrator.state_machine import RequestedItem

    runner, shop = await _food_runner()
    await runner.dispatch(
        UserUtterance("masala dosa", "add", (RequestedItem("masala dosa", 2, None),))
    )
    assert runner.session.state is State.READBACK
    assert runner.session.cart[0].quantity == 2

    await runner.dispatch(UserConfirmed(runner.session.cart_version, runner.session.readback_hash))

    assert runner.session.state is State.DONE
    assert runner.session.order_verified is True
    assert shop.checkout_calls == 1


async def test_second_restaurant_stays_out_of_an_open_cart() -> None:
    from app.orchestrator.state_machine import RequestedItem

    runner, shop = await _food_runner()
    await runner.dispatch(
        UserUtterance("masala dosa", "add", (RequestedItem("masala dosa", 1, None),))
    )

    effects = await runner.dispatch(
        UserUtterance("margherita", "add", (RequestedItem("margherita", 1, None),))
    )

    assert "clarification" in _says(effects), "that dish isn't at this restaurant"
    assert {item.name for item in shop._cart} == {"Masala Dosa from Vidyarthi Bhavan"}  # pyright: ignore[reportPrivateUsage]


async def test_size_is_asked_and_chosen() -> None:
    from app.orchestrator.state_machine import RequestedItem

    runner, _ = await _food_runner()
    await runner.dispatch(
        UserUtterance("margherita", "add", (RequestedItem("margherita", 1, None),))
    )
    pending = runner.session.pending
    assert pending is not None
    assert [option.pack_size for option in pending.options] == ["regular", "large"]

    await runner.dispatch(OptionSelected(1))

    assert runner.session.cart[0].pack_size == "large"
