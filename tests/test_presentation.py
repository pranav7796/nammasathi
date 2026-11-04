from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

import app.orchestrator.state_machine as machine
from app.orchestrator.state_machine import CartEntry, Say
from app.voice.presentation import _PHRASES, response_text  # pyright: ignore[reportPrivateUsage]

LANGUAGES = ["kn-IN", "hi-IN", "en-IN"]
RICE = CartEntry("r", "sku-r", "Sona Masoori Rice", "1 kg", 2, 78)
MILK = CartEntry("m", "sku-m", "Amul Milk", "500 ml x 4", 1, Decimal("118.50"))


def _readback() -> Say:
    return Say(
        "readback",
        data={"total": "156", "fees": "14", "stores": "2", "payment": "COD", "address": "1 Road"},
        lines=(RICE, MILK),
    )


def test_every_language_has_every_phrase() -> None:
    assert set(_PHRASES["kn-IN"]) == set(_PHRASES["en-IN"]) == set(_PHRASES["hi-IN"])


def test_every_template_the_state_machine_can_say_exists() -> None:
    source = Path(machine.__file__).read_text()
    templates = set(re.findall(r'Say\(\s*"([a-z_]+)"', source))
    templates |= {"order_not_attempted", "order_limit", "checkout_disabled"}
    templates -= {"clarification"}  # rendered by kind
    assert templates <= set(_PHRASES["en-IN"])


@pytest.mark.parametrize("language", LANGUAGES)
def test_readback_states_items_prices_fees_stores_payment_address(language: str) -> None:
    text = response_text([_readback()], language=language, commerce_mode="swiggy")
    for fact in (
        "Sona Masoori Rice",
        "1 kg",
        "₹78",
        "₹156",
        "₹14",
        "Amul Milk",
        "₹118.50",
        "1 Road",
    ):
        assert fact in text
    assert "2" in text


@pytest.mark.parametrize("language", LANGUAGES)
def test_spoken_pack_avoids_multiplied_by(language: str) -> None:
    text = response_text([_readback()], language=language, commerce_mode="swiggy")
    assert " x " not in text, "Gnani's text normalization reads x as 'multiplied by'"


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "template", ["order_placed", "order_placed_unverified", "order_partial", "order_problem"]
)
def test_real_orders_are_never_called_demos_and_demos_never_called_real(
    language: str, template: str
) -> None:
    real = response_text([Say(template)], language=language, commerce_mode="swiggy")
    mock = response_text([Say(template)], language=language, commerce_mode="mock")
    assert not re.search(r"demo|mock|डेमो|ಡೆಮೊ", real, re.IGNORECASE)
    assert re.search(r"demo|डेमो|ಡೆಮೊ", mock, re.IGNORECASE)


def test_commerce_mode_is_mandatory() -> None:
    with pytest.raises(TypeError):
        response_text([Say("order_placed")], language="en-IN")  # type: ignore[call-arg]


@pytest.mark.parametrize("language", LANGUAGES)
def test_options_are_read_with_name_pack_and_price(language: str) -> None:
    say = Say("clarification", data={"kind": "pack_size", "item": "milk"}, lines=(RICE, MILK))
    text = response_text([say], language=language, commerce_mode="swiggy")
    assert "Sona Masoori Rice" in text
    assert "₹78" in text
    assert "Amul Milk" in text


@pytest.mark.parametrize("language", LANGUAGES)
def test_unknown_outcome_never_claims_success(language: str) -> None:
    text = response_text([Say("order_unknown")], language=language, commerce_mode="swiggy")
    assert text == _PHRASES[language]["order_unknown"]
    assert text != _PHRASES[language]["order_placed_real"]


@pytest.mark.parametrize(
    "issue",
    [
        "min_order",
        "not_serviceable",
        "payment_unavailable",
        "address_mismatch",
        "out_of_stock",
        "other",
    ],
)
def test_blocked_cart_explains_itself(issue: str) -> None:
    say = Say("cart_blocked", data={"total": "52", "issue": issue}, lines=(RICE,))
    text = response_text([say], language="en-IN", commerce_mode="swiggy")
    assert _PHRASES["en-IN"][f"issue_{issue}"] in text
    assert "Shall I" not in text
