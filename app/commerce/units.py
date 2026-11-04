"""Deterministic pack-size and unit parsing shared by adapters and item resolution.

Live Swiggy `quantityDescription` values look like "500 ml", "1 ltr", "5 kg",
"500 ml x 4" and "30 g". Anything unparsable returns None so callers must ask
the user rather than guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_UNIT_FACTORS: dict[str, tuple[str, Decimal]] = {}
for _names, _measure, _factor in (
    (("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"), "g", 1000),
    (("ಕೆಜಿ", "ಕಿಲೋ", "ಕೆ.ಜಿ", "किलो", "किलोग्राम", "केजी"), "g", 1000),
    (("g", "gm", "gms", "gram", "grams", "ಗ್ರಾಂ", "ग्राम"), "g", 1),
    (("l", "ltr", "ltrs", "lt", "litre", "liter", "litres", "liters"), "ml", 1000),
    (("ಲೀಟರ್", "ಲೀ", "लीटर"), "ml", 1000),
    (("ml", "mls", "millilitre", "milliliter", "millilitres", "milliliters"), "ml", 1),
    (("ಮಿಲಿ", "ಎಂಎಲ್", "मिली", "एमएल"), "ml", 1),
    (("pc", "pcs", "piece", "pieces", "unit", "units", "nos", "no"), "pc", 1),
    (("ಪೀಸ್", "पीस"), "pc", 1),
    (("dozen", "ಡಜನ್", "दर्जन"), "pc", 12),
):
    for _name in _names:
        _UNIT_FACTORS[_name] = (_measure, Decimal(_factor))

# A count of whole packs rather than a physical amount ("3 packets of milk").
_PACK_COUNT_UNITS = frozenset(
    {"packet", "packets", "pack", "packs", "pkt", "pkts", "ಪ್ಯಾಕೆಟ್", "ಪ್ಯಾಕ್", "पैकेट", "पैक"}
)

_MULTIPLY = r"[x×*]"
_PACK_RE = re.compile(
    rf"^(?:(\d+)\s*{_MULTIPLY}\s*)?(\d+(?:\.\d+)?)\s*([^\d\s×*]+)(?:\s*{_MULTIPLY}\s*(\d+))?$"
)


@dataclass(frozen=True)
class Amount:
    """A physical amount in a base unit: grams, millilitres, or pieces."""

    value: Decimal
    measure: str


def normalize_unit(unit: str) -> tuple[str, Decimal] | None:
    return _UNIT_FACTORS.get(unit.casefold().strip().rstrip("."))


def is_pack_count_unit(unit: str) -> bool:
    return unit.casefold().strip().rstrip(".") in _PACK_COUNT_UNITS


def requested_amount(quantity: float, unit: str) -> Amount | None:
    normalized = normalize_unit(unit)
    if normalized is None:
        return None
    measure, factor = normalized
    try:
        return Amount(Decimal(str(quantity)) * factor, measure)
    except InvalidOperation:
        return None


def parse_pack_size(text: str) -> Amount | None:
    """Parse a provider pack label into its total amount, or None when unsure."""
    match = _PACK_RE.match(text.casefold().strip())
    if match is None:
        return None
    before, value, unit, after = match.groups()
    normalized = normalize_unit(unit)
    if normalized is None:
        return None
    measure, factor = normalized
    total = Decimal(value) * factor * Decimal(before or 1) * Decimal(after or 1)
    return Amount(total, measure) if total > 0 else None
