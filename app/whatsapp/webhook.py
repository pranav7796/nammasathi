"""Pure webhook helpers. No network, no database, no logging.

Official docs relied on:
- Payload shapes (statuses[], messages[]):
  https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
- X-Hub-Signature-256 (`sha256=` + HMAC-SHA256 of the raw body keyed with the App Secret):
  https://developers.facebook.com/docs/graph-api/webhooks/getting-started
  (that page does not spell out "HMAC over the raw bytes"; Meta's payload-validation guidance
  does, and it is the standard construction. Unverified against a live webhook.)

Inbound message text is never read here: only a count is returned.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, cast

from app.integrations.base import MESSAGE_STATUSES

SIGNATURE_PREFIX = "sha256="
# What Meta reports for an outgoing message. "accepted" is ours only, never reported by Meta.
REPORTED_STATUSES = frozenset(s for s in MESSAGE_STATUSES if s != "accepted")


@dataclass(frozen=True)
class StatusUpdate:
    message_id: str
    status: str  # sent | delivered | read | failed
    timestamp: int | None = None  # seconds since the epoch, as Meta sent it
    error_code: int | None = None
    error_title: str = ""  # Meta's short generic title; never a phone number


def verify_signature(app_secret: str | None, raw_body: bytes, header: str | None) -> bool:
    """True only for `sha256=<hex>` equal to the HMAC-SHA256 of the raw body. Constant time."""
    if not app_secret or not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    given = header[len(SIGNATURE_PREFIX) :]
    if len(given) != 64:
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given.lower())


def _dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def _values(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    found: list[dict[str, Any]] = []
    for entry in _dicts(cast(dict[str, Any], payload).get("entry")):
        for change in _dicts(entry.get("changes")):
            value = change.get("value")
            if isinstance(value, dict):
                found.append(cast(dict[str, Any], value))
    return found


def parse_statuses(payload: object) -> list[StatusUpdate]:
    """Every well-formed status in the payload; anything odd is skipped, never raised."""
    updates: list[StatusUpdate] = []
    for value in _values(payload):
        for item in _dicts(value.get("statuses")):
            message_id = item.get("id")
            status = item.get("status")
            if not isinstance(message_id, str) or not message_id or len(message_id) > 200:
                continue
            if not isinstance(status, str) or status not in REPORTED_STATUSES:
                continue
            raw_time = item.get("timestamp")
            timestamp = int(raw_time) if isinstance(raw_time, str) and raw_time.isdigit() else None
            code: int | None = None
            title = ""
            errors = _dicts(item.get("errors"))
            if errors:
                raw_code = errors[0].get("code")
                code = raw_code if isinstance(raw_code, int) else None
                raw_title = errors[0].get("title")
                title = raw_title[:120] if isinstance(raw_title, str) else ""
            updates.append(StatusUpdate(message_id, status, timestamp, code, title))
    return updates


def count_inbound(payload: object) -> int:
    """How many inbound messages the payload carries. Their content is not touched."""
    return sum(len(_dicts(value.get("messages"))) for value in _values(payload))
