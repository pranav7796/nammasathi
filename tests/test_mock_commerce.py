from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.commerce.base import CartLine, CommerceAdapter, CommerceError
from app.commerce.mock import CATALOG, MockInstamart

HOME = "addr_home"


def line(spin: str, qty: int = 1) -> CartLine:
    return CartLine(spin_id=spin, sku_id=f"sku_{spin}", quantity=qty)


@pytest.fixture
def im() -> MockInstamart:
    return MockInstamart(now=datetime(2026, 11, 3, 10, 0, tzinfo=UTC))


def test_implements_protocol(im: MockInstamart) -> None:
    adapter: CommerceAdapter = im
    assert adapter is im


def test_catalog_size_and_variants() -> None:
    assert len(CATALOG) >= 20
    atta = next(p for p in CATALOG if p.id == "p_atta")
    assert {v.pack_size for v in atta.variants} == {"1 kg", "5 kg"}


async def test_get_addresses(im: MockInstamart) -> None:
    assert (await im.get_addresses())[0].id == HOME


async def test_search_with_kannada_synonym(im: MockInstamart) -> None:
    res = await im.search_products(HOME, "haalu")
    assert res.products[0].brand == "Nandini"


async def test_search_out_of_stock_returns_similar(im: MockInstamart) -> None:
    res = await im.search_products(HOME, "coriander")
    assert not any(v.in_stock for p in res.products for v in p.variants)
    assert 0 < len(res.similar_products) <= 2


async def test_empty_query_rejected(im: MockInstamart) -> None:
    with pytest.raises(CommerceError, match="Missing"):
        await im.search_products(HOME, "  ")


async def test_go_to_items(im: MockInstamart) -> None:
    assert len(await im.go_to_items(HOME)) >= 3


async def test_update_cart_replaces_whole_cart(im: MockInstamart) -> None:
    await im.update_cart(HOME, [line("milk_500ml", 2)])
    cart = await im.update_cart(HOME, [line("atta_5kg")])
    assert [i.spin_id for i in cart.items] == ["atta_5kg"]


async def test_out_of_stock_add_fails(im: MockInstamart) -> None:
    with pytest.raises(CommerceError, match="out of stock"):
        await im.update_cart(HOME, [line("coriander_100g")])


async def test_bad_sku_rejected(im: MockInstamart) -> None:
    with pytest.raises(CommerceError, match="skuId"):
        await im.update_cart(HOME, [CartLine(spin_id="milk_500ml", sku_id="x", quantity=1)])


async def test_min_order_99(im: MockInstamart) -> None:
    cart = await im.update_cart(HOME, [line("milk_500ml", 2)])  # ₹52
    assert cart.errors and "Minimum order" in cart.errors[0]
    with pytest.raises(CommerceError, match="Minimum order"):
        await im.checkout(HOME)
    cart = await im.update_cart(HOME, [line("milk_500ml", 4)])  # ₹104
    assert cart.errors == []
    assert cart.grand_total_inr == 104 + cart.delivery_fee_inr


async def test_checkout_cod_success(im: MockInstamart) -> None:
    await im.update_cart(HOME, [line("atta_5kg")])
    res = await im.checkout(HOME)
    assert res.success and res.order_ids
    assert (await im.get_orders())[0].order_id == res.order_ids[0]
    assert (await im.get_cart()).items == []


async def test_duplicate_checkout_detected(im: MockInstamart) -> None:
    await im.update_cart(HOME, [line("atta_5kg")])
    await im.checkout(HOME)
    await im.update_cart(HOME, [line("atta_5kg")])
    dup = await im.checkout(HOME)
    assert not dup.success and dup.probable_duplicate
    assert len(im.orders) == 1
    ok = await im.checkout(HOME, confirm_duplicate=True)
    assert ok.success and len(im.orders) == 2


async def test_timeout_after_success_is_reconcilable(im: MockInstamart) -> None:
    await im.update_cart(HOME, [line("atta_5kg")])
    im.fail_next_checkout_after_success = True
    with pytest.raises(TimeoutError):
        await im.checkout(HOME)
    assert len(await im.get_orders()) == 1  # reconcile finds it — do NOT retry


async def test_timeout_before_placing(im: MockInstamart) -> None:
    await im.update_cart(HOME, [line("atta_5kg")])
    im.fail_next_checkout_before = True
    with pytest.raises(TimeoutError):
        await im.checkout(HOME)
    assert await im.get_orders() == []


async def test_unserviceable_address(im: MockInstamart) -> None:
    im.unserviceable_address_ids.add(HOME)
    with pytest.raises(CommerceError, match="not serviceable"):
        await im.search_products(HOME, "milk")
