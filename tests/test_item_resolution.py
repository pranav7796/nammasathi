"""Product selection against live-shaped Swiggy data: no silent incompatible choice, ever."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.commerce.base import Product, SearchResult, Variant
from app.commerce.units import is_pack_count_unit, parse_pack_size, requested_amount
from app.orchestrator.item_resolution import MAX_OPTIONS, MAX_SUBSTITUTES, resolve_item
from app.orchestrator.state_machine import RequestedItem


def v(
    spin: str, pack: str, price: str = "30", *, stock: bool = True, limit: int | None = None
) -> Variant:
    amount = parse_pack_size(pack)
    return Variant(
        spin_id=spin,
        sku_id=f"sku-{spin}",
        pack_size=pack,
        quantity_value=float(amount.value) if amount else None,
        unit=amount.measure if amount else None,
        price_inr=Decimal(price),
        in_stock=stock,
        max_quantity=limit,
    )


def p(pid: str, name: str, *variants: Variant, promoted: bool = False) -> Product:
    return Product(id=pid, display_name=name, brand="", variants=list(variants), promoted=promoted)


AMUL = p("a", "Amul Gold Full Cream Milk", v("a500", "500 ml"), v("a4", "500 ml x 4", "118"))
NANDINI = p("n", "Nandini Shubham Milk", v("n1l", "1 ltr", "58"), v("n500", "500 ml", "30"))
CURD = p("c", "Nandini Curd", v("c500", "500 g", "32"))


@pytest.mark.parametrize(
    ("label", "value", "measure"),
    [
        ("500 ml", "500", "ml"),
        ("1 ltr", "1000", "ml"),
        ("500 ml x 4", "2000", "ml"),
        ("1 ltr x 2", "2000", "ml"),
        ("4 x 500 ml", "2000", "ml"),
        ("5 kg", "5000", "g"),
        ("30 g", "30", "g"),
        ("6 pcs", "6", "pc"),
        ("1.5 L", "1500", "ml"),
    ],
)
def test_live_pack_labels_parse(label: str, value: str, measure: str) -> None:
    amount = parse_pack_size(label)
    assert amount is not None
    assert amount.value == Decimal(value)
    assert amount.measure == measure


@pytest.mark.parametrize("label", ["", "Amul Gold Milk", "1 bunch", "pack of many", "x", "0 kg"])
def test_unknown_pack_labels_are_not_guessed(label: str) -> None:
    assert parse_pack_size(label) is None


def test_indic_units_are_understood() -> None:
    assert requested_amount(2, "ಕೆಜಿ") == requested_amount(2, "kg")
    assert requested_amount(1, "लीटर") == requested_amount(1, "litre")
    assert is_pack_count_unit("ಪ್ಯಾಕೆಟ್")
    assert is_pack_count_unit("पैकेट")


def result(*products: Product, similar: tuple[Product, ...] = ()) -> SearchResult:
    return SearchResult(products=list(products), similar_products=list(similar))


def milk(qty: float = 1, unit: str | None = "litre") -> RequestedItem:
    return RequestedItem("milk", qty, unit, "milk")


def test_several_brands_with_the_exact_size_ask_and_read_options() -> None:
    other = p("h", "Heritage Toned Milk", v("h1l", "1 ltr", "60"))
    outcome = resolve_item(milk(), result(NANDINI, other))
    assert outcome.entry is None
    assert outcome.clarification is not None
    assert [o.spin_id for o in outcome.clarification.options] == ["n1l", "h1l"]
    assert outcome.clarification.requested == milk()


def test_single_exact_match_is_chosen_and_read_back_later() -> None:
    outcome = resolve_item(milk(), result(NANDINI))
    assert outcome.entry is not None
    assert (outcome.entry.spin_id, outcome.entry.quantity) == ("n1l", 1)
    assert outcome.auto_resolved is False


def test_pack_arithmetic_is_only_exact_and_is_flagged_to_be_spoken() -> None:
    outcome = resolve_item(milk(), result(p("a", "Amul Milk", v("a500", "500 ml"))))
    assert outcome.entry is not None
    assert (outcome.entry.spin_id, outcome.entry.quantity) == ("a500", 2)
    assert outcome.auto_resolved is True


def test_requested_amount_is_never_rounded_up_silently() -> None:
    outcome = resolve_item(milk(0.75), result(NANDINI))
    assert outcome.entry is None
    assert outcome.clarification is not None
    assert outcome.clarification.kind == "only_bigger_pack"


def test_amount_that_packs_cannot_make_exactly_asks() -> None:
    outcome = resolve_item(milk(0.75), result(p("a", "Amul Milk", v("a500", "500 ml"))))
    assert outcome.entry is None
    assert outcome.clarification is not None
    assert outcome.clarification.kind == "pack_size"


def test_incompatible_measure_is_never_chosen() -> None:
    outcome = resolve_item(milk(), result(p("m", "Milk Powder", v("m1", "1 kg"))))
    assert outcome.entry is None
    assert outcome.clarification is not None


def test_pack_count_never_silently_picks_a_size() -> None:
    outcome = resolve_item(milk(3, "packet"), result(AMUL))
    assert outcome.entry is None, "the old code silently bought 1 x the first variant"
    assert outcome.clarification is not None
    assert {o.quantity for o in outcome.clarification.options} == {3}


def test_unknown_unit_never_resolves_even_with_a_single_variant() -> None:
    outcome = resolve_item(
        RequestedItem("milk", 2, "bunch", "milk"), result(p("a", "Amul Milk", v("a", "500 ml")))
    )
    assert outcome.entry is None
    assert outcome.clarification is not None


def test_no_unit_asks_for_the_pack() -> None:
    outcome = resolve_item(milk(1, None), result(AMUL))
    assert outcome.clarification is not None
    assert outcome.clarification.kind == "pack_size"


def test_no_unit_with_one_possible_variant_is_chosen_for_read_back() -> None:
    outcome = resolve_item(milk(2, None), result(p("a", "Amul Milk", v("a", "500 ml"))))
    assert outcome.entry is not None
    assert outcome.entry.quantity == 2


def test_unparsable_pack_never_counts_as_matching_a_size() -> None:
    weird = p("w", "Milk Special", v("w1", "family bottle"))
    outcome = resolve_item(milk(), result(weird))
    assert outcome.entry is None
    assert outcome.clarification is not None


def test_search_noise_is_filtered_by_name() -> None:
    cardamom = p("x", "Black Cardamom", v("x1", "1 kg"))
    rice = p("r", "Dosa Rice", v("r1", "1 kg", "80"))
    outcome = resolve_item(RequestedItem("akki", 1, "kg", "rice"), result(cardamom, rice))
    assert outcome.entry is not None
    assert outcome.entry.spin_id == "r1"


def test_results_unrelated_to_the_request_are_not_auto_selected() -> None:
    cardamom = p("x", "Black Cardamom", v("x1", "1 kg"))
    outcome = resolve_item(RequestedItem("akki", 1, "kg", "rice"), result(cardamom))
    assert outcome.entry is None
    assert outcome.clarification is not None


def test_native_script_request_without_a_term_is_not_auto_selected() -> None:
    outcome = resolve_item(RequestedItem("ಹಾಲು", 1, "ಲೀಟರ್"), result(NANDINI))
    assert outcome.entry is None
    assert outcome.clarification is not None


def test_out_of_stock_variants_are_never_chosen() -> None:
    gone = p("a", "Amul Milk", v("a1", "1 ltr", stock=False), v("a500", "500 ml", stock=False))
    sub = p("s", "Other Milk", v("s1", "1 ltr"))
    outcome = resolve_item(milk(), result(gone, similar=(sub,)))
    assert outcome.entry is None
    assert outcome.clarification is not None
    assert outcome.clarification.kind == "out_of_stock"
    assert [o.spin_id for o in outcome.clarification.options] == ["s1"], "offered, never added"


def test_substitute_count_is_limited() -> None:
    gone = p("a", "Amul Milk", v("a1", "1 ltr", stock=False))
    subs = tuple(p(str(i), f"Milk {i}", v(f"s{i}", "1 ltr")) for i in range(5))
    outcome = resolve_item(milk(), result(gone, similar=subs))
    assert outcome.clarification is not None
    assert len(outcome.clarification.options) == MAX_SUBSTITUTES


def test_quantity_limit_is_respected() -> None:
    limited = p("a", "Amul Milk", v("a500", "500 ml", limit=1))
    outcome = resolve_item(milk(), result(limited))
    assert outcome.entry is None


def test_nothing_found_asks_for_a_replacement() -> None:
    outcome = resolve_item(milk(), result())
    assert outcome.clarification is not None
    assert outcome.clarification.kind == "not_found"


def test_voice_lists_are_limited_and_sponsored_results_come_last() -> None:
    ad = p("ad", "Sponsored Milk", v("ad1", "1 ltr"), promoted=True)
    others = [p(str(i), f"Brand {i} Milk", v(f"b{i}", "1 ltr")) for i in range(5)]
    outcome = resolve_item(milk(), result(ad, *others))
    assert outcome.clarification is not None
    assert len(outcome.clarification.options) == MAX_OPTIONS
    assert "ad1" not in [o.spin_id for o in outcome.clarification.options]
