"""Swiggy Food behind the same commerce interface as Instamart.

Contracts: https://mcp.swiggy.com/builders/docs/reference/food/search_menu.md,
update_food_cart.md, get_food_cart.md, place_food_order.md, get_food_orders.md, and
track_food_order.md. Live details are recorded in docs/swiggy-integration-notes.md.

A dish is a Product and a size is a Variant, so the cart safety engine is shared with
groceries. A Food cart holds one restaurant, so search stays inside the cart's restaurant
and a second restaurant is refused rather than silently flushing the cart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast

import httpx2
from mcp.client.auth import AuthorizationCodeResult
from mcp.client.auth.oauth2 import TokenStorage

from app.commerce.base import (
    Cart,
    CartIssue,
    CartItem,
    CartLine,
    CommerceError,
    CommerceNotAttempted,
    CommerceWriteDisabled,
    OrderResult,
    OrderSummary,
    OrderSummaryItem,
    OrderTracking,
    PaymentMethod,
    PaymentOptions,
    Product,
    SearchResult,
    Variant,
)
from app.commerce.swiggy import (
    OAUTH_REDIRECT_URI,
    SWIGGY_MCP_URL,
    SWIGGY_STAGING_MCP_URL,
    DelegatedLogin,
    SwiggyMcpClient,
    _list,  # pyright: ignore[reportPrivateUsage]
    _money,  # pyright: ignore[reportPrivateUsage]
    _optional_int,  # pyright: ignore[reportPrivateUsage]
    _optional_money,  # pyright: ignore[reportPrivateUsage]
    _optional_str,  # pyright: ignore[reportPrivateUsage]
    food_or_dining_url,
)

SWIGGY_FOOD_URL = food_or_dining_url(SWIGGY_MCP_URL, "food")
SWIGGY_STAGING_FOOD_URL = food_or_dining_url(SWIGGY_STAGING_MCP_URL, "food")
_MAX_DRILLS = 5  # search_menu per restaurant, to learn a dish's real customizations
_MAX_DISHES = 3


@dataclass(frozen=True)
class _Dish:
    restaurant_id: str
    restaurant_name: str
    menu_item_id: str
    label: str = ""
    # The live API needs the same format the item came in: variants or variantsV2.
    selection: dict[str, object] = field(default_factory=lambda: dict[str, object]())


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _required_addons(item: Mapping[str, object]) -> bool:
    groups = _list(item.get("addons"), "addons", allow_missing=True)
    return any((_optional_int(group.get("minAddons")) or 0) > 0 for group in groups)


class SwiggyFood(SwiggyMcpClient):
    """Implements CommerceAdapter for one delivery address."""

    allowed_endpoints = frozenset({SWIGGY_FOOD_URL, SWIGGY_STAGING_FOOD_URL})
    read_tools = frozenset(
        {
            "get_addresses",
            "search_menu",
            "get_food_cart",
            "get_payment_options",
            "get_food_orders",
            "track_food_order",
        }
    )
    write_tools = frozenset({"update_food_cart", "flush_food_cart", "place_food_order"})

    def __init__(
        self,
        *,
        address_id: str,
        endpoint: str = SWIGGY_FOOD_URL,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        storage: TokenStorage | None = None,
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]] | None = None,
        allow_real_checkout: bool = False,
        allow_cart_writes: bool = False,
        delegated_auth: DelegatedLogin | None = None,
        http_transport: httpx2.AsyncBaseTransport | None = None,
        user_label: str = "",
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            redirect_uri=redirect_uri,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            allow_real_checkout=allow_real_checkout,
            allow_cart_writes=allow_cart_writes,
            delegated_auth=delegated_auth,
            http_transport=http_transport,
            user_label=user_label,
        )
        self.address_id = address_id
        self._dishes: dict[str, _Dish] = {}
        self._restaurant_id: str | None = None
        self._restaurant_name = ""
        self._cod_available = False

    async def search_products(self, address_id: str, query: str, offset: int = 0) -> SearchResult:
        if not query.strip():
            raise CommerceError("Search query cannot be empty")
        arguments: dict[str, object] = {"addressId": address_id, "query": query, "offset": offset}
        if self._restaurant_id:
            arguments["restaurantIdOfAddedItem"] = self._restaurant_id
        data = await self._call("search_menu", arguments)
        items = _list(data.get("items"), "items", allow_missing=True)
        products: list[Product] = []
        drills = 0
        for item in items:
            if len(products) >= _MAX_DISHES:
                break
            detail = item
            needs_detail = item.get("hasVariants") is True or item.get("hasAddons") is True
            if needs_detail and not self._restaurant_id:
                if drills >= _MAX_DRILLS:
                    continue
                drills += 1
                detail = await self._dish_detail(address_id, item)
            product = self._product(detail, fallback=item)
            if product is not None:
                products.append(product)
        return SearchResult(products=products)

    async def _dish_detail(
        self, address_id: str, item: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Results across restaurants omit customizations; a scoped search returns them."""
        data = await self._call(
            "search_menu",
            {
                "addressId": address_id,
                "query": str(item.get("name") or ""),
                "restaurantIdOfAddedItem": str(item.get("restaurant_id") or ""),
            },
        )
        for candidate in _list(data.get("items"), "items", allow_missing=True):
            if candidate.get("menu_item_id") == item.get("menu_item_id"):
                return candidate
        return item

    def _product(
        self, item: Mapping[str, object], *, fallback: Mapping[str, object]
    ) -> Product | None:
        """A dish we can order exactly as read out, or None if it needs choices we can't offer."""
        menu_item_id = _optional_str(item.get("menu_item_id"))
        name = _optional_str(item.get("name"))
        restaurant_id = _optional_str(fallback.get("restaurant_id")) or self._restaurant_id
        restaurant = _optional_str(fallback.get("restaurant_name")) or self._restaurant_name
        if not menu_item_id or not name or not restaurant_id or _required_addons(item):
            return None
        groups = _list(item.get("variantsV2"), "variantsV2", allow_missing=True)
        legacy = _list(item.get("variations"), "variations", allow_missing=True)
        if len(groups) > 1:
            return None  # several independent choices cannot be spoken as one short list
        base_price = _optional_money(item.get("price")) or Decimal(0)
        stocked = item.get("inStock") != 0
        variants: list[Variant] = []

        def add(label: str, price: Decimal, in_stock: bool, selection: dict[str, object]) -> None:
            variation_id = label if not selection else str(selection["id"])
            sku_id = f"{menu_item_id}:{variation_id}"
            self._dishes[sku_id] = _Dish(
                restaurant_id,
                restaurant,
                menu_item_id,
                label,
                {k: v for k, v in selection.items() if k != "id"},
            )
            variants.append(
                Variant(
                    spin_id=menu_item_id,
                    sku_id=sku_id,
                    pack_size=label,
                    price_inr=price,
                    in_stock=in_stock,
                )
            )

        if groups:
            group_id = groups[0].get("groupId")
            for option in _list(groups[0].get("variations"), "variations", allow_missing=True):
                add(
                    str(option.get("name") or ""),
                    _optional_money(option.get("price")) or base_price,
                    stocked and option.get("inStock") != 0,
                    {
                        "id": option.get("id"),
                        "variantsV2": [{"group_id": group_id, "variation_id": option.get("id")}],
                    },
                )
        elif legacy:
            for option in legacy:
                add(
                    str(option.get("name") or ""),
                    _optional_money(option.get("price")) or base_price,
                    stocked and option.get("inStock") != 0,
                    {
                        "id": option.get("id"),
                        "variants": [
                            {"group_id": option.get("groupId"), "variation_id": option.get("id")}
                        ],
                    },
                )
        else:
            add("", base_price, stocked, {})
        return Product(
            id=f"{restaurant_id}:{menu_item_id}",
            display_name=f"{name} from {restaurant}",
            brand=restaurant,
            variants=variants,
        )

    async def go_to_items(self, address_id: str) -> list[Product]:
        del address_id
        return []  # Food has no "usual items" tool

    async def update_cart(self, address_id: str, items: list[CartLine]) -> Cart:
        if not self.allow_cart_writes:
            raise CommerceWriteDisabled("Changing the real Swiggy cart is disabled")
        if not items:
            await self._call("flush_food_cart", {})
            cart = await self.get_cart()
            if cart.items:
                raise CommerceError("Swiggy Food cart was not empty after flush")
            return cart
        dishes: list[tuple[_Dish, int]] = []
        for line in items:
            dish = self._dishes.get(line.sku_id)
            if dish is None:
                # Rewriting a line we can't reproduce exactly could drop its customizations.
                raise CommerceNotAttempted("Cannot safely rewrite this Food cart line")
            dishes.append((dish, line.quantity))
        restaurants = {dish.restaurant_id for dish, _ in dishes}
        if len(restaurants) != 1:
            raise CommerceNotAttempted("A Food cart holds one restaurant")
        first = dishes[0][0]
        self._restaurant_id = first.restaurant_id
        self._restaurant_name = first.restaurant_name
        await self._call(
            "update_food_cart",
            {
                "addressId": address_id,
                "restaurantId": first.restaurant_id,
                "restaurantName": first.restaurant_name,
                "cartItems": [
                    {"menu_item_id": dish.menu_item_id, "quantity": quantity, **dish.selection}
                    for dish, quantity in dishes
                ],
            },
        )
        return await self.get_cart()

    async def get_cart(self) -> Cart:
        arguments: dict[str, object] = {"addressId": self.address_id}
        if self._restaurant_name:
            arguments["restaurantName"] = self._restaurant_name
        return self._to_cart(await self._call("get_food_cart", arguments))

    def _to_cart(self, data: Mapping[str, object]) -> Cart:
        # Live and documented nesting differ, so accept the cart at either level.
        inner = _mapping(data.get("data")) or data
        raw_items = _list(inner.get("items"), "items", allow_missing=True)
        restaurant = _mapping(inner.get("restaurant"))
        if not raw_items:
            self._restaurant_id = None
            self._restaurant_name = ""
            return Cart(
                address_id=self.address_id,
                items=[],
                item_total_inr=Decimal(0),
                delivery_fee_inr=Decimal(0),
                grand_total_inr=Decimal(0),
                payment_methods=[],
                lat=0.0,
                lng=0.0,
            )
        self._restaurant_id = _optional_str(restaurant.get("id")) or self._restaurant_id
        self._restaurant_name = _optional_str(restaurant.get("name")) or self._restaurant_name
        pricing = _mapping(inner.get("pricing"))
        items = [self._cart_item(raw) for raw in raw_items]
        item_total = _optional_money(pricing.get("item_total")) or sum(
            (item.line_total_inr for item in items), Decimal(0)
        )
        fees = (_optional_money(pricing.get("delivery_charge")) or Decimal(0)) + (
            _optional_money(pricing.get("taxes_and_charges")) or Decimal(0)
        )
        discount = _optional_money(_mapping(inner.get("offers")).get("coupon_discount"))
        issues = (
            [CartIssue.OUT_OF_STOCK]
            if any(raw.get("in_stock") is False for raw in raw_items)
            else []
        )
        return Cart(
            address_id=self.address_id,
            items=items,
            item_total_inr=item_total,
            delivery_fee_inr=fees,
            savings_inr=discount or Decimal(0),  # docs: a 0 discount is only a suggested coupon
            grand_total_inr=_money(pricing.get("to_pay"), "pricing.to_pay"),
            payment_methods=[],
            issues=issues,
            lat=0.0,  # track_food_order needs no coordinates
            lng=0.0,
        )

    def _cart_item(self, raw: Mapping[str, object]) -> CartItem:
        menu_item_id = str(raw.get("menu_item_id") or "")
        quantity = _optional_int(raw.get("quantity")) or 1
        variants = _list(raw.get("variants"), "variants", allow_missing=True)
        addons = _list(raw.get("addons"), "addons", allow_missing=True)
        names = [str(v.get("name") or "") for v in variants] + [
            f"with {a.get('name')}" for a in addons
        ]
        label = ", ".join(name for name in names if name)
        # A line with add-ons never matches a known dish, so it can't be rewritten without them.
        sku_id = next(
            (
                sku
                for sku, dish in self._dishes.items()
                if dish.menu_item_id == menu_item_id and dish.label == label and not addons
            ),
            f"{menu_item_id}:cart:{label}",
        )
        total = _optional_money(raw.get("total"))
        unit = total / quantity if total is not None else _money(raw.get("final_price"), "price")
        restaurant = f" from {self._restaurant_name}" if self._restaurant_name else ""
        return CartItem(
            spin_id=menu_item_id,
            sku_id=sku_id,
            name=f"{raw.get('name')}{restaurant}",
            pack_size=label,
            quantity=quantity,
            unit_price_inr=unit,
        )

    async def get_payment_options(self) -> PaymentOptions:
        data = await self._call("get_payment_options", {"addressId": self.address_id})
        cod = _mapping(data.get("cod"))
        self._cod_available = cod.get("available") is True
        return PaymentOptions(
            cod_available=self._cod_available, cod_id=_optional_str(cod.get("id"))
        )

    async def checkout(
        self,
        address_id: str,
        payment_method: PaymentMethod = PaymentMethod.COD,
        confirm_duplicate: bool = False,
    ) -> OrderResult:
        if not self.allow_real_checkout:
            raise CommerceNotAttempted("Real Swiggy Food ordering is disabled by configuration")
        if confirm_duplicate or payment_method is not PaymentMethod.COD:
            raise CommerceNotAttempted("Only a first cash-on-delivery attempt is supported")
        if not self._cod_available:
            raise CommerceNotAttempted("Cash on delivery is not available for this cart")
        data, message = await self._call_with_message(
            "place_food_order", {"addressId": address_id, "paymentMethod": "Cash"}
        )
        status = _optional_str(data.get("status")) or ""
        pending = status.upper() == "PENDING_PAYMENT" or data.get("normalizedStatus") == "pending"
        order_id = _optional_str(data.get("orderId"))
        return OrderResult(
            success=bool(order_id) and not pending,
            order_ids=[order_id] if order_id else [],
            message=message,
            status=status,
            total_inr=_optional_money(data.get("totalAmount")),
        )

    async def get_orders(self, count: int = 10) -> list[OrderSummary]:
        data = await self._call(
            "get_food_orders", {"addressId": self.address_id, "orderCount": min(count, 15)}
        )
        orders: list[OrderSummary] = []
        for raw in _list(data.get("orders"), "orders", allow_missing=True):
            order_id = _optional_str(raw.get("orderId"))
            if order_id is None:
                raise CommerceError("Missing orderId in Swiggy Food response")
            orders.append(
                OrderSummary(
                    order_id=order_id,
                    items=[OrderSummaryItem(name=str(raw.get("orderedItems") or ""), quantity=1)],
                    total_inr=_optional_money(raw.get("orderTotal")) or Decimal(0),
                    status=str(raw.get("orderStatus") or ""),
                )
            )
        return orders

    async def track_order(self, order_id: str, lat: float, lng: float) -> OrderTracking:
        del lat, lng
        data = await self._call("track_food_order", {"orderId": order_id})
        for raw in _list(data.get("orders"), "orders", allow_missing=True):
            if raw.get("orderId") == order_id:
                return OrderTracking(
                    order_id=order_id,
                    status_message=str(raw.get("orderStatus") or raw.get("title") or ""),
                    eta_text=_optional_str(raw.get("etaText")),
                )
        raise CommerceError("Order not found in Swiggy Food tracking")
