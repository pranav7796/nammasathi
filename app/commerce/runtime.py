"""Construct the commerce adapter and checkout safeguards selected by runtime settings."""

from __future__ import annotations

from pathlib import Path

from mcp.shared.auth import AuthorizationCodeResult

from app.commerce.base import CommerceAdapter, CommerceAuthRequired, CommerceError
from app.commerce.mock import MockInstamart
from app.commerce.order_journal import FileOrderJournal, InMemoryOrderJournal, OrderJournal
from app.commerce.swiggy import (
    OAUTH_REDIRECT_URI,
    SWIGGY_MCP_URL,
    KeyringTokenStorage,
    SwiggyInstamart,
    food_or_dining_url,
)
from app.config import Settings
from app.dining.base import DiningAdapter
from app.dining.mock import MockDineout
from app.dining.swiggy import SwiggyDineout
from app.food.mock import MockFood
from app.food.swiggy import SwiggyFood
from app.orchestrator.checkout import (
    REAL_RECONCILE_DELAYS,
    REAL_REJECTION_DELAYS,
    REAL_VERIFY_DELAYS,
    CheckoutPolicy,
)

_LOGIN_MESSAGE = "Swiggy login is missing or expired. Run `uv run python -m app.swiggy_auth`."


async def interactive_login_required(_: str) -> None:
    raise CommerceAuthRequired(_LOGIN_MESSAGE)


async def no_interactive_callback() -> AuthorizationCodeResult:
    raise CommerceAuthRequired(_LOGIN_MESSAGE)


def commerce_adapter(settings: Settings) -> CommerceAdapter:
    """Mock mode never constructs a Swiggy client, so it cannot touch real account data."""
    if settings.commerce_backend == "mock":
        return MockInstamart()
    if not settings.swiggy_live_access:
        raise CommerceError("Enable SWIGGY_LIVE_ACCESS for local Swiggy account access")
    endpoint = settings.swiggy_mcp_url or SWIGGY_MCP_URL
    return SwiggyInstamart(
        endpoint=endpoint,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(endpoint=endpoint),
        redirect_handler=interactive_login_required,
        callback_handler=no_interactive_callback,
        allow_real_checkout=settings.allow_real_checkout,
        allow_cart_writes=settings.allow_swiggy_cart_writes,
    )


def food_adapter(settings: Settings, address_id: str) -> CommerceAdapter:
    if settings.commerce_backend == "mock":
        return MockFood()
    if not settings.swiggy_live_access:
        raise CommerceError("Enable SWIGGY_LIVE_ACCESS for local Swiggy account access")
    endpoint = food_or_dining_url(settings.swiggy_mcp_url or SWIGGY_MCP_URL, "food")
    return SwiggyFood(
        address_id=address_id,
        endpoint=endpoint,
        redirect_uri=OAUTH_REDIRECT_URI,
        # One Swiggy login works across servers, so the stored login is the Instamart one.
        storage=KeyringTokenStorage(endpoint=settings.swiggy_mcp_url or SWIGGY_MCP_URL),
        redirect_handler=interactive_login_required,
        callback_handler=no_interactive_callback,
        allow_real_checkout=settings.allow_real_checkout,
        allow_cart_writes=settings.allow_swiggy_cart_writes,
    )


def dining_adapter(settings: Settings) -> DiningAdapter:
    if settings.commerce_backend == "mock":
        return MockDineout()
    if not settings.swiggy_live_access:
        raise CommerceError("Enable SWIGGY_LIVE_ACCESS for local Swiggy account access")
    return SwiggyDineout(
        endpoint=food_or_dining_url(settings.swiggy_mcp_url or SWIGGY_MCP_URL, "dineout"),
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(endpoint=settings.swiggy_mcp_url or SWIGGY_MCP_URL),
        redirect_handler=interactive_login_required,
        callback_handler=no_interactive_callback,
        allow_real_checkout=settings.allow_real_booking,
    )


def order_journal(settings: Settings) -> OrderJournal:
    if settings.commerce_backend == "mock":
        return InMemoryOrderJournal()
    return FileOrderJournal(Path(settings.order_journal_path))


def checkout_policy(settings: Settings) -> CheckoutPolicy:
    if settings.commerce_backend == "mock":
        return CheckoutPolicy()
    return CheckoutPolicy(
        max_total_inr=settings.max_real_order_inr,
        max_orders=settings.real_order_limit,
        reconcile_delays=REAL_RECONCILE_DELAYS,
        rejection_delays=REAL_REJECTION_DELAYS,
        verify_delays=REAL_VERIFY_DELAYS,
    )
