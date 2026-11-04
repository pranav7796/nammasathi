"""Connect a local Swiggy account and verify read-only Instamart access."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from mcp.shared.auth import AuthorizationCodeResult

from app.commerce.base import CommerceError
from app.commerce.swiggy import (
    OAUTH_REDIRECT_URI,
    SWIGGY_MCP_URL,
    KeyringTokenStorage,
    SwiggyDiagnosticError,
    SwiggyInstamart,
)


async def _show_authorization_url(url: str) -> None:
    print("Open this Swiggy authorization URL in your browser:")
    print(url)


async def read_oauth_redirect_from_input() -> AuthorizationCodeResult:
    redirected_url = input(
        "After Swiggy redirects to http://localhost, paste the full address-bar URL here: "
    )
    parsed = urlsplit(redirected_url.strip())
    if parsed.scheme != "http" or parsed.netloc != "localhost" or parsed.path not in {"", "/"}:
        raise CommerceError("Swiggy OAuth must return to the configured http://localhost URI")
    values = parse_qs(parsed.query, strict_parsing=True)
    code = values.get("code", [""])[0]
    state = values.get("state", [""])[0]
    issuer = values.get("iss", [None])[0]
    if not code or not state:
        raise CommerceError("Swiggy OAuth redirect did not include code and state")
    return AuthorizationCodeResult(code=code, state=state, iss=issuer)


def _adapter() -> SwiggyInstamart:
    return SwiggyInstamart(
        endpoint=SWIGGY_MCP_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(),
        redirect_handler=_show_authorization_url,
        callback_handler=read_oauth_redirect_from_input,
        allow_real_checkout=False,
    )


async def _run() -> None:
    adapter = _adapter()
    addresses = await adapter.get_addresses()
    if not addresses:
        print("Swiggy account connected, but it has no saved delivery addresses.")
        return
    for index, address in enumerate(addresses, start=1):
        print(f"{index}. {address.label}: {address.full_address}")
    selected = input("Choose an address number for read-only checks: ")
    if not selected.isdigit() or not 1 <= int(selected) <= len(addresses):
        raise CommerceError("Choose one of the displayed address numbers")
    address = addresses[int(selected) - 1]

    results = await adapter.search_products(address.id, "milk")
    print(f"Products found at {address.label}:")
    for product in results.products:
        for variant in product.variants:
            availability = "available" if variant.in_stock else "unavailable"
            print(
                f"- {product.display_name}, {variant.pack_size}, "
                f"₹{variant.price_inr} ({availability})"
            )

    cart = await adapter.get_cart()
    print("Current Instamart cart:")
    for item in cart.items:
        print(f"- {item.quantity} x {item.name}, {item.pack_size}: ₹{item.line_total_inr}")
    print(
        "Address: "
        f"{cart.delivery_label or address.label}, "
        f"{cart.delivery_address or address.full_address}"
    )
    print(f"Total: ₹{cart.grand_total_inr}")
    if cart.errors:
        print("Cart notices: " + "; ".join(cart.errors))
    print("Read-only verification complete. Checkout was not called.")


def format_diagnostic(error: Exception) -> str:
    if isinstance(error, SwiggyDiagnosticError):
        fields = [
            f"operation={error.operation}",
            f"stage={error.stage}",
            f"exception={error.error_type}",
        ]
        if error.detail:
            fields.append(f"detail={error.detail}")
    else:
        fields = [
            "operation=Swiggy CLI",
            "stage=local setup",
            f"exception={type(error).__name__}",
        ]
    return "Swiggy diagnostic | " + " | ".join(fields)


def main() -> None:
    try:
        asyncio.run(_run())
    except Exception as exc:
        print(format_diagnostic(exc))


if __name__ == "__main__":
    main()
