"""MockFood: an in-memory Swiggy Food with real Food rules, such as one restaurant per cart."""

from __future__ import annotations

from decimal import Decimal

from app.commerce.base import Cart, CartLine, CommerceRejected, Product, RejectionKind, Variant
from app.commerce.mock import MockInstamart


def _dish(item: str, name: str, restaurant: str, *sizes: tuple[str, int]) -> Product:
    return Product(
        id=f"{restaurant}:{item}",
        display_name=f"{name} from {restaurant}",
        brand=restaurant,
        variants=[
            Variant(
                spin_id=f"{item}_{index}",
                sku_id=f"sku_{item}_{index}",
                pack_size=size,
                price_inr=Decimal(price),
            )
            for index, (size, price) in enumerate(sizes)
        ],
    )


FOOD_CATALOG: list[Product] = [
    _dish("masala_dosa", "Masala Dosa", "Vidyarthi Bhavan", ("", 90)),
    _dish("idli", "Idli Vada", "Vidyarthi Bhavan", ("", 70)),
    _dish("filter_coffee", "Filter Coffee", "Vidyarthi Bhavan", ("", 40)),
    _dish("veg_biryani", "Veg Biryani", "Meghana Foods", ("", 280)),
    _dish("chicken_biryani", "Chicken Biryani", "Meghana Foods", ("half", 220), ("full", 380)),
    _dish("biryani_box", "Chicken Biryani", "Paradise", ("", 340)),
    _dish("thali", "South Indian Thali", "Mavalli Tiffin Room", ("", 210)),
    _dish("margherita", "Margherita Pizza", "Pizza Corner", ("regular", 250), ("large", 450)),
]


class MockFood(MockInstamart):
    """Implements CommerceAdapter over dishes. A dish is a product and a size is a variant."""

    catalog = FOOD_CATALOG
    min_order_inr = 0
    delivery_fee_inr = 30

    def _restaurant_of(self, spin_id: str) -> str:
        return self._variant(spin_id)[0].brand

    async def search_products(self, address_id: str, query: str, offset: int = 0):  # type: ignore[no-untyped-def]
        result = await super().search_products(address_id, query, offset)
        if self._cart:
            current = self._restaurant_of(self._cart[0].spin_id)
            result = result.model_copy(
                update={"products": [p for p in result.products if p.brand == current]}
            )
        return result

    async def update_cart(self, address_id: str, items: list[CartLine]) -> Cart:
        restaurants = {self._restaurant_of(line.spin_id) for line in items}
        if len(restaurants) > 1:
            raise CommerceRejected("A cart holds one restaurant", kind=RejectionKind.OTHER)
        return await super().update_cart(address_id, items)
