"""MockInstamart: in-memory CommerceAdapter for dev and tests (DESIGN.md §8).

Behaviour copied from Swiggy docs: ₹99 minimum order, full-replace carts,
out-of-stock variants, non-idempotent checkout with probable-duplicate
detection (honours `confirm_duplicate`), plus failure injection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.commerce.base import (
    Address,
    Cart,
    CartIssue,
    CartItem,
    CartLine,
    CommerceError,
    CommerceRejected,
    OrderResult,
    OrderSummary,
    OrderSummaryItem,
    OrderTracking,
    PaymentMethod,
    PaymentOptions,
    Product,
    RejectionKind,
    SearchResult,
    Variant,
)
from app.commerce.vocabulary import catalogue_term

MIN_ORDER_INR = 99
DELIVERY_FEE_INR = 25
DUPLICATE_WINDOW = timedelta(minutes=30)


def _v(spin: str, size: str, qty: float, unit: str, price: int, stock: bool = True) -> Variant:
    return Variant(
        spin_id=spin,
        sku_id=f"sku_{spin}",
        pack_size=size,
        quantity_value=qty,
        unit=unit,
        price_inr=Decimal(price),
        in_stock=stock,
    )


def _p(pid: str, name: str, brand: str, *variants: Variant) -> Product:
    return Product(id=pid, display_name=name, brand=brand, variants=list(variants))


CATALOG: list[Product] = [
    _p(
        "p_atta",
        "Aashirvaad Whole Wheat Atta",
        "Aashirvaad",
        _v("atta_1kg", "1 kg", 1000, "g", 68),
        _v("atta_5kg", "5 kg", 5000, "g", 299),
    ),
    _p("p_onion", "Onion", "Fresho", _v("onion_1kg", "1 kg", 1000, "g", 45)),
    _p(
        "p_tomato",
        "Tomato",
        "Fresho",
        _v("tomato_500g", "500 g", 500, "g", 22),
        _v("tomato_1kg", "1 kg", 1000, "g", 40),
    ),
    _p("p_potato", "Potato", "Fresho", _v("potato_1kg", "1 kg", 1000, "g", 38)),
    _p("p_milk", "Nandini Toned Milk", "Nandini", _v("milk_500ml", "500 ml", 500, "ml", 26)),
    _p(
        "p_curd",
        "Nandini Curd",
        "Nandini",
        _v("curd_500g", "500 g", 500, "g", 30),
        _v("curd_1kg", "1 kg", 1000, "g", 55),
    ),
    _p("p_sugar", "Madhur Sugar", "Madhur", _v("sugar_1kg", "1 kg", 1000, "g", 52)),
    _p(
        "p_rice",
        "Sona Masoori Rice",
        "India Gate",
        _v("rice_1kg", "1 kg", 1000, "g", 78),
        _v("rice_5kg", "5 kg", 5000, "g", 365),
        _v("rice_10kg", "10 kg", 10000, "g", 699),
    ),
    _p(
        "p_toor",
        "Toor Dal",
        "Tata Sampann",
        _v("toor_500g", "500 g", 500, "g", 92),
        _v("toor_1kg", "1 kg", 1000, "g", 175),
    ),
    _p(
        "p_oil",
        "Sunflower Oil",
        "Fortune",
        _v("oil_1l", "1 L", 1000, "ml", 155),
        _v("oil_5l", "5 L", 5000, "ml", 720),
    ),
    _p("p_salt", "Iodised Salt", "Tata", _v("salt_1kg", "1 kg", 1000, "g", 28)),
    _p(
        "p_tea",
        "Red Label Tea",
        "Brooke Bond",
        _v("tea_250g", "250 g", 250, "g", 140),
        _v("tea_500g", "500 g", 500, "g", 275),
    ),
    _p("p_coffee", "Filter Coffee Powder", "Cothas", _v("coffee_500g", "500 g", 500, "g", 260)),
    _p("p_banana", "Banana Robusta", "Fresho", _v("banana_6", "6 pcs", 6, "pc", 42)),
    _p("p_bread", "Milk Bread", "Modern", _v("bread_400g", "400 g", 400, "g", 45)),
    _p(
        "p_eggs",
        "Farm Eggs",
        "Eggoz",
        _v("eggs_6", "6 pcs", 6, "pc", 60),
        _v("eggs_12", "12 pcs", 12, "pc", 115),
    ),
    _p("p_ghee", "Nandini Ghee", "Nandini", _v("ghee_500ml", "500 ml", 500, "ml", 330)),
    _p("p_ragi", "Ragi Flour", "Pro Nature", _v("ragi_1kg", "1 kg", 1000, "g", 85)),
    _p(
        "p_coriander",
        "Coriander Leaves",
        "Fresho",
        _v("coriander_100g", "100 g", 100, "g", 15, stock=False),
    ),  # out of stock
    _p(
        "p_soap",
        "Mysore Sandal Soap",
        "Mysore Sandal",
        _v("soap_3x150g", "3 x 150 g", 3, "pc", 210),
    ),
    _p("p_jaggery", "Jaggery", "24 Mantra", _v("jaggery_1kg", "1 kg", 1000, "g", 90)),
]


class MockInstamart:
    """Implements CommerceAdapter. Single-user, in-memory."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self.addresses = [
            Address(
                id="addr_home",
                label="Home",
                full_address="12, 4th Cross, Jayanagar, Bengaluru 560041",
                lat=12.925,
                lng=77.593,
            ),
        ]
        self._cart: list[CartItem] = []
        self._cart_address: str | None = None
        self.orders: list[OrderSummary] = []
        self.checkout_calls = 0
        self._order_seq = 0
        self._now = now
        # failure injection
        self.fail_next_checkout_after_success = False  # order placed, then raise TimeoutError
        self.fail_next_checkout_before = False  # raise TimeoutError, nothing placed
        self.unserviceable_address_ids: set[str] = set()
        self.cod_available = True

    # helpers
    def _clock(self) -> datetime:
        return self._now or datetime.now(UTC)

    def _variant(self, spin_id: str) -> tuple[Product, Variant]:
        for p in CATALOG:
            for v in p.variants:
                if v.spin_id == spin_id:
                    return p, v
        raise CommerceError(f"Invalid spinId: {spin_id}")

    def _check_address(self, address_id: str) -> None:
        if address_id not in {a.id for a in self.addresses}:
            raise CommerceError(f"Invalid addressId: {address_id}")
        if address_id in self.unserviceable_address_ids:
            raise CommerceRejected("Address not serviceable", kind=RejectionKind.NOT_SERVICEABLE)

    def _build_cart(self) -> Cart:
        total = sum((i.line_total_inr for i in self._cart), Decimal(0))
        errors: list[str] = []
        issues: list[CartIssue] = []
        if self._cart and total < MIN_ORDER_INR:
            errors.append(f"Minimum order not met: add items worth ₹{MIN_ORDER_INR - total} more")
            issues.append(CartIssue.MIN_ORDER)
        fee = Decimal(DELIVERY_FEE_INR if self._cart else 0)
        return Cart(
            address_id=self._cart_address,
            delivery_label=next(
                (address.label for address in self.addresses if address.id == self._cart_address),
                None,
            ),
            delivery_address=next(
                (
                    address.full_address
                    for address in self.addresses
                    if address.id == self._cart_address
                ),
                None,
            ),
            items=list(self._cart),
            item_total_inr=total,
            delivery_fee_inr=fee,
            grand_total_inr=total + fee,
            payment_methods=[PaymentMethod.COD, PaymentMethod.UPI],
            errors=errors,
            issues=issues,
            lat=next((a.lat for a in self.addresses if a.id == self._cart_address), None),
            lng=next((a.lng for a in self.addresses if a.id == self._cart_address), None),
        )

    # CommerceAdapter
    async def get_addresses(self) -> list[Address]:
        return list(self.addresses)

    async def search_products(self, address_id: str, query: str, offset: int = 0) -> SearchResult:
        self._check_address(address_id)
        q = query.strip().lower()
        if not q:
            raise CommerceError("Missing query")
        q = catalogue_term(q).casefold()
        hits = [
            p for p in CATALOG if q in p.display_name.lower() or q in p.brand.lower() or q in p.id
        ]
        similar: list[Product] = []
        if hits and not any(v.in_stock for p in hits for v in p.variants):
            similar = [p for p in CATALOG if p.id in {"p_ragi", "p_jaggery"}][:2]
        return SearchResult(products=hits[offset:], similar_products=similar)

    async def go_to_items(self, address_id: str) -> list[Product]:
        self._check_address(address_id)
        ids = {"p_milk", "p_curd", "p_bread", "p_banana"}
        return [p for p in CATALOG if p.id in ids]

    async def update_cart(self, address_id: str, items: list[CartLine]) -> Cart:
        self._check_address(address_id)
        new: list[CartItem] = []
        for line in items:
            p, v = self._variant(line.spin_id)
            if line.sku_id != v.sku_id:
                raise CommerceError(f"Invalid skuId for spinId {line.spin_id}")
            if not v.in_stock:
                raise CommerceError(f"Item out of stock: {p.display_name} {v.pack_size}")
            new.append(
                CartItem(
                    spin_id=v.spin_id,
                    sku_id=v.sku_id,
                    name=p.display_name,
                    pack_size=v.pack_size,
                    quantity=line.quantity,
                    unit_price_inr=v.price_inr,
                )
            )
        self._cart = new  # full replace
        self._cart_address = address_id
        return self._build_cart()

    async def add_to_cart(self, address_id: str, items: list[CartLine]) -> Cart:
        """Add quantities to the current mock cart by SKU."""
        current = [
            CartLine(spin_id=item.spin_id, sku_id=item.sku_id, quantity=item.quantity)
            for item in self._cart
        ]
        quantities = {item.sku_id: item.quantity for item in current}
        for item in items:
            quantities[item.sku_id] = quantities.get(item.sku_id, 0) + item.quantity
        merged = [
            CartLine(
                spin_id=item.spin_id,
                sku_id=item.sku_id,
                quantity=quantities[item.sku_id],
            )
            for item in current
        ]
        known = {item.sku_id for item in current}
        merged.extend(item for item in items if item.sku_id not in known)
        return await self.update_cart(address_id, merged)

    async def remove_from_cart(self, address_id: str, sku_ids: set[str]) -> Cart:
        """Remove complete SKU lines from the current mock cart."""
        items = [
            CartLine(spin_id=item.spin_id, sku_id=item.sku_id, quantity=item.quantity)
            for item in self._cart
            if item.sku_id not in sku_ids
        ]
        return await self.update_cart(address_id, items)

    async def change_quantity(self, address_id: str, sku_id: str, quantity: int) -> Cart:
        """Set one existing SKU quantity without adding an unknown item."""
        if quantity <= 0:
            raise CommerceError("Quantity must be positive")
        found = False
        items: list[CartLine] = []
        for item in self._cart:
            if item.sku_id == sku_id:
                found = True
                items.append(CartLine(spin_id=item.spin_id, sku_id=sku_id, quantity=quantity))
            else:
                items.append(
                    CartLine(spin_id=item.spin_id, sku_id=item.sku_id, quantity=item.quantity)
                )
        if not found:
            raise CommerceError(f"SKU not in cart: {sku_id}")
        return await self.update_cart(address_id, items)

    async def get_cart(self) -> Cart:
        return self._build_cart()

    async def get_payment_options(self) -> PaymentOptions:
        return PaymentOptions(cod_available=self.cod_available, cod_id="COD")

    async def checkout(
        self,
        address_id: str,
        payment_method: PaymentMethod = PaymentMethod.COD,
        confirm_duplicate: bool = False,
    ) -> OrderResult:
        self.checkout_calls += 1
        self._check_address(address_id)
        if self.fail_next_checkout_before:
            self.fail_next_checkout_before = False
            raise TimeoutError("upstream timeout")
        cart = self._build_cart()
        if not cart.items:
            raise CommerceRejected("Cart is empty", kind=RejectionKind.CART_EXPIRED)
        if cart.errors:
            raise CommerceRejected(cart.errors[0], kind=RejectionKind.MIN_ORDER)
        lines = [
            CartLine(spin_id=i.spin_id, sku_id=i.sku_id, quantity=i.quantity) for i in cart.items
        ]
        key = sorted((ln.spin_id, ln.quantity) for ln in lines)
        now = self._clock()
        dup = any(
            sorted((ln.item_id, ln.quantity) for ln in o.items) == key
            and o.created_at is not None
            and now - o.created_at < DUPLICATE_WINDOW
            for o in self.orders
        )
        if dup and not confirm_duplicate:
            return OrderResult(
                success=False,
                probable_duplicate=True,
                message="Probable duplicate order. Re-checkout with "
                "confirmDuplicate only if the user confirms.",
            )
        self._order_seq += 1
        oid = f"IM{self._order_seq:06d}"
        self.orders.append(
            OrderSummary(
                order_id=oid,
                created_at=now,
                items=[
                    OrderSummaryItem(name=item.name, quantity=item.quantity, item_id=item.spin_id)
                    for item in cart.items
                ],
                total_inr=cart.grand_total_inr,
                status="PLACED",
                delivery_address_id=cart.address_id,
            )
        )
        self._cart = []
        if self.fail_next_checkout_after_success:
            self.fail_next_checkout_after_success = False
            raise TimeoutError("upstream timeout (order was actually placed)")
        return OrderResult(
            success=True,
            order_ids=[oid],
            message="Instamart order placed successfully",
            status="PLACED",
            total_inr=cart.grand_total_inr,
        )

    async def get_orders(self, count: int = 10) -> list[OrderSummary]:
        return list(reversed(self.orders))[:count]

    async def track_order(self, order_id: str, lat: float, lng: float) -> OrderTracking:
        del lat, lng
        for order in self.orders:
            if order.order_id == order_id:
                return OrderTracking(
                    order_id=order_id,
                    status_message=order.status,
                    eta_text=None,
                    eta_minutes=None,
                )
        raise CommerceError(f"Unknown order: {order_id}")
