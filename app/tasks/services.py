"""Chooses, for one person, real Gmail/Calendar/WhatsApp or the labelled practice versions.

A real adapter is used only when the service is configured on this server AND this person has
connected it. Everything else is practice, and the engine marks it as such. Providers are chosen
one by one: someone with Gmail connected but no calendar still gets a real inbox and a practice
calendar, and the board says which is which.
"""

from __future__ import annotations

from typing import Literal

import httpx

from app.accounts.service import AccountService
from app.google.calendar import GoogleCalendarClient
from app.google.gmail import GmailClient
from app.google.oauth import GoogleOAuth
from app.integrations.base import MessagingClient
from app.memory.service import MemoryService
from app.personal.service import PersonalService
from app.tasks.demo import DemoWorld
from app.tasks.executors import ContactResolver, DemoContacts, ResolvedContact, Services
from app.twilio_messaging import TwilioMessaging
from app.whatsapp.client import WhatsAppClient


class MemoryContacts:
    """Saved contacts, looked up exactly (see MemoryService.resolve_contact)."""

    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    async def resolve(
        self, user_id: str, spoken: str, channel: Literal["email", "phone"]
    ) -> ResolvedContact:
        match = await self._memory.resolve_contact(user_id, spoken)
        if match.kind == "ambiguous":
            return ResolvedContact("ambiguous", candidates=tuple(c.name for c in match.candidates))
        contact = match.contact
        if match.kind != "one" or contact is None:
            return ResolvedContact("none")
        if (channel == "email" and not contact.email) or (channel == "phone" and not contact.phone):
            return ResolvedContact("none")
        return ResolvedContact("one", contact.name, contact.email, contact.phone)


class ServiceHub:
    def __init__(
        self,
        accounts: AccountService,
        google: GoogleOAuth,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` is for tests, which stand in for Google and Meta. Production passes none."""
        self._accounts = accounts
        self._google = google
        self._transport = transport
        self._demo = DemoWorld()
        self._personal = PersonalService(accounts)
        self._contacts: ContactResolver = MemoryContacts(MemoryService(accounts))
        settings = accounts.settings
        # Meta's API when configured, else Twilio's, else the labelled practice version.
        self._whatsapp: MessagingClient | None
        if settings.whatsapp_configured:
            self._whatsapp = WhatsAppClient(settings, accounts, transport=transport)
        elif settings.twilio_can_send:
            self._whatsapp = TwilioMessaging(settings, accounts, transport=transport)
        else:
            self._whatsapp = None

    async def for_user(self, user_id: str) -> Services:
        settings = self._accounts.settings
        connected: dict[str, bool] = {"gmail": False, "calendar": False}
        if settings.google_configured:
            try:
                status = await self._google.status(user_id)
                connected = {
                    name: bool(status.get(name, {}).get("connected")) for name in connected
                }
            except Exception:
                connected = {"gmail": False, "calendar": False}
        demo_email, demo_calendar, demo_messaging = self._demo.of(user_id)
        email = (
            GmailClient(self._google.tokens, transport=self._transport)
            if connected["gmail"]
            else demo_email
        )
        calendar = (
            GoogleCalendarClient(self._google.tokens, transport=self._transport)
            if connected["calendar"]
            else demo_calendar
        )
        messaging = self._whatsapp if self._whatsapp is not None else demo_messaging
        demo = {
            "gmail": not connected["gmail"],
            "calendar": not connected["calendar"],
            "whatsapp": self._whatsapp is None,
            "local": False,
        }
        return Services(
            email=email,
            calendar=calendar,
            messaging=messaging,
            contacts=DemoContacts(
                self._contacts, demo_email=demo["gmail"], demo_phone=demo["whatsapp"]
            ),
            personal=self._personal,
            demo=demo,
        )
