"""Commerce Adapter interface (DESIGN.md §8).

Models mirror the Swiggy Instamart MCP tool shapes documented in
swiggy-docs-notes.md §3 (variants carry `spinId` + `skuId`; `update_cart`
replaces the whole cart; `checkout` requires `addressId`). Swiggy JSON must not
leak past this package: adapters translate into these models.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class PaymentMethod(StrEnum):
    COD = "COD"  # Swiggy also accepts "Cash"
    UPI = "UPI"
    SWIGGY_PAY = "SwiggyPay"


class Address(BaseModel):
    id: str  # Swiggy addressId
    label: str  # e.g. "Home"
    full_address: str
    lat: float | None = None
    lng: float | None = None
    is_default: bool = False


class Variant(BaseModel):
    spin_id: str  # Swiggy spinId (SKU-level id)
    sku_id: str  # Swiggy skuId
    pack_size: str  # human label, e.g. "1 kg", "500 ml"
    quantity_value: float | None = None  # numeric size when the adapter provides it
    unit: str | None = None  # "g" | "ml" | "pc" when the adapter provides it
    price_inr: int
    in_stock: bool = True


class Product(BaseModel):
    id: str
    display_name: str
    brand: str
    variants: list[Variant]


class SearchResult(BaseModel):
    products: list[Product]
    similar_products: list[Product] = Field(default_factory=lambda: list[Product]())
    next_offset: int | None = None


class CartLine(BaseModel):
    spin_id: str
    sku_id: str
    quantity: int = Field(gt=0)


class CartItem(BaseModel):
    spin_id: str
    sku_id: str
    name: str
    pack_size: str
    quantity: int
    unit_price_inr: int

    @property
    def line_total_inr(self) -> int:
        return self.unit_price_inr * self.quantity


class Cart(BaseModel):
    address_id: str | None
    delivery_label: str | None = None
    delivery_address: str | None = None
    items: list[CartItem]
    item_total_inr: int
    delivery_fee_inr: int
    grand_total_inr: int
    payment_methods: list[PaymentMethod]
    errors: list[str] = Field(default_factory=lambda: list[str]())  # e.g. "Minimum order not met"


class OrderResult(BaseModel):
    success: bool
    order_ids: list[str] = Field(default_factory=lambda: list[str]())  # multi-store => several
    message: str = ""
    probable_duplicate: bool = False  # Swiggy rejection hinting confirmDuplicate


class OrderSummaryItem(BaseModel):
    name: str
    quantity: int
    item_id: str | None = None


class OrderSummary(BaseModel):
    order_id: str
    created_at: datetime | None = None
    items: list[OrderSummaryItem]
    total_inr: int
    status: str
    delivery_address_id: str | None = None


class OrderTracking(BaseModel):
    order_id: str
    status_message: str
    eta_text: str | None = None
    eta_minutes: int | None = None


class CommerceError(Exception):
    """Domain failure (Swiggy `success:false`). Terminal: surface, don't retry."""


class CommerceAdapter(Protocol):
    async def get_addresses(self) -> list[Address]: ...

    async def search_products(
        self, address_id: str, query: str, offset: int = 0
    ) -> SearchResult: ...

    async def go_to_items(self, address_id: str) -> list[Product]:
        """Swiggy `your_go_to_items` — "same as usual"."""
        ...

    async def update_cart(self, address_id: str, items: list[CartLine]) -> Cart:
        """Full replace (Swiggy semantics). Safe to retry."""
        ...

    async def get_cart(self) -> Cart: ...

    async def checkout(
        self,
        address_id: str,
        payment_method: PaymentMethod = PaymentMethod.COD,
        confirm_duplicate: bool = False,
    ) -> OrderResult:
        """NOT idempotent. Never blind-retry; reconcile with get_orders."""
        ...

    async def get_orders(self, count: int = 10) -> list[OrderSummary]: ...

    async def track_order(self, order_id: str, lat: float, lng: float) -> OrderTracking: ...
