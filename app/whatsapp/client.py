"""WhatsApp Cloud API sender. Implements app.integrations.base.MessagingClient.

Official docs relied on:
- Send messages (POST /{phone-number-id}/messages, text body, response `messages[0].id`, the
  24-hour customer service window: free text only inside it, templates otherwise):
  https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
- Error codes (190, 131047, 131026, 130429, 131056):
  https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
- 131030 ("recipient not in allowed list", test numbers) is NOT on the pages I could read;
  it is from Meta's published code list and is unverified here.

The API's reply only means the request was accepted. Delivery arrives later by webhook, so a
new message is recorded as "accepted" and `status` repeats only what Meta reported.

Idempotency: an in-memory map (user, key) -> SentMessage inside this process, guarded by a lock
per key so two concurrent calls send once. The table has no key column, so a restart forgets
the map: the caller (the task engine) must not re-send after a restart without checking.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from app.accounts.service import AccountService
from app.config import Settings
from app.integrations.base import (
    IntegrationError,
    NotConnected,
    OutgoingMessage,
    RateLimited,
    ReauthRequired,
    SentMessage,
)
from app.observability import log_event
from app.whatsapp import store

GRAPH = "https://graph.facebook.com"
TIMEOUT_SECONDS = 10.0
MAX_BODY = 1000
_E164 = re.compile(r"^\+\d{8,15}$")
_VERSION = re.compile(r"^v\d{1,3}\.\d{1,2}$")

UNKNOWN_OUTCOME = "outcome unknown: check WhatsApp before sending again"
EXPIRED = "the WhatsApp access token expired: ask the operator to renew it"
OUTSIDE_WINDOW = (
    "WhatsApp only allows free text within 24 hours of the person's last message to this "
    "number, so a message template is needed for this one"
)
NOT_ALLOWED = "this number is not on the test allow list"
UNDELIVERABLE = "WhatsApp could not deliver to this number"


def validate(message: OutgoingMessage) -> None:
    if not _E164.match(message.to_phone):
        raise IntegrationError("the phone number must look like +919876543210")
    body = message.body
    if not body.strip() or len(body) > MAX_BODY:
        raise IntegrationError(f"the message must be 1 to {MAX_BODY} characters")
    if any(unicodedata.category(c) == "Cc" and c != "\n" for c in body):
        raise IntegrationError("the message contains control characters")


class WhatsAppClient:
    def __init__(
        self,
        settings: Settings,
        accounts_store: AccountService,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._accounts = accounts_store
        self._transport = transport
        self._sent: dict[tuple[str, str], SentMessage] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def send(
        self, user_id: str, message: OutgoingMessage, idempotency_key: str
    ) -> SentMessage:
        s = self._settings
        if not s.whatsapp_configured:
            raise NotConnected("WhatsApp is not configured")
        validate(message)  # before any request
        if not _VERSION.match(s.whatsapp_graph_version):
            raise IntegrationError("WhatsApp is configured with an invalid Graph API version")
        key = (user_id, idempotency_key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            earlier = self._sent.get(key)
            if earlier is not None:
                return earlier
            sent = await self._post(user_id, message)
            self._sent[key] = sent
            return sent

    async def _post(self, user_id: str, message: OutgoingMessage) -> SentMessage:
        s = self._settings
        url = f"{GRAPH}/{s.whatsapp_graph_version}/{s.whatsapp_phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.to_phone,
            "type": "text",
            "text": {"preview_url": False, "body": message.body},
        }
        headers = {"Authorization": f"Bearer {s.whatsapp_access_token}"}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS
            ) as http:
                response = await http.post(url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Nothing was transmitted, so trying again later is safe.
            log_event("whatsapp", op="send", outcome="not_connected_network")
            raise IntegrationError("could not reach WhatsApp", retryable=True) from None
        except httpx.HTTPError:
            # A read timeout or dropped connection: the message may have gone out.
            log_event("whatsapp", op="send", outcome="unknown")
            raise IntegrationError(UNKNOWN_OUTCOME) from None

        code = _error_code(response)
        if response.status_code >= 400 or code is not None:
            log_event(
                "whatsapp", op="send", outcome="refused", reason=str(code or response.status_code)
            )
            raise _map_error(response.status_code, code)

        message_id = _message_id(response)
        if message_id is None:
            log_event("whatsapp", op="send", outcome="unknown", reason="no_id")
            raise IntegrationError(UNKNOWN_OUTCOME)
        try:
            await self._accounts.run(
                store.record_sent,
                self._accounts.database,
                user_id,
                message_id,
                message.recipient_label,
            )
        except Exception:
            # Sent but not recorded: say so rather than let a caller send it again.
            log_event("whatsapp", op="send", outcome="unrecorded")
            raise IntegrationError(
                "the message was accepted by WhatsApp but its status could not be saved: "
                "check WhatsApp before sending again"
            ) from None
        log_event("whatsapp", op="send", outcome="accepted")
        return SentMessage(
            id=message_id,
            status="accepted",
            detail="accepted by WhatsApp; delivery is not confirmed yet",
            reported_at=datetime.now(UTC),
        )

    async def status(self, user_id: str, message_id: str) -> SentMessage:
        row = await self._accounts.run(
            store.status_for, self._accounts.database, user_id, message_id
        )
        if row is None:
            raise IntegrationError("that WhatsApp message was not found")
        return SentMessage(
            id=message_id,
            status=row.status,
            detail=row.detail,
            reported_at=datetime.fromtimestamp(row.updated_at_ms / 1000, UTC),
        )


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _error_code(response: httpx.Response) -> int | None:
    error = _json(response).get("error")
    if isinstance(error, dict):
        code = cast(dict[str, Any], error).get("code")
        if isinstance(code, int):
            return code
    return None


def _message_id(response: httpx.Response) -> str | None:
    messages = _json(response).get("messages")
    if isinstance(messages, list) and messages:
        first = cast(list[object], messages)[0]
        if isinstance(first, dict):
            mid = cast(dict[str, Any], first).get("id")
            if isinstance(mid, str) and mid:
                return mid
    return None


def _map_error(http_status: int, code: int | None) -> IntegrationError:
    if code == 190 or http_status == 401:
        return ReauthRequired(EXPIRED)
    if code == 131047:
        return IntegrationError(OUTSIDE_WINDOW)
    if code == 131030:
        return IntegrationError(NOT_ALLOWED)
    if code == 131026:
        return IntegrationError(UNDELIVERABLE)
    if code in (130429, 131056) or http_status == 429:
        return RateLimited("WhatsApp is limiting how fast messages can be sent", retry_after=30.0)
    if http_status >= 500:
        # The send may have been processed before the failure.
        return IntegrationError(UNKNOWN_OUTCOME)
    return IntegrationError(f"WhatsApp refused the message (error {code or http_status})")
