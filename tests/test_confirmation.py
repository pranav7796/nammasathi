from __future__ import annotations

import pytest

from app.orchestrator.confirmation import explicit_confirmation, normalize_reply, option_choice


@pytest.mark.parametrize(
    "word",
    [
        "yes",
        "Yes.",
        "YES!",
        "haan",
        "Haan ji",
        "ok",
        "हाँ",
        "हां",
        "हाँ।",
        "जी हाँ",
        "ठीक है",
        "कर दीजिए",
        "ಹೌದು",
        "ಹೌದು.",
        "ಸರಿ",
        "ಆರ್ಡರ್ ಮಾಡಿ",
    ],
)
def test_explicit_yes_in_every_language(word: str) -> None:
    assert explicit_confirmation(word) is True


@pytest.mark.parametrize(
    "word", ["no", "No thanks", "cancel", "nahi", "नहीं", "नहीं।", "मत करो", "ಬೇಡ", "ಇಲ್ಲ", "ರದ್ದು ಮಾಡಿ"]
)
def test_explicit_no_in_every_language(word: str) -> None:
    assert explicit_confirmation(word) is False


@pytest.mark.parametrize(
    "word",
    [
        "",
        "hmm",
        "maybe",
        "yes but remove the milk",
        "not yes",
        "no yes",
        "हाँ लेकिन दूध हटा दो",
        "शायद",
        "ಹೌದು ಆದರೆ ಹಾಲು ಬೇಡ",
        "ಗೊತ್ತಿಲ್ಲ",
        "ह",  # what the old matcher reduced हाँ to
    ],
)
def test_anything_else_is_never_a_yes(word: str) -> None:
    assert explicit_confirmation(word) is not True


def test_normalization_keeps_indic_vowel_signs() -> None:
    assert normalize_reply("ಹೌದು") == "ಹೌದು"
    assert normalize_reply("हाँ") == "हाँ"


def test_normalization_handles_decomposed_input() -> None:
    assert explicit_confirmation("हाँ") is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("first", 0),
        ("The second one", 1),
        ("option 3", 2),
        ("2", 1),
        ("number two", 1),
        ("पहला", 0),
        ("दूसरा वाला", 1),
        ("तीसरा", 2),
        ("ಮೊದಲನೆಯದು", 0),
        ("ಎರಡನೆಯದು", 1),
        ("ಮೂರು", 2),
    ],
)
def test_option_choice_in_every_language(text: str, expected: int) -> None:
    assert option_choice(text, 3) == expected


def test_option_out_of_range_is_not_a_choice() -> None:
    assert option_choice("third", 2) is None


def test_unrelated_reply_is_not_a_choice() -> None:
    assert option_choice("the cheaper milk please", 3) is None
    assert option_choice("yes", 3) is None


def test_yes_picks_only_when_there_is_a_single_option() -> None:
    assert option_choice("yes", 1) == 0
    assert option_choice("ಹೌದು", 1) == 0
