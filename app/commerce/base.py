"""Commerce Adapter interface (DESIGN.md §8).

Models mirror the Swiggy Instamart MCP tool shapes recorded in
docs/swiggy-integration-notes.md (variants carry `spinId` + `skuId`;
`update_cart` replaces the whole cart; `checkout` requires `addressId`). Swiggy
JSON must not leak past this package: adapters translate into these models.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class PaymentMethod(StrEnum):
    COD = "COD"  # Swiggy also accepts "Cash"
    UPI = "UPI"
    SWIGGY_PAY = "SwiggyPay"


class CartIssue(StrEnum):
    """A live cart condition that blocks confirmation until it is resolved."""

    MIN_ORDER = "min_order"
    OUT_OF_STOCK = "out_of_stock"
    NOT_SERVICEABLE = "not_serviceable"
    ADDRESS_MISMATCH = "address_mismatch"
    PAYMENT_UNAVAILABLE = "payment_unavailable"
    OTHER = "other"


class RejectionKind(StrEnum):
    """Why the provider refused an operation. Only `is_definitive` kinds prove no order exists."""

    MIN_ORDER = "min_order"
    OUT_OF_STOCK = "out_of_stock"
    NOT_SERVICEABLE = "not_serviceable"
    CART_EXPIRED = "cart_expired"
    PAYMENT = "payment"
    DUPLICATE = "duplicate"
    NOT_PERMITTED = "not_permitted"
    OTHER = "other"

    @property
    def is_definitive(self) -> bool:
        return self not in {RejectionKind.DUPLICATE, RejectionKind.OTHER}


def same_address_id(first: str | None, second: str | None) -> bool:
    """Whether two address IDs name the same address.

    Observed live: get_addresses returns "<id>__<token>" while the cart's
    selectedAddressDetails.id is the bare "<id>". Always pass the full ID from
    get_addresses back to Swiggy; only comparisons use this.
    """
    if not first or not second:
        return False
    return first.split("__", maxsplit=1)[0] == second.split("__", maxsplit=1)[0]


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
    pack_size: str  # human label, e.g. "1 kg", "500 ml x 4"
    quantity_value: float | None = None  # total numeric size; None when it cannot be parsed
    unit: str | None = None  # "g" | "ml" | "pc"; None when it cannot be parsed
    price_inr: Decimal
    in_stock: bool = True
    max_quantity: int | None = None


class Product(BaseModel):
    id: str
    display_name: str
    brand: str
    variants: list[Variant]
    promoted: bool = False


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
    unit_price_inr: Decimal

    @property
    def line_total_inr(self) -> Decimal:
        return self.unit_price_inr * self.quantity


class Cart(BaseModel):
    address_id: str | None
    delivery_label: str | None = None
    delivery_address: str | None = None
    items: list[CartItem]
    item_total_inr: Decimal
    delivery_fee_inr: Decimal  # every charge beyond the items: delivery, handling, other fees
    savings_inr: Decimal = Decimal(0)  # discounts shown on the bill
    grand_total_inr: Decimal
    payment_methods: list[PaymentMethod]
    errors: list[str] = Field(default_factory=lambda: list[str]())  # e.g. "Minimum order not met"
    issues: list[CartIssue] = Field(default_factory=lambda: list[CartIssue]())
    store_count: int = 1
    lat: float | None = None
    lng: float | None = None


class PaymentOptions(BaseModel):
    """Live payment facts for the current cart. Ajji only offers cash on delivery."""

    cod_available: bool
    cod_id: str | None = None


class OrderResult(BaseModel):
    success: bool
    order_ids: list[str] = Field(default_factory=lambda: list[str]())  # multi-store => several
    message: str = ""
    probable_duplicate: bool = False  # Swiggy rejection hinting confirmDuplicate
    partial: bool = False  # multi-store: at least one order placed, at least one failed
    status: str = ""
    total_inr: Decimal | None = None


class OrderSummaryItem(BaseModel):
    name: str
    quantity: int
    item_id: str | None = None


class OrderSummary(BaseModel):
    order_id: str
    created_at: datetime | None = None
    items: list[OrderSummaryItem]
    total_inr: Decimal
    status: str
    delivery_address_id: str | None = None


class OrderTracking(BaseModel):
    order_id: str
    status_message: str
    eta_text: str | None = None
    eta_minutes: int | None = None


class CommerceError(Exception):
    """A commerce operation failed. Its outcome may be unknown unless a subclass says otherwise."""


class CommerceAuthRequired(CommerceError):
    """The account login is missing or expired. Nothing reached the provider."""


class CommerceNotAttempted(CommerceError):
    """A local guard refused the operation before anything was sent to the provider."""


class CommerceWriteDisabled(CommerceNotAttempted):
    """Configuration forbids mutating the real cart."""


class CommerceRejected(CommerceError):
    """The provider answered and refused the operation."""

    def __init__(
        self,
        message: str,
        *,
        kind: RejectionKind = RejectionKind.OTHER,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.provider_message = ""  # Swiggy's own reason; kept out of str() and logs


def classify_rejection(message: str) -> RejectionKind | None:
    """Map a provider message to a kind; None means a transient failure, not a refusal.

    The provider has no symbolic error code: classification by message text is
    the documented contract (https://mcp.swiggy.com/builders/docs/reference/errors.md).
    """
    text = message.casefold().replace("_", " ")
    if any(word in text for word in ("timeout", "timed out", "internal", "temporar", "gateway")):
        return None
    if "duplicate" in text:
        return RejectionKind.DUPLICATE
    if any(
        phrase in text
        for phrase in ("not permitted", "not allowed", "not eligible", "not whitelisted")
    ):
        return RejectionKind.NOT_PERMITTED
    if "minimum order" in text or "min order" in text:
        return RejectionKind.MIN_ORDER
    if "out of stock" in text or "unavailable" in text or "sold out" in text:
        return RejectionKind.OUT_OF_STOCK
    if "serviceable" in text:
        return RejectionKind.NOT_SERVICEABLE
    if "cart expired" in text or "abandoned" in text or "cart is empty" in text:
        return RejectionKind.CART_EXPIRED
    if "payment" in text or "balance" in text:
        return RejectionKind.PAYMENT
    return RejectionKind.OTHER


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

    async def get_payment_options(self) -> PaymentOptions: ...

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
