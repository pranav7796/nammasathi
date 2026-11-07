"""Gmail adapter: implements app.integrations.base.EmailClient over the Gmail REST API.

Contract (read 2026-11-07):
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
  (POST /gmail/v1/users/me/messages/send, scope gmail.send)
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list, /get
  and users.threads/list, /get, users/getProfile (gmail.readonly); `q` uses Gmail search syntax
  https://support.google.com/mail/answer/7190
- Message resource: `raw` is the whole RFC 2822 message, base64url; `threadId` joins a thread;
  `labelIds` such as INBOX, SENT, UNREAD, IMPORTANT; `internalDate` is epoch milliseconds.
Not verified against a live mailbox: error `reason` strings for 403 (taken from Google's common
error documentation), and Retry-After on 429 (honoured when present).

Message bodies are untrusted data. They are decoded and returned as text and never interpreted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any, cast

import httpx

from app.integrations.base import (
    Email,
    EmailSummary,
    IntegrationError,
    OutgoingEmail,
    RateLimited,
    ReauthRequired,
    SentEmail,
    TokenProvider,
)
from app.observability import log_event

API = "https://gmail.googleapis.com/gmail/v1/users/me"
PROVIDER = "gmail"
IMPORTANT_QUERY = "is:important in:inbox newer_than:14d"
NEEDS_REPLY_QUERY = "in:inbox newer_than:14d"
MAX_BODY = 20000
TIMEOUT_SECONDS = 10.0
CONCURRENCY = 4
MAX_LIMIT = 50
MAX_RETRIES = 2
MAX_WAIT_SECONDS = 5.0
REAUTH_MESSAGE = "Gmail access has ended or was not allowed. Please connect Gmail again."
_RATE_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"}
)

Json = dict[str, Any]
Sleep = Callable[[float], Awaitable[None]]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self._skip += 1
        elif tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self._skip:
            self._skip -= 1
        elif tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup must not fail the read
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _dict(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[Json]:
    if not isinstance(value, list):
        return []
    return [cast(Json, item) for item in cast(list[object], value) if isinstance(item, dict)]


def _strs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast(list[object], value) if isinstance(item, str)]


def _headers(payload: Json) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in _dicts(payload.get("headers")):
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result.setdefault(name.lower(), value)
    return result


def _charset(part: Json) -> str:
    content_type = _headers(part).get("content-type", "")
    match = re.search(r"charset=\"?([\w.\-]+)", content_type, re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _decode(part: Json) -> str:
    data = _dict(part.get("body")).get("data")
    if not isinstance(data, str):
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: Json, mime: str, found: list[str]) -> None:
    if part.get("filename"):
        return  # an attachment, not the message text
    if part.get("mimeType") == mime:
        text = _decode(part)
        if text:
            found.append(text)
    for child in _dicts(part.get("parts")):
        _walk(child, mime, found)


def extract_body(payload: Json) -> str:
    plain: list[str] = []
    _walk(payload, "text/plain", plain)
    if plain:
        text = "\n".join(plain).strip()
    else:
        markup: list[str] = []
        _walk(payload, "text/html", markup)
        text = html_to_text("\n".join(markup))
    return text[:MAX_BODY]


def _received(message: Json) -> datetime:
    try:
        return datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return datetime.fromtimestamp(0, tz=UTC)


def _summary(message: Json, *, needs_reply: bool = False) -> EmailSummary:
    headers = _headers(_dict(message.get("payload")))
    name, address = parseaddr(headers.get("from", ""))
    labels = _strs(message.get("labelIds"))
    return EmailSummary(
        id=str(message.get("id", "")),
        thread_id=str(message.get("threadId", "")),
        sender_name=name or address,
        sender_email=address.lower(),
        subject=headers.get("subject", ""),
        snippet=html.unescape(str(message.get("snippet", ""))),
        received=_received(message),
        unread="UNREAD" in labels,
        important="IMPORTANT" in labels,
        needs_reply=needs_reply,
    )


def _clean_header(value: str, what: str) -> str:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{what} must not contain line breaks")
    return value.strip()


def build_raw(mail: OutgoingEmail) -> str:
    """The base64url RFC 2822 message Gmail expects in `raw`. Raises ValueError on bad input."""
    if not mail.to:
        raise ValueError("A recipient is required")
    recipients: list[str] = []
    for address in mail.to:
        address = _clean_header(address, "Recipient")
        _, bare = parseaddr(address)
        if not bare or "@" not in bare or any(c in bare for c in ' ,;<>"') or "," in address:
            raise ValueError("Recipient is not a single email address")
        recipients.append(bare)
    message = EmailMessage()
    message["To"] = ", ".join(recipients)
    message["Subject"] = _clean_header(mail.subject, "Subject")
    if mail.in_reply_to:
        reference = _clean_header(mail.in_reply_to, "In-Reply-To")
        message["In-Reply-To"] = reference
        message["References"] = reference
    message.set_content(mail.body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class GmailClient:
    def __init__(
        self,
        tokens: TokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._tokens = tokens
        self._transport = transport
        self._sleep = sleep
        self._sent: dict[str, SentEmail] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}

    # HTTP --------------------------------------------------------------------------------

    async def _token(self, user_id: str, rejected: str = "") -> str:
        if rejected:
            invalidate = getattr(self._tokens, "invalidate", None)
            if invalidate is not None:
                return cast(str, await invalidate(user_id, PROVIDER, rejected))
        return await self._tokens.access_token(user_id, PROVIDER)

    async def _request(
        self,
        user_id: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Json | None = None,
        idempotent: bool = True,
    ) -> Json:
        token = await self._token(user_id)
        refreshed = False
        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(
                    timeout=TIMEOUT_SECONDS, transport=self._transport
                ) as http:
                    response = await http.request(
                        method,
                        f"{API}{path}",
                        params=params,
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError:
                log_event("gmail", op=method, outcome="unreachable")
                raise IntegrationError(
                    "Gmail could not be reached"
                    if idempotent
                    else "Gmail did not answer, so the email may or may not have been sent",
                    retryable=idempotent,
                ) from None
            status = response.status_code
            if status < 300:
                try:
                    data = response.json()
                except ValueError:
                    raise IntegrationError("Gmail sent an unreadable reply") from None
                return cast(Json, data) if isinstance(data, dict) else {}
            if status == 401:
                if refreshed:
                    raise ReauthRequired(REAUTH_MESSAGE)
                refreshed = True
                token = await self._token(user_id, rejected=token)
                continue
            reason = _reason(response)
            if status == 403 and reason not in _RATE_REASONS:
                log_event("gmail", op=method, outcome="forbidden")
                raise ReauthRequired(REAUTH_MESSAGE)
            if status == 429 or (status == 403 and reason in _RATE_REASONS):
                wait = _retry_after(response)
                if idempotent and attempt < MAX_RETRIES and wait <= MAX_WAIT_SECONDS:
                    attempt += 1
                    await self._sleep(wait)
                    continue
                log_event("gmail", op=method, outcome="rate_limited")
                raise RateLimited("Gmail is limiting requests right now", retry_after=wait)
            if status >= 500:
                if idempotent and attempt < MAX_RETRIES:
                    attempt += 1
                    await self._sleep(min(_retry_after(response), MAX_WAIT_SECONDS))
                    continue
                log_event("gmail", op=method, outcome="server_error")
                raise IntegrationError(
                    "Gmail is having trouble"
                    if idempotent
                    else "Gmail failed while sending, so the email may or may not have been sent",
                    retryable=idempotent,
                )
            log_event("gmail", op=method, outcome="rejected", status=status)
            raise IntegrationError("Gmail did not accept the request")

    # Reads -------------------------------------------------------------------------------

    async def _summaries(self, user_id: str, ids: list[str]) -> list[EmailSummary]:
        gate = asyncio.Semaphore(CONCURRENCY)

        async def one(message_id: str) -> EmailSummary:
            async with gate:
                message = await self._request(
                    user_id,
                    "GET",
                    f"/messages/{message_id}",
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                )
            return _summary(message)

        return list(await asyncio.gather(*(one(i) for i in ids)))

    async def search(self, user_id: str, query: str, limit: int) -> list[EmailSummary]:
        limit = max(1, min(limit, MAX_LIMIT))
        listing = await self._request(
            user_id, "GET", "/messages", params={"q": query, "maxResults": limit}
        )
        ids = [str(m["id"]) for m in _dicts(listing.get("messages")) if "id" in m]
        return await self._summaries(user_id, ids[:limit])

    async def important(self, user_id: str, limit: int) -> list[EmailSummary]:
        return await self.search(user_id, IMPORTANT_QUERY, limit)

    async def needs_reply(self, user_id: str, limit: int) -> list[EmailSummary]:
        limit = max(1, min(limit, MAX_LIMIT))
        profile = await self._request(user_id, "GET", "/profile")
        me = str(profile.get("emailAddress", "")).lower()
        if not me:
            raise IntegrationError("Gmail did not say which address is signed in")
        listing = await self._request(
            user_id, "GET", "/threads", params={"q": NEEDS_REPLY_QUERY, "maxResults": limit}
        )
        ids = [str(t["id"]) for t in _dicts(listing.get("threads")) if "id" in t][:limit]
        gate = asyncio.Semaphore(CONCURRENCY)

        async def one(thread_id: str) -> EmailSummary | None:
            async with gate:
                thread = await self._request(
                    user_id,
                    "GET",
                    f"/threads/{thread_id}",
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                )
            messages = _dicts(thread.get("messages"))
            if not messages:
                return None
            newest = max(messages, key=lambda m: _received(m))
            summary = _summary(newest, needs_reply=True)
            # The newest message is from someone else, so nothing from me came after it.
            return None if summary.sender_email == me else summary

        found = [s for s in await asyncio.gather(*(one(i) for i in ids)) if s is not None]
        return sorted(found, key=lambda s: s.received, reverse=True)

    async def get(self, user_id: str, message_id: str) -> Email:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", message_id):
            raise IntegrationError("That message id is not valid")
        message = await self._request(
            user_id, "GET", f"/messages/{message_id}", params={"format": "full"}
        )
        payload = _dict(message.get("payload"))
        headers = _headers(payload)
        return Email(
            summary=_summary(message),
            body_text=extract_body(payload),
            to=tuple(a.lower() for _, a in getaddresses([headers.get("to", "")]) if a),
            cc=tuple(a.lower() for _, a in getaddresses([headers.get("cc", "")]) if a),
            message_id_header=headers.get("message-id", ""),
        )

    # Send --------------------------------------------------------------------------------

    async def send(self, user_id: str, mail: OutgoingEmail, idempotency_key: str) -> SentEmail:
        key = f"{user_id}|{idempotency_key}"
        lock = self._send_locks.setdefault(key, asyncio.Lock())
        async with lock:
            earlier = self._sent.get(key)
            if earlier is not None:
                return earlier
            try:
                raw = build_raw(mail)
            except ValueError as exc:
                raise IntegrationError(str(exc)) from None
            body: Json = {"raw": raw}
            if mail.thread_id:
                body["threadId"] = mail.thread_id
            # Never retried: a lost reply could mean the email went out.
            sent = await self._request(
                user_id, "POST", "/messages/send", body=body, idempotent=False
            )
            message_id = str(sent.get("id", ""))
            if not message_id:
                raise IntegrationError("Gmail did not confirm the email, so it may have been sent")
            verified = False
            thread_id = str(sent.get("threadId", mail.thread_id or ""))
            try:
                check = await self._request(
                    user_id, "GET", f"/messages/{message_id}", params={"format": "minimal"}
                )
                verified = "SENT" in _strs(check.get("labelIds"))
                thread_id = str(check.get("threadId", thread_id))
            except IntegrationError:
                log_event("gmail", op="verify", outcome="unreadable")
            result = SentEmail(id=message_id, thread_id=thread_id, verified=verified)
            self._sent[key] = result
            log_event("gmail", op="send", outcome="sent", verified=verified)
            return result


def _reason(response: httpx.Response) -> str:
    try:
        error = _dict(_dict(cast(object, response.json())).get("error"))
    except ValueError:
        return ""
    errors = _dicts(error.get("errors"))
    if errors:
        return str(errors[0].get("reason", ""))
    return str(error.get("status", ""))


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0
