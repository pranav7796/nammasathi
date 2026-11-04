"""Turn one requested grocery item plus search results into a cart line or one question. Pure.

The rule is that nothing incompatible is ever chosen silently. A line is chosen
without asking only when exactly one in-stock variant, from a product whose name
matches the request, delivers exactly the requested amount. Everything else is
spoken as up to three options (DESIGN.md §7, and the voice guidance at
https://mcp.swiggy.com/builders/docs/build/agent-patterns/voice-vs-chat.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.commerce.base import Product, SearchResult, Variant
from app.commerce.units import (
    Amount,
    is_pack_count_unit,
    parse_pack_size,
    requested_amount,
)
from app.orchestrator.state_machine import (
    CartEntry,
    Clarification,
    ClarificationKind,
    RequestedItem,
)

MAX_OPTIONS = 3
MAX_SUBSTITUTES = 2


@dataclass(frozen=True)
class Resolution:
    entry: CartEntry | None = None
    auto_resolved: bool = False  # chosen by pack arithmetic, must be spoken
    clarification: Clarification | None = None


def _entry(product: Product, variant: Variant, quantity: int) -> CartEntry:
    return CartEntry(
        spin_id=variant.spin_id,
        sku_id=variant.sku_id,
        name=product.display_name,
        pack_size=variant.pack_size,
        quantity=quantity,
        unit_price_inr=variant.price_inr,
    )


def variant_amount(variant: Variant) -> Amount | None:
    if variant.quantity_value is not None and variant.unit in {"g", "ml", "pc"}:
        return Amount(Decimal(str(variant.quantity_value)), variant.unit)
    return parse_pack_size(variant.pack_size)


def _ask(
    requested: RequestedItem,
    kind: ClarificationKind,
    question: str,
    options: tuple[CartEntry, ...] = (),
) -> Resolution:
    return Resolution(
        clarification=Clarification(kind, question, options[:MAX_OPTIONS], requested=requested)
    )


def _relevant_products(
    requested: RequestedItem, products: list[Product]
) -> tuple[list[Product], bool]:
    """Keep products named like the request; (products, whether the match can be trusted)."""
    term = (requested.search_term or requested.name).casefold()
    words = term.split()
    if not words or not term.isascii():
        return products, False  # a native-script name cannot be compared with English catalogues
    matches = [
        product
        for product in products
        if all(word in f"{product.display_name} {product.brand}".casefold() for word in words)
    ]
    return (matches, True) if matches else (products, False)


def _whole_packs(target: Decimal, pack: Decimal) -> int | None:
    if pack <= 0 or target < pack or target % pack != 0:
        return None
    return int(target / pack)


def _fits_limit(variant: Variant, quantity: int) -> bool:
    return variant.max_quantity is None or quantity <= variant.max_quantity


def _organic_first(products: list[Product]) -> list[Product]:
    return sorted(products, key=lambda product: product.promoted)


def _substitutes(result: SearchResult) -> tuple[CartEntry, ...]:
    options = [
        _entry(product, variant, 1)
        for product in result.similar_products
        for variant in product.variants[:1]
        if variant.in_stock
    ]
    return tuple(options[:MAX_SUBSTITUTES])


def _exact_or_multiple(
    target: Amount, products: list[Product]
) -> tuple[list[CartEntry], list[CartEntry]]:
    """At most one candidate per product: the exact pack, else the fewest whole packs."""
    exact: list[CartEntry] = []
    multiples: list[CartEntry] = []
    for product in products:
        best: tuple[int, Variant] | None = None
        for variant in product.variants:
            amount = variant_amount(variant)
            if not variant.in_stock or amount is None or amount.measure != target.measure:
                continue
            quantity = _whole_packs(target.value, amount.value)
            if quantity is None or not _fits_limit(variant, quantity):
                continue
            if best is None or quantity < best[0]:
                best = (quantity, variant)
        if best is None:
            continue
        quantity, variant = best
        (exact if quantity == 1 else multiples).append(_entry(product, variant, quantity))
    return exact, multiples


def _resolve_amount(
    requested: RequestedItem, target: Amount, products: list[Product], trusted: bool
) -> Resolution:
    exact, multiples = _exact_or_multiple(target, products)
    candidates = [*exact, *multiples]
    if len(candidates) == 1 and trusted:
        entry = candidates[0]
        return Resolution(entry=entry, auto_resolved=entry.quantity > 1)
    if candidates:
        return _ask(
            requested,
            "ambiguous_item",
            f"Which {requested.name} should I add?",
            tuple(candidates),
        )
    compatible = [
        (product, variant, amount)
        for product in products
        for variant in product.variants
        if variant.in_stock
        and (amount := variant_amount(variant)) is not None
        and amount.measure == target.measure
    ]
    bigger = sorted(
        (item for item in compatible if item[2].value > target.value),
        key=lambda item: item[2].value,
    )
    if bigger:
        return _ask(
            requested,
            "only_bigger_pack",
            f"The exact size of {requested.name} is not available. A larger pack is.",
            tuple(_entry(product, variant, 1) for product, variant, _ in bigger),
        )
    fallback = compatible or [
        (product, variant, Amount(Decimal(0), ""))
        for product in products
        for variant in product.variants
        if variant.in_stock
    ]
    return _ask(
        requested,
        "pack_size",
        f"The requested size of {requested.name} is not available.",
        tuple(_entry(product, variant, 1) for product, variant, _ in fallback),
    )


def _resolve_pack_count(
    requested: RequestedItem, products: list[Product], trusted: bool, *, known_unit: bool
) -> Resolution:
    """No physical amount was given ("milk", "3 packets of milk"): the pack must be chosen."""
    count = Decimal(str(requested.qty))
    quantity = int(count) if count == count.to_integral_value() and count >= 1 else 1
    in_stock = [
        (product, variant)
        for product in products
        for variant in product.variants
        if variant.in_stock and _fits_limit(variant, quantity)
    ]
    if len(in_stock) == 1 and trusted and known_unit:
        product, variant = in_stock[0]
        return Resolution(entry=_entry(product, variant, quantity), auto_resolved=quantity > 1)
    # Prefer one small pack from each product, so options offer brands before bulk sizes.
    distinct: list[CartEntry] = []
    seen: set[str] = set()

    def size(item: tuple[Product, Variant]) -> Decimal:
        amount = variant_amount(item[1])
        return amount.value if amount is not None else Decimal(10**9)

    for product, variant in sorted(in_stock, key=size):
        if product.id not in seen:
            seen.add(product.id)
            distinct.append(_entry(product, variant, quantity))
    if len(distinct) < MAX_OPTIONS:
        for product, variant in in_stock:
            entry = _entry(product, variant, quantity)
            if entry not in distinct:
                distinct.append(entry)
    return _ask(
        requested, "pack_size", f"Which pack of {requested.name} should I add?", tuple(distinct)
    )


def resolve_item(requested: RequestedItem, result: SearchResult) -> Resolution:
    if not result.products:
        return _ask(
            requested,
            "not_found",
            f"I could not find {requested.name}. What should I add instead?",
            _substitutes(result),
        )
    products, trusted = _relevant_products(requested, _organic_first(result.products))
    if not any(variant.in_stock for product in products for variant in product.variants):
        return _ask(
            requested,
            "out_of_stock",
            f"{requested.name} is out of stock.",
            _substitutes(result),
        )
    if requested.unit is None or is_pack_count_unit(requested.unit):
        return _resolve_pack_count(requested, products, trusted, known_unit=True)
    target = requested_amount(requested.qty, requested.unit)
    if target is None:
        return _resolve_pack_count(requested, products, trusted, known_unit=False)
    return _resolve_amount(requested, target, products, trusted)
