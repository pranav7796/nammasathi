"""Deterministic understanding of plain demo requests, for mock mode only.

It keeps a demo running when every LLM is out of quota. The page says so whenever it was used,
and config refuses it outside mock mode. It handles the demo script and simple variations in
English, Hindi, and Kannada, and asks the user to repeat anything else.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from app.llm.extractor import ClarificationContext, GroceryIntent, GroceryItem

_NUMBERS = {
    "half": 0.5,
    "aadha": 0.5,
    "आधा": 0.5,
    "ಅರ್ಧ": 0.5,
    "a": 1,
    "an": 1,
    "one": 1,
    "ek": 1,
    "एक": 1,
    "ondu": 1,
    "ಒಂದು": 1,
    "two": 2,
    "do": 2,
    "दो": 2,
    "eradu": 2,
    "ಎರಡು": 2,
    "ಇಬ್ಬರಿಗೆ": 2,
    "three": 3,
    "teen": 3,
    "तीन": 3,
    "mooru": 3,
    "ಮೂರು": 3,
    "four": 4,
    "chaar": 4,
    "चार": 4,
    "naalku": 4,
    "ನಾಲ್ಕು": 4,
    "five": 5,
    "paanch": 5,
    "पांच": 5,
    "पाँच": 5,
    "aidu": 5,
    "ಐದು": 5,
    "six": 6,
    "chhe": 6,
    "छह": 6,
    "aaru": 6,
    "ಆರು": 6,
    "seven": 7,
    "saat": 7,
    "सात": 7,
    "elu": 7,
    "ಏಳು": 7,
    "eight": 8,
    "aath": 8,
    "आठ": 8,
    "entu": 8,
    "ಎಂಟು": 8,
    "nine": 9,
    "nau": 9,
    "नौ": 9,
    "ombattu": 9,
    "ಒಂಬತ್ತು": 9,
}
_UNITS = {
    "kg": "kg",
    "kgs": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "किलो": "kg",
    "ಕೆಜಿ": "kg",
    "ಕಿಲೋ": "kg",
    "litre": "l",
    "litres": "l",
    "liter": "l",
    "liters": "l",
    "लीटर": "l",
    "ಲೀಟರ್": "l",
    "packet": "packet",
    "packets": "packet",
    "पैकेट": "packet",
    "ಪ್ಯಾಕೆಟ್": "packet",
}
_GROCERIES = {
    "rice": ("rice", "chawal", "चावल", "akki", "ಅಕ್ಕಿ"),
    "milk": ("milk", "doodh", "दूध", "haalu", "ಹಾಲು"),
    "onion": ("onion", "onions", "pyaaz", "प्याज", "eerulli", "ಈರುಳ್ಳಿ"),
    "potato": ("potato", "potatoes", "aloo", "आलू", "aalugadde", "ಆಲೂಗಡ್ಡೆ"),
    "sugar": ("sugar", "cheeni", "चीनी", "sakkare", "ಸಕ್ಕರೆ"),
    "salt": ("salt", "namak", "नमक", "uppu", "ಉಪ್ಪು"),
    "coffee": ("coffee", "कॉफ़ी", "ಕಾಫಿ"),
    "banana": ("banana", "bananas", "kela", "केला", "ಬಾಳೆಹಣ್ಣು"),
    "bread": ("bread", "ब्रेड", "ಬ್ರೆಡ್"),
}
_DISHES = {
    "chicken biryani": ("chicken biryani", "चिकन बिरयानी", "ಚಿಕನ್ ಬಿರಿಯಾನಿ"),
    "veg biryani": ("veg biryani", "वेज बिरयानी", "ವೆಜ್ ಬಿರಿಯಾನಿ"),
    "biryani": ("biryani", "बिरयानी", "ಬಿರಿಯಾನಿ"),
    "masala dosa": ("masala dosa", "मसाला डोसा", "ಮಸಾಲೆ ದೋಸೆ"),
    "dosa": ("dosa", "डोसा", "ದೋಸೆ"),
}
_MEAL_WORDS = (
    "dinner",
    "lunch",
    "breakfast",
    "hungry",
    "something to eat",
    "khana",
    "खाना",
    "खाने",
    "भूख",
    "ಊಟ",
    "ತಿಂಡಿ",
)
_TABLE_WORDS = ("table", "टेबल", "ಟೇಬಲ್", "reservation", "reserve")
_AREAS = {
    "Indiranagar": ("indiranagar", "इंदिरानगर", "ಇಂದಿರಾನಗರ"),
    "Koramangala": ("koramangala", "कोरमंगला", "ಕೋರಮಂಗಲ"),
    "Bengaluru": ("bengaluru", "bangalore", "बेंगलुरु", "ಬೆಂಗಳೂರು"),
    "Mumbai": ("mumbai", "मुंबई", "ಮುಂಬೈ"),
    "Delhi": ("delhi", "दिल्ली", "ದೆಹಲಿ"),
}
_CUISINES = {
    "Italian": ("italian", "इटालियन", "ಇಟಾಲಿಯನ್"),
    "North Indian": ("north indian", "उत्तर भारतीय", "ಉತ್ತರ ಭಾರತೀಯ"),
    "South Indian": ("south indian", "दक्षिण भारतीय", "ದಕ್ಷಿಣ ಭಾರತೀಯ"),
    "Chinese": ("chinese", "चाइनीज़", "ಚೈನೀಸ್"),
}
_TOMORROW = ("tomorrow", "kal", "कल", "naale", "ನಾಳೆ")
_TODAY = ("today", "tonight", "aaj", "आज", "ivattu", "ಇವತ್ತು", "ಇಂದು")
_TIME_MARKERS = ("baje", "बजे", "gante", "ಗಂಟೆ", "ಗಂಟೆಗೆ", "o'clock", "pm")
_REMOVE_WORDS = ("remove", "hatao", "हटा", "ತೆಗೆ", "tegi")
_TOKEN = re.compile(r"[\w'ऀ-ॿಀ-೿]+")


def _first(lowered: str, table: dict[str, tuple[str, ...]]) -> str | None:
    for canonical, words in table.items():
        if any(word in lowered for word in words):
            return canonical
    return None


def _number(token: str) -> float | None:
    if token.isdigit():
        return float(token)
    return _NUMBERS.get(token)


def _guests(tokens: list[str]) -> int | None:
    for index, token in enumerate(tokens):
        if token in {"ಇಬ್ಬರಿಗೆ"}:
            return 2
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in {"for", "ke"} or following in {"people", "log", "logon", "लोग", "लोगों", "ಜನ"}:
            value = _number(following if token in {"for", "ke"} else token)
            if value is not None and value >= 1:
                return int(value)
    return None


def _time(tokens: list[str], lowered: str) -> str | None:
    """Spoken restaurant hours mean the evening unless the user says otherwise."""
    hour: float | None = None
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token == "at" and _number(following) is not None:
            hour = _number(following)
        elif following in _TIME_MARKERS and _number(token) is not None:
            hour = _number(token)
    if hour is None or not 1 <= hour <= 12:
        return None
    morning = any(word in lowered for word in (" am", "morning", "सुबह", "ಬೆಳಿಗ್ಗೆ"))
    afternoon_hours = hour == 12
    return f"{int(hour) if morning or afternoon_hours else int(hour) + 12:02d}:00"


def _date(lowered: str, today: str) -> str | None:
    if not today:
        return None
    base = date.fromisoformat(today)
    if any(word in lowered for word in _TOMORROW):
        return (base + timedelta(days=1)).isoformat()
    if any(word in lowered for word in _TODAY):
        return base.isoformat()
    return None


def _items(lowered: str, tokens: list[str]) -> list[GroceryItem]:
    del lowered
    found: list[GroceryItem] = []
    for index, token in enumerate(tokens):
        for canonical, words in _GROCERIES.items():
            if token not in words:
                continue
            before = [word for word in tokens[max(0, index - 3) : index] if word != "of"]
            unit = next((_UNITS[word] for word in before if word in _UNITS), None)
            quantity = next((q for q in map(_number, before) if q is not None), 1.0)
            found.append(
                GroceryItem(name=token, quantity=float(quantity), unit=unit, search_term=canonical)
            )
    return found


class ScriptedExtractor:
    def extract(
        self,
        text: str,
        *,
        language: str = "kn-IN",
        context: ClarificationContext | None = None,
    ) -> GroceryIntent:
        del language
        lowered = text.casefold()
        tokens = _TOKEN.findall(lowered)
        today = context.today if context else ""
        if any(word in lowered for word in _TABLE_WORDS) or (
            context is not None
            and context.active_domain == "dining"
            and (_first(lowered, _AREAS) or _first(lowered, _CUISINES))
        ):
            return GroceryIntent(
                intent="book_table",
                items=[],
                domain="dining",
                query=_first(lowered, _CUISINES),
                area=_first(lowered, _AREAS),
                date=_date(lowered, today),
                time=_time(tokens, lowered),
                guests=_guests(tokens),
            )
        dish = _first(lowered, _DISHES)
        if dish is not None:
            item = GroceryItem(name=dish, quantity=1.0, unit=None, search_term=dish)
            return GroceryIntent(intent="add_items", items=[item], domain="food")
        items = _items(lowered, tokens)
        if items:
            removing = any(word in lowered for word in _REMOVE_WORDS)
            return GroceryIntent(
                intent="remove_items" if removing else "add_items", items=items, domain="grocery"
            )
        if any(word in lowered for word in _MEAL_WORDS):
            return GroceryIntent(
                intent="unclear",
                items=[],
                domain="food",
                needs_clarification=True,
                clarification_question="What would you like to eat?",
            )
        return GroceryIntent(intent="unclear", items=[])
