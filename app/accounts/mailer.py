"""Sending account email. Without SMTP settings, no mail is sent and the page says so."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import Protocol

from app.config import Settings
from app.observability import log_event


class Mailer(Protocol):
    @property
    def can_send(self) -> bool: ...

    async def send(self, to: str, subject: str, body: str) -> bool: ...


class NoMailer:
    can_send = False

    async def send(self, to: str, subject: str, body: str) -> bool:
        del to, subject, body
        return False


class SmtpMailer:
    """STARTTLS on the configured port; it refuses to send credentials over a plain connection."""

    can_send = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _send_now(self, message: EmailMessage) -> None:
        settings = self._settings
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
            client.starttls()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password or "")
            client.send_message(message)

    async def send(self, to: str, subject: str, body: str) -> bool:
        message = EmailMessage()
        message["From"] = self._settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            await asyncio.to_thread(self._send_now, message)
        except (OSError, smtplib.SMTPException) as exc:
            log_event("mail", outcome="failed", kind=type(exc).__name__)
            return False
        log_event("mail", outcome="sent")
        return True


def build_mailer(settings: Settings) -> Mailer:
    return SmtpMailer(settings) if settings.smtp_host else NoMailer()
