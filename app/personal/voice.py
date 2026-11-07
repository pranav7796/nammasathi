"""Spoken reminder requests: "remind me to call Amma tomorrow at 7 pm", in English, Hindi, Kannada.

Deterministic, like the rest of the router: a phrase either clearly asks for a reminder or it is
left to the ordering assistant. The model is not involved, so it cannot invent a reminder.

A saved reminder is only a list entry. Saathi cannot ring or message anyone yet, and every reply
says so. Silence on that point would let someone rely on an alert that never comes.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.genui.builders import ReminderInput, reminder_list_widget
from app.genui.models import Widget
from app.genui.sanitize import clean_text
from app.personal.service import from_ms
from app.personal.store import Reminder

_ENGLISH = re.compile(
    r"^\s*(?:(?:hey|ok|okay)\s+saathi[, ]*)?(?:please\s+)?"
    r"(?:remind me\b|set (?:a |an )?reminder\b|add (?:a |an )?reminder\b|"
    r"create (?:a |an )?reminder\b|reminder\s*:)",
    re.IGNORECASE,
)
# Hindi and Kannada put the verb last: "<what and when> yaad dilao" / "<what and when> nenapisi".
_WORD = r"[\w\u0900-\u097F\u0C80-\u0CFF]*"  # \w drops Indic vowel signs, which end many verbs
_HINDI_VERB = re.compile(
    rf"(?:मुझे\s+)?(?:याद\s*दिला{_WORD}(?:\s+(?:दो|दीजिए|दीजिये|देना))?|रिमाइंडर\s*{_WORD})"
    r"\s*[।.!]*\s*$"
)
_ROMAN_HINDI_VERB = re.compile(
    r"(?:mujhe\s+)?yaad\s*dila\w*(?:\s+(?:do|dena|dijiye))?\s*[.!]*\s*$", re.IGNORECASE
)
_KANNADA_VERB = re.compile(rf"(?:ನನಗೆ\s+)?(?:ನೆನಪಿಸ{_WORD}|ರಿಮೈಂಡರ್\s*{_WORD})\s*[.!]*\s*$")

_LEADING_PRONOUN = re.compile(r"^(?:मुझे|mujhe|ನನಗೆ)\s+", re.IGNORECASE)


def reminder_request(text: str) -> str | None:
    """The reminder wording if this is clearly a reminder request, otherwise None."""
    if _ENGLISH.match(text):
        return text.strip()
    for verb in (_HINDI_VERB, _ROMAN_HINDI_VERB, _KANNADA_VERB):
        match = verb.search(text.strip())
        if match is not None:
            remainder = text.strip()[: match.start()].strip(" ,.।")
            remainder = _LEADING_PRONOUN.sub("", remainder)
            return remainder or None
    return None


def _when(due: datetime) -> str:
    return due.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def reply_text(language: str, reminder: Reminder) -> str:
    """What Saathi says after saving. Always states that it will not ring."""
    shown = clean_text(reminder.text, 140)
    due = None if reminder.due_at is None else from_ms(reminder.due_at)
    if language == "hi-IN":
        if due is None:
            return (
                f"मैंने रिमाइंडर सेव कर लिया: {shown}। इसमें कोई समय नहीं है। "
                "यह आपकी सूची में है, पर साथी अभी आपको अलर्ट नहीं भेज सकता।"
            )
        return (
            f"मैंने रिमाइंडर सेव कर लिया: {shown}, {_when(due)} के लिए। "
            "यह आपकी सूची में है, पर साथी अभी आपको अलर्ट नहीं भेज सकता।"
        )
    if language == "kn-IN":
        if due is None:
            return (
                f"ರಿಮೈಂಡರ್ ಉಳಿಸಿದ್ದೇನೆ: {shown}. ಸಮಯ ಇಲ್ಲ. "
                "ಇದು ನಿಮ್ಮ ಪಟ್ಟಿಯಲ್ಲಿದೆ, ಆದರೆ ಸಾಥಿ ಇನ್ನೂ ನಿಮಗೆ ಎಚ್ಚರಿಕೆ ಕಳುಹಿಸಲಾರದು."
            )
        return (
            f"ರಿಮೈಂಡರ್ ಉಳಿಸಿದ್ದೇನೆ: {shown}, {_when(due)}ಕ್ಕೆ. "
            "ಇದು ನಿಮ್ಮ ಪಟ್ಟಿಯಲ್ಲಿದೆ, ಆದರೆ ಸಾಥಿ ಇನ್ನೂ ನಿಮಗೆ ಎಚ್ಚರಿಕೆ ಕಳುಹಿಸಲಾರದು."
        )
    if due is None:
        return (
            f"Saved a reminder: {shown}. I didn't find a time in that. "
            "It's in your list, but Saathi can't alert you yet."
        )
    return (
        f"Saved a reminder: {shown}, for {_when(due)}. "
        "It's in your list, but Saathi can't alert you yet."
    )


def sign_in_text(language: str) -> str:
    if language == "hi-IN":
        return "रिमाइंडर सेव करने के लिए पहले साइन इन करें।"
    if language == "kn-IN":
        return "ರಿಮೈಂಡರ್ ಉಳಿಸಲು ಮೊದಲು ಸೈನ್ ಇನ್ ಮಾಡಿ."
    return "Please sign in first, then I can save reminders."


def unavailable_text(language: str) -> str:
    if language == "hi-IN":
        return "अभी रिमाइंडर सेव नहीं हो पाया। थोड़ी देर बाद फिर कोशिश करें।"
    if language == "kn-IN":
        return "ಈಗ ರಿಮೈಂಡರ್ ಉಳಿಸಲಾಗಲಿಲ್ಲ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ."
    return "I couldn't save that reminder just now. Please try again in a moment."


def reminders_ui(reminders: list[Reminder]) -> list[Widget]:
    """The person's reminders as a widget, newest state, so a repeat spoken request updates it."""
    items = [
        ReminderInput(
            id=item.id,
            text=clean_text(item.text, 500),
            due=None if item.due_at is None else from_ms(item.due_at),
            done=item.done_at is not None,
        )
        for item in reminders
    ]
    return [reminder_list_widget(items)]
