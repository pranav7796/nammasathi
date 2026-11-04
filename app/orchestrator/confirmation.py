"""Deterministic yes/no and option-choice matching for spoken replies. Pure.

Only whole-phrase matches count: "yes but remove the milk" is not a yes. Text is
normalized to NFC with punctuation removed, keeping Indic vowel signs (category
M*), which `str.isalnum` would strip and so break every Kannada and Hindi word.
"""

from __future__ import annotations

import unicodedata

_AFFIRMATIVE = frozenset(
    {
        # English and romanized speech
        "yes",
        "yes please",
        "yes confirm",
        "yes place order",
        "yes place the order",
        "yeah",
        "yep",
        "ok",
        "okay",
        "ok confirm",
        "confirm",
        "confirm order",
        "confirm it",
        "proceed",
        "place order",
        "place the order",
        "haan",
        "han",
        "haan ji",
        "ji haan",
        "houdu",
        "haudu",
        "sari",
        # Hindi
        "हाँ",
        "हां",
        "जी हाँ",
        "जी हां",
        "हाँ जी",
        "हां जी",
        "ठीक है",
        "हाँ ठीक है",
        "हां ठीक है",
        "कर दो",
        "कर दीजिए",
        "ऑर्डर कर दो",
        "ऑर्डर कर दीजिए",
        "ओके",
        # Kannada
        "ಹೌದು",
        "ಹೌದು ಮಾಡಿ",
        "ಹೌದು ಆರ್ಡರ್ ಮಾಡಿ",
        "ಆರ್ಡರ್ ಮಾಡಿ",
        "ಸರಿ",
        "ಸರಿ ಮಾಡಿ",
        "ಆಗಲಿ",
        "ಆಯ್ತು",
        "ಓಕೆ",
        "ಖಚಿತಪಡಿಸಿ",
    }
)

_NEGATIVE = frozenset(
    {
        "no",
        "no thanks",
        "nope",
        "cancel",
        "stop",
        "dont",
        "nahi",
        "nahin",
        "illa",
        "beda",
        # Hindi
        "नहीं",
        "नही",
        "जी नहीं",
        "नहीं चाहिए",
        "मत करो",
        "रहने दो",
        "ना",
        # Kannada
        "ಇಲ್ಲ",
        "ಬೇಡ",
        "ಬೇಡ ಧನ್ಯವಾದ",
        "ಬೇಡ ಬಿಡಿ",
        "ರದ್ದು ಮಾಡಿ",
    }
)

_FIRST = frozenset(
    {"1", "one", "first", "first one", "pehla", "pehli", "modalu", "modaladu", "ondu"}
    | {"१", "एक", "पहला", "पहली", "पहले वाला", "पहली वाली", "पहला वाला"}
    | {"೧", "ಒಂದು", "ಮೊದಲು", "ಮೊದಲನೆಯದು", "ಮೊದಲನೇ", "ಮೊದಲಿನದು", "ಒಂದನೇದು", "ಒಂದನೆಯದು", "ಮೊದಲನೆಯ"}
)
_SECOND = frozenset(
    {"2", "two", "second", "second one", "doosra", "dusra", "doosri", "dusri", "eradu"}
    | {"२", "दो", "दूसरा", "दूसरी", "दूसरा वाला", "दूसरी वाली", "दूसरे वाला"}
    | {"೨", "ಎರಡು", "ಎರಡನೆಯದು", "ಎರಡನೇ", "ಎರಡನೇದು", "ಎರಡನೆಯ"}
)
_THIRD = frozenset(
    {"3", "three", "third", "third one", "teesra", "tisra", "teesri", "tisri", "mooru", "muru"}
    | {"३", "तीन", "तीसरा", "तीसरी", "तीसरा वाला", "तीसरी वाली"}
    | {"೩", "ಮೂರು", "ಮೂರನೆಯದು", "ಮೂರನೇ", "ಮೂರನೇದು", "ಮೂರನೆಯ"}
)
_CHOICE_FILLERS = ("the ", "number ", "option ", "नंबर ", "ನಂಬರ್ ")


def normalize_reply(text: str) -> str:
    """NFC-normalize, casefold, and drop punctuation and symbols, keeping combining marks."""
    composed = unicodedata.normalize("NFC", text).casefold()
    kept = "".join(
        character
        for character in composed
        if character.isspace() or unicodedata.category(character)[0] in {"L", "M", "N"}
    )
    return " ".join(kept.split())


_NORMALIZED_AFFIRMATIVE = frozenset(normalize_reply(word) for word in _AFFIRMATIVE)
_NORMALIZED_NEGATIVE = frozenset(normalize_reply(word) for word in _NEGATIVE)
_CHOICES = tuple(
    frozenset(normalize_reply(word) for word in group) for group in (_FIRST, _SECOND, _THIRD)
)


def explicit_confirmation(text: str) -> bool | None:
    """True for an explicit yes, False for an explicit no, None for anything else."""
    normalized = normalize_reply(text)
    if normalized in _NORMALIZED_AFFIRMATIVE:
        return True
    if normalized in _NORMALIZED_NEGATIVE:
        return False
    return None


def option_choice(text: str, option_count: int) -> int | None:
    """The 0-based option named by a spoken reply, if unambiguous and in range."""
    normalized = normalize_reply(text)
    for filler in _CHOICE_FILLERS:
        normalized = normalized.removeprefix(filler)
    for index, words in enumerate(_CHOICES):
        if normalized in words:
            return index if index < option_count else None
    if option_count == 1 and explicit_confirmation(text) is True:
        return 0
    return None
