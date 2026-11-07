"""What each connected app is really doing, for the Connected apps screen.

"Connected" is claimed only after asking the service: a stored Google credential must still be able
to get an access token, and the WhatsApp token must be accepted by Meta. A credential that has been
revoked shows as authorization failed, not connected. A service that cannot be reached shows as
unavailable. Nothing here includes a token, a secret, or another person's data.

States: connected, not_connected, configuration_required, authorization_failed, unavailable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.accounts.service import AccountService
from app.accounts.store import User
from app.config import Settings
from app.google.calendar import GoogleCalendarClient
from app.google.gmail import GmailClient
from app.google.oauth import GoogleOAuth
from app.integrations.base import IntegrationError, NotConnected, ReauthRequired
from app.whatsapp.client import WhatsAppClient

NOTE_SECONDS = 24 * 3600


@dataclass
class IntegrationNotes:
    """Why an authorization last failed, kept briefly so the screen can say so. Per person."""

    _notes: dict[tuple[str, str], tuple[str, float]] = field(
        default_factory=dict[tuple[str, str], tuple[str, float]]
    )

    def record(self, user_id: str, service: str, message: str) -> None:
        if len(self._notes) > 5000:
            self._notes.clear()
        self._notes[(user_id, service)] = (message[:200], time.time())

    def get(self, user_id: str, service: str) -> str | None:
        found = self._notes.get((user_id, service))
        if found is None or time.time() - found[1] > NOTE_SECONDS:
            return None
        return found[0]

    def clear(self, user_id: str, service: str) -> None:
        self._notes.pop((user_id, service), None)


GOOGLE_PERMISSIONS = {
    "gmail": [
        "Read your email so Saathi can search, summarise and find what needs a reply",
        "Send email, only after you approve each message",
    ],
    "calendar": [
        "See and change events on your calendar, only after you approve each change",
    ],
}


def _missing_google(settings: Settings) -> list[str]:
    missing: list[str] = []
    if not settings.google_client_id:
        missing.append("GOOGLE_CLIENT_ID")
    if not settings.google_client_secret:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not settings.public_base_url:
        missing.append("PUBLIC_BASE_URL")
    return missing


def _missing_whatsapp(settings: Settings) -> list[str]:
    pairs = (
        ("WHATSAPP_ACCESS_TOKEN", settings.whatsapp_access_token),
        ("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id),
        ("WHATSAPP_APP_SECRET", settings.whatsapp_app_secret),
        ("WHATSAPP_VERIFY_TOKEN", settings.whatsapp_verify_token),
    )
    return [name for name, value in pairs if not value]


def _read_connected_at(accounts: AccountService, user_id: str, service: str) -> int | None:
    with accounts.database.transaction() as tx:
        rows = tx.execute(
            "SELECT updated_at FROM connections WHERE user_id = ? AND provider = ?",
            (user_id, service),
        )
    return int(str(rows[0]["updated_at"])) if rows else None


async def _connected_at(accounts: AccountService, user_id: str, service: str) -> str | None:
    """When this person last connected, as an ISO time, read from their own row only."""
    try:
        stamp = await accounts.run(_read_connected_at, accounts, user_id, service)
    except Exception:
        return None
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp / 1000 if stamp > 10**11 else stamp, UTC).isoformat()


async def _google_card(
    service_name: str,
    title: str,
    description: str,
    user: User,
    accounts: AccountService,
    google: GoogleOAuth,
    notes: IntegrationNotes,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    settings = accounts.settings
    card: dict[str, Any] = {
        "id": service_name,
        "name": title,
        "description": description,
        "permissions": GOOGLE_PERMISSIONS[service_name],
        "account": None,
        "last_connected": None,
        "setup": [],
        "can_connect": False,
        "can_disconnect": False,
        "can_check": True,
    }
    missing = _missing_google(settings)
    if missing:
        card.update(
            state="configuration_required",
            detail="The site's owner needs to set up Google sign-in before this can connect.",
            setup=missing,
            can_check=False,
        )
        return card
    info = (await google.tokens.status(user.id)).get(service_name, {})
    if not info.get("connected"):
        failure = notes.get(user.id, service_name)
        card.update(
            state="authorization_failed" if failure else "not_connected",
            detail=failure
            or "Not connected. Saathi uses clearly marked demo data until you connect.",
            can_connect=True,
        )
        return card
    card.update(
        account=str(info.get("account") or "") or None,
        last_connected=await _connected_at(accounts, user.id, service_name),
        can_connect=False,
        can_disconnect=True,
    )
    try:
        # Ask the service itself. A stored credential, or a cached access token, proves nothing.
        if service_name == "gmail":
            await GmailClient(google.tokens, transport=transport).verify(user.id)
        else:
            await GoogleCalendarClient(google.tokens, transport=transport).verify(user.id)
    except ReauthRequired as exc:
        notes.record(user.id, service_name, str(exc))
        card.update(
            state="authorization_failed",
            detail="Google no longer accepts this connection. Connect again.",
            can_connect=True,
            can_disconnect=False,
            account=None,
        )
    except (IntegrationError, NotConnected):
        card.update(state="unavailable", detail="Couldn't reach Google just now. Try again soon.")
    else:
        notes.clear(user.id, service_name)
        card.update(state="connected", detail="Connected and verified just now.")
    return card


async def _whatsapp_card(settings: Settings, whatsapp: WhatsAppClient | None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": "whatsapp",
        "name": "WhatsApp Business",
        "description": (
            "Sends messages you approve from this site's WhatsApp Business number. "
            "It is not your personal WhatsApp."
        ),
        "permissions": [
            "Send a message from the business number, only after you approve each one",
            "Receive delivery status from Meta",
        ],
        "account": None,
        "last_connected": None,
        "setup": [],
        "can_connect": False,
        "can_disconnect": False,
        "can_check": True,
        "notes": [
            "Free text only works within 24 hours of the person's last message to the number.",
            "A test number can message only the people you have added and verified in Meta.",
        ],
    }
    missing = _missing_whatsapp(settings)
    if missing or whatsapp is None:
        card.update(
            state="configuration_required",
            detail=(
                "The site's owner needs to add WhatsApp Business Cloud API details. "
                "Until then messages are drafts and demo only."
            ),
            setup=missing,
            can_check=False,
        )
        return card
    try:
        last4 = await whatsapp.check()
    except ReauthRequired:
        card.update(
            state="authorization_failed",
            detail="Meta no longer accepts this site's WhatsApp token. The owner must renew it.",
        )
    except (IntegrationError, NotConnected):
        card.update(state="unavailable", detail="Couldn't reach WhatsApp just now.")
    else:
        card.update(
            state="connected",
            detail="Meta accepted this site's WhatsApp credentials just now.",
            account=f"Business number ending {last4}" if last4 else None,
        )
    return card


async def _swiggy_card(user: User, accounts: AccountService) -> dict[str, Any]:
    refusal = await accounts.live_refusal(user)
    connected = await accounts.tokens.connected(user.id)
    card: dict[str, Any] = {
        "id": "swiggy",
        "name": "Swiggy",
        "description": "Groceries (Instamart), food delivery and Dineout table booking.",
        "permissions": [
            "Search, build a basket, and place an order or booking only after you approve it",
        ],
        "account": None,
        "last_connected": None,
        "setup": [],
        "can_connect": False,
        "can_disconnect": False,
        "can_check": False,
    }
    if connected:
        card.update(
            state="connected",
            detail="Your Swiggy account is connected.",
            can_disconnect=True,
        )
    elif refusal:
        card.update(
            state="not_connected",
            detail=f"{refusal} Demo shops work now and are always marked as demo.",
        )
    else:
        card.update(state="not_connected", detail="Not connected.", can_connect=True)
    return card


async def collect(
    user: User,
    accounts: AccountService,
    google: GoogleOAuth,
    whatsapp: WhatsAppClient | None,
    notes: IntegrationNotes,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    return [
        await _google_card(
            "gmail",
            "Gmail",
            "Search, summarise and answer your email.",
            user,
            accounts,
            google,
            notes,
            transport,
        ),
        await _google_card(
            "calendar",
            "Google Calendar",
            "See your day, check availability, and add or move events.",
            user,
            accounts,
            google,
            notes,
            transport,
        ),
        await _whatsapp_card(accounts.settings, whatsapp),
        await _swiggy_card(user, accounts),
    ]
