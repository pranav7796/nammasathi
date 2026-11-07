"""Sending WhatsApp messages through Twilio's Messages API, after the person approved each one.

Official documentation used:
  * https://www.twilio.com/docs/messaging/api/message-resource (POST .../Messages.json with To,
    From, Body, StatusCallback; status values accepted, queued, sending, sent, delivered,
    undelivered, failed, read)
  * https://www.twilio.com/docs/api/errors/63016 (outside the 24-hour window: a template is needed)
  * https://www.twilio.com/docs/api/errors/63015 (the sandbox only reaches numbers that joined it)
  * https://www.twilio.com/docs/api/errors/21211 (invalid To number)

Implements app.integrations.base.MessagingClient, so the task engine, the approval rule and the
status table work exactly as they do for Meta's API. Never retries a send whose outcome is unclear.
"""

from __future__ import annotations

import asyncio
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
from app.whatsapp.client import validate

API = "https://api.twilio.com/2010-04-01"
TIMEOUT_SECONDS = 10.0
UNKNOWN_OUTCOME = "Outcome unknown: check WhatsApp before sending again."
# Twilio's status words, in our own terms (see MESSAGE_STATUSES).
STATUS = {
    "accepted": "accepted",
    "scheduled": "accepted",
    "queued": "accepted",
    "sending": "accepted",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "undelivered": "failed",
    "failed": "failed",
    "canceled": "failed",
}
_REFUSALS = {
    63016: "WhatsApp only allows a free message within 24 hours of the person's last message to "
    "this number. Ask them to message first, or use an approved template.",
    63015: "This sandbox can only message numbers that joined it. Ask the person to send the "
    "sandbox join code first.",
    21211: "That isn't a valid phone number for Twilio.",
}


def _error_code(response: httpx.Response) -> int | None:
    try:
        data: Any = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    code = cast(dict[str, Any], data).get("code")
    return code if isinstance(code, int) else None


class TwilioMessaging:
    def __init__(
        self,
        settings: Settings,
        accounts: AccountService,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._accounts = accounts
        self._transport = transport
        self._sent: dict[tuple[str, str], SentMessage] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _auth(self) -> tuple[str, str]:
        s = self._settings
        if s.twilio_api_key_sid and s.twilio_api_key_secret:
            return s.twilio_api_key_sid, s.twilio_api_key_secret
        return s.twilio_account_sid, s.twilio_auth_token or ""

    async def send(
        self, user_id: str, message: OutgoingMessage, idempotency_key: str
    ) -> SentMessage:
        if not self._settings.twilio_can_send:
            raise NotConnected("Twilio is not set up for sending.")
        validate(message)  # before any request
        key = (user_id, idempotency_key)
        async with self._locks.setdefault(key, asyncio.Lock()):
            earlier = self._sent.get(key)
            if earlier is not None:
                return earlier  # asked twice: sent once
            sent = await self._post(user_id, message)
            self._sent[key] = sent
            return sent

    async def _post(self, user_id: str, message: OutgoingMessage) -> SentMessage:
        s = self._settings
        form = {
            "To": f"whatsapp:{message.to_phone}",
            "From": s.twilio_sender,
            "Body": message.body,
        }
        if s.public_base_url:
            form["StatusCallback"] = f"{s.public_base_url.rstrip('/')}/api/twilio/status"
        url = f"{API}/Accounts/{s.twilio_account_sid}/Messages.json"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS
            ) as http:
                response = await http.post(url, data=form, auth=self._auth())
        except (httpx.ConnectError, httpx.ConnectTimeout):
            log_event("twilio", op="send", outcome="not_reached")
            raise IntegrationError(
                "Couldn't reach Twilio. Nothing was sent.", retryable=True
            ) from None
        except httpx.HTTPError:
            log_event("twilio", op="send", outcome="unknown")
            raise IntegrationError(UNKNOWN_OUTCOME) from None
        if response.status_code in (401, 403):
            log_event("twilio", op="send", outcome="refused_credentials")
            raise ReauthRequired(
                "Twilio refused this site's credentials. The owner must check them."
            )
        if response.status_code == 429:
            raise RateLimited("Twilio is busy. Nothing was sent; try again in a moment.")
        if response.status_code >= 500:
            log_event("twilio", op="send", outcome="unknown")
            raise IntegrationError(UNKNOWN_OUTCOME)
        if response.status_code >= 400:
            code = _error_code(response)
            log_event(
                "twilio", op="send", outcome="refused", reason=str(code or response.status_code)
            )
            raise IntegrationError(_REFUSALS.get(code or 0, f"Twilio refused it (error {code})."))
        try:
            data = cast(dict[str, Any], response.json())
        except ValueError:
            raise IntegrationError(UNKNOWN_OUTCOME) from None
        message_id = str(data.get("sid") or "")
        if not message_id:
            raise IntegrationError(UNKNOWN_OUTCOME)
        status = STATUS.get(str(data.get("status") or "queued"), "accepted")
        try:
            await self._accounts.run(
                store.record_sent,
                self._accounts.database,
                user_id,
                message_id,
                message.recipient_label,
            )
            if status != "accepted":
                await self._accounts.run(
                    store.apply_status, self._accounts.database, message_id, status, ""
                )
        except Exception:
            # Twilio accepted it but we could not note it: never let a caller send it again.
            raise IntegrationError(UNKNOWN_OUTCOME) from None
        log_event("twilio", op="send", outcome="accepted")
        return SentMessage(message_id, status, "", datetime.now(UTC))

    async def status(self, user_id: str, message_id: str) -> SentMessage:
        row = await self._accounts.run(
            store.status_for, self._accounts.database, user_id, message_id
        )
        if row is None:
            raise IntegrationError("That message was not found.")
        return SentMessage(message_id, row.status, row.detail)

    async def check(self) -> None:
        """Asks Twilio whether the credentials work, by listing at most one message. Sends nothing.

        Not GET /Accounts/{SID}.json: a Standard API key may not read the Accounts resource (only a
        Main key may; https://www.twilio.com/docs/iam/api-keys/key-resource-v1), so that check
        would wrongly refuse a working key. The listed message is never read or kept.
        """
        s = self._settings
        if not s.twilio_can_send:
            raise NotConnected("Twilio is not set up.")
        url = f"{API}/Accounts/{s.twilio_account_sid}/Messages.json?PageSize=1"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS
            ) as http:
                response = await http.get(url, auth=self._auth())
        except httpx.HTTPError:
            raise IntegrationError("Couldn't reach Twilio.", retryable=True) from None
        if response.status_code in (401, 403):
            raise ReauthRequired("Twilio refused this site's credentials.")
        if response.status_code >= 400:
            raise IntegrationError(f"Twilio answered {response.status_code}.", retryable=True)
