"""Plain-text hygiene for strings that come from outside (product names, emails, ...)."""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<(?=[A-Za-z/])")
_JS = re.compile(r"javascript\s*:", re.IGNORECASE)


def clean_text(value: str, limit: int) -> str:
    """Strip control characters, collapse whitespace, neutralise markup-looking text, truncate.

    The result is at most `limit` characters (an ellipsis counts). A "<" that would start a tag
    is dropped and "javascript:" is removed, so the strict widget validator accepts the output.
    """
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in value
        if ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")
    )
    text = _WS.sub(" ", kept).strip()
    previous = None
    while (
        previous != text
    ):  # repeat: removal can expose a new match ("<<b", "javajavascript:script:")
        previous = text
        text = _JS.sub("", _TAG.sub("", text))
    text = _WS.sub(" ", text).strip()
    if limit <= 0:
        return ""
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text
