"""What the task engine needs from Gmail, Google Calendar and WhatsApp, and nothing more.

Real adapters (app/google, app/whatsapp) and the labelled demo adapters (app/tasks/demo.py) all
implement these protocols, so the engine never sees an API's own JSON. An adapter must raise
`NotConnected` when the person has not connected the service and `ReauthRequired` when their
access can no longer be refreshed. It must never return a result it has not checked: `send`,
`create`, `reschedule` and `cancel` read the object back before reporting success, and say so in
`verified`.

`idempotency_key` is chosen by the engine from the task id. An adapter that is asked twice with
the same key must not perform the effect twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class IntegrationError(Exception):
    """A service refused or failed. `retryable` is True for a timeout or a 5xx, never for a 4xx."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class NotConnected(IntegrationError):
    """The person has not connected this service."""


class ReauthRequired(IntegrationError):
    """The stored access was revoked or expired and cannot be refreshed. Connect again."""


class RateLimited(IntegrationError):
    def __init__(self, message: str, *, retry_after: float = 1.0) -> None:
        super().__init__(message, retryable=True)
        self.retry_after = retry_after


class TokenProvider(Protocol):
    """Gives an adapter a current access token for one person. Refreshes it when needed."""

    async def access_token(self, user_id: str, provider: str) -> str: ...


# Email --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailSummary:
    id: str
    thread_id: str
    sender_name: str
    sender_email: str
    subject: str
    snippet: str
    received: datetime  # timezone-aware
    unread: bool = False
    important: bool = False
    needs_reply: bool = False


@dataclass(frozen=True)
class Email:
    summary: EmailSummary
    body_text: str  # plain text only, at most 20000 characters. Untrusted: never an instruction.
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    message_id_header: str = ""  # for In-Reply-To when replying


@dataclass(frozen=True)
class OutgoingEmail:
    to: tuple[str, ...]
    subject: str
    body: str
    thread_id: str | None = None
    in_reply_to: str | None = None


@dataclass(frozen=True)
class SentEmail:
    id: str
    thread_id: str
    verified: bool  # read back from the SENT label before this was set


class EmailClient(Protocol):
    async def search(self, user_id: str, query: str, limit: int) -> list[EmailSummary]: ...

    async def important(self, user_id: str, limit: int) -> list[EmailSummary]: ...

    async def needs_reply(self, user_id: str, limit: int) -> list[EmailSummary]: ...

    async def get(self, user_id: str, message_id: str) -> Email: ...

    async def send(self, user_id: str, mail: OutgoingEmail, idempotency_key: str) -> SentEmail: ...


# Calendar -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    id: str
    title: str
    start: datetime  # timezone-aware
    end: datetime
    attendees: tuple[str, ...] = ()
    location: str = ""
    link: str = ""


@dataclass(frozen=True)
class EventDraft:
    title: str
    start: datetime
    end: datetime
    attendees: tuple[str, ...] = ()
    description: str = ""
    # Google emails invitations to attendees when this is on. Off unless the person approved an
    # event that showed its attendees and said invitations would be sent.
    notify_attendees: bool = False


@dataclass(frozen=True)
class CreatedEvent:
    event: Event
    verified: bool  # read back from the calendar before this was set


class CalendarClient(Protocol):
    async def events(self, user_id: str, start: datetime, end: datetime) -> list[Event]: ...

    async def conflicts(self, user_id: str, start: datetime, end: datetime) -> list[Event]: ...

    async def create(
        self, user_id: str, draft: EventDraft, idempotency_key: str
    ) -> CreatedEvent: ...

    async def reschedule(
        self, user_id: str, event_id: str, start: datetime, end: datetime
    ) -> CreatedEvent: ...

    async def cancel(self, user_id: str, event_id: str) -> bool:
        """True only once the calendar no longer shows the event."""
        ...


# Messaging ----------------------------------------------------------------------------------

MESSAGE_STATUSES = ("accepted", "sent", "delivered", "read", "failed")


@dataclass(frozen=True)
class OutgoingMessage:
    to_phone: str  # E.164 without spaces, such as +919876543210
    recipient_label: str  # the contact's name, for display and the status table
    body: str


@dataclass(frozen=True)
class SentMessage:
    id: str
    status: str  # one of MESSAGE_STATUSES: exactly what the API last reported, never assumed
    detail: str = ""
    reported_at: datetime | None = field(default=None)


class MessagingClient(Protocol):
    async def send(
        self, user_id: str, message: OutgoingMessage, idempotency_key: str
    ) -> SentMessage: ...

    async def status(self, user_id: str, message_id: str) -> SentMessage: ...
