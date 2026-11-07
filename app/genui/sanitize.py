"""Plain-text hygiene for strings that come from outside (product names, emails, ...)."""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<(?=[A-Za-z/])")
_JS = re.compile(r"j\s*a\s*v\s*a\s*s\s*c\s*r\s*i\s*p\s*t\s*:", re.IGNORECASE)


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


def clean_multiline(value: str, limit: int) -> str:
    """Like clean_text but keeps line breaks (for a message body). Blank-line runs shrink to one."""
    lines = [
        clean_text(raw, len(raw) + 1)
        for raw in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    text = "\n".join(out).strip()
    if "javascript:" in re.sub(r"\s", "", text).lower():  # split across lines: flatten instead
        return clean_text(text, limit)
    if limit <= 0:
        return ""
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text
