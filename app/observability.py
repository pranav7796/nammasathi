"""Structured, allow-listed event logging (DESIGN.md §11).

Only scalar fields named in `_ALLOWED_FIELDS` are emitted, so transcripts,
addresses, phone numbers, tokens, and provider payloads cannot reach the logs
even if a caller passes them by mistake.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

logger = logging.getLogger("ajji.events")

_ALLOWED_FIELDS = frozenset(
    {
        "attempt_id",
        "correlation_id",
        "duration_ms",
        "edit",
        "from_state",
        "items",
        "kind",
        "language",
        "mode",
        "op",
        "outcome",
        "reason",
        "status",
        "to_state",
        "tool",
        "turn_event",
        "verified",
    }
)


def log_event(event: str, **fields: object) -> dict[str, object]:
    """Log one JSON event and return what was logged. Unlisted or non-scalar fields are dropped."""
    record: dict[str, object] = {"ts": datetime.now(UTC).isoformat(), "event": event}
    for key, value in fields.items():
        if key in _ALLOWED_FIELDS and isinstance(value, (str, int, float, bool)):
            record[key] = value
    logger.info(json.dumps(record, ensure_ascii=False))
    return record


def configure_logging() -> None:
    """Give the event logger a console handler when the host process has none."""
    if not logger.handlers and not logging.getLogger().handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
