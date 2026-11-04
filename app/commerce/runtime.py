"""Construct the commerce adapter selected by local runtime settings."""

from __future__ import annotations

from mcp.shared.auth import AuthorizationCodeResult

from app.commerce.base import CommerceAdapter, CommerceError
from app.commerce.mock import MockInstamart
from app.commerce.swiggy import (
    OAUTH_REDIRECT_URI,
    SWIGGY_MCP_URL,
    KeyringTokenStorage,
    SwiggyInstamart,
)
from app.config import Settings


async def _interactive_login_required(_: str) -> None:
    raise CommerceError(
        "Swiggy login is missing or expired. Run `uv run python -m app.swiggy_auth`."
    )


async def _no_interactive_callback() -> AuthorizationCodeResult:
    raise CommerceError(
        "Swiggy login is missing or expired. Run `uv run python -m app.swiggy_auth`."
    )


def commerce_adapter(settings: Settings) -> CommerceAdapter:
    if settings.commerce_backend == "mock":
        return MockInstamart()
    if not settings.swiggy_live_access:
        raise CommerceError("Enable SWIGGY_LIVE_ACCESS for local Swiggy account access")
    return SwiggyInstamart(
        endpoint=settings.swiggy_mcp_url or SWIGGY_MCP_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(),
        redirect_handler=_interactive_login_required,
        callback_handler=_no_interactive_callback,
        allow_real_checkout=settings.allow_real_checkout,
    )
