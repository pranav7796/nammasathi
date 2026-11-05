"""Decide which capability handles a request. Pure and deterministic.

The LLM's suggested domain is only an input. Replies to the current task stay with it, so a
yes or a cancel can never reach a different capability from the one that asked.
"""

from __future__ import annotations

from typing import Literal

Domain = Literal["grocery", "food", "dining"]
DOMAINS: tuple[Domain, ...] = ("grocery", "food", "dining")

# These only make sense as replies to whatever is happening now.
_REPLY_INTENTS = frozenset(
    {"confirm", "reject", "cancel", "done", "select_option", "remove_items", "change_quantity"}
)
_DINING_WORDS = (
    "book a table",
    "reserve a table",
    "table for",
    "reservation",
    "dine out",
    "eat out",
    "ಟೇಬಲ್",
    "टेबल",
)
_FOOD_WORDS = (
    "dinner",
    "lunch",
    "breakfast",
    "hungry",
    "biryani",
    "pizza",
    "dosa",
    "burger",
    "thali",
    "restaurant",
    "ಬಿರಿಯಾನಿ",
    "ದೋಸೆ",
    "ಊಟ",
    "ತಿಂಡಿ",
    "बिरयानी",
    "डोसा",
    "खाना",
    "भूख",
)


_BACK_WORDS = ("back to", "go back", "return to", "wapas", "वापस", "ವಾಪಸ್", "ಮತ್ತೆ")
_TASK_WORDS: dict[Domain, tuple[str, ...]] = {
    "grocery": ("grocer", "basket", "kirana", "किराना", "टोकरी", "ದಿನಸಿ", "ಬುಟ್ಟಿ"),
    "food": ("meal", "food", "khana", "खाना", "ಊಟ"),
    "dining": ("table", "booking", "टेबल", "ಟೇಬಲ್"),
}


def resume_domain(text: str) -> Domain | None:
    """Only an explicit "back to my groceries" returns to a waiting task."""
    lowered = text.casefold()
    if not any(word in lowered for word in _BACK_WORDS):
        return None
    for domain, words in _TASK_WORDS.items():
        if any(word in lowered for word in words):
            return domain
    return None


def keyword_domain(text: str) -> Domain | None:
    lowered = text.casefold()
    if any(word in lowered for word in _DINING_WORDS):
        return "dining"
    if any(word in lowered for word in _FOOD_WORDS):
        return "food"
    return None


def _valid(domain: str | None) -> Domain | None:
    for known in DOMAINS:
        if domain == known:
            return known
    return None


def route(
    *,
    suggested: str | None,
    intent: str,
    text: str,
    active: Domain | None,
    has_items: bool,
    needs_clarification: bool = False,
) -> Domain | None:
    """The capability for this turn, or None if Saathi should ask what the user wants."""
    domain = _valid(suggested)
    if intent in _REPLY_INTENTS:
        return active
    if intent == "unclear" and not needs_clarification:
        return active or domain
    if intent == "book_table":
        return "dining"
    if intent == "reorder_usual":
        return "grocery"
    if domain is not None:
        return domain
    found = keyword_domain(text)
    if found is not None:
        return found
    if active is not None:
        return active
    return "grocery" if has_items else None
