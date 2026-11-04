"""Read-only readiness report for a real order. Prints no addresses, names, or tokens.

    uv run python -m app.swiggy_preflight

Only read tools are called. Cart writes and checkout are disabled on the adapter
here regardless of configuration.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.commerce.base import same_address_id
from app.commerce.order_journal import FileOrderJournal, JournalError, unresolved_attempts
from app.commerce.runtime import interactive_login_required, no_interactive_callback
from app.commerce.swiggy import (
    OAUTH_REDIRECT_URI,
    KeyringTokenStorage,
    SwiggyDiagnosticError,
    SwiggyInstamart,
)
from app.config import Settings, load_settings
from app.orchestrator.item_resolution import variant_amount

_SEARCH_TERMS = ("milk", "rice", "ಹಾಲು", "दूध")


class Report:
    def __init__(self) -> None:
        self.blockers = 0

    def check(self, passed: bool, text: str) -> None:
        print(f"[{'ok' if passed else 'BLOCKER'}] {text}")
        self.blockers += 0 if passed else 1

    @staticmethod
    def note(text: str) -> None:
        print(f"[note] {text}")


async def run(settings: Settings, report: Report) -> None:
    adapter = SwiggyInstamart(
        endpoint=settings.swiggy_mcp_url,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(endpoint=settings.swiggy_mcp_url),
        redirect_handler=interactive_login_required,
        callback_handler=no_interactive_callback,
        allow_real_checkout=False,
        allow_cart_writes=False,
    )
    try:
        addresses = await adapter.get_addresses()
        report.check(True, "Swiggy login works")
        report.check(bool(addresses), f"{len(addresses)} saved delivery address(es)")
        if not addresses:
            return
        selected = next((item for item in addresses if item.is_default), addresses[0])
        for term in _SEARCH_TERMS:
            result = await adapter.search_products(selected.id, term)
            variants = [variant for product in result.products for variant in product.variants]
            parsed = sum(variant_amount(variant) is not None for variant in variants)
            report.note(
                f"search {term!r}: {len(result.products)} products, pack sizes "
                f"understood for {parsed}/{len(variants)} variants"
            )
        cart = await adapter.get_cart()
        if cart.items:
            report.note(
                f"cart holds {len(cart.items)} line(s) from {cart.store_count} store(s); "
                "Ajji keeps them and reads them back"
            )
            report.check(
                any(same_address_id(cart.address_id, address.id) for address in addresses),
                "cart address is one of the saved addresses (otherwise empty the basket "
                "in the Swiggy app first)",
            )
            for issue in cart.issues:
                report.check(False, f"cart issue: {issue.value}")
            options = await adapter.get_payment_options()
            report.check(options.cod_available, "cash on delivery is available for this cart")
        else:
            report.note("cart is empty; cash on delivery is checked once it has items")
        orders = await adapter.get_orders(count=20)
        report.check(True, f"order history readable ({len(orders)} recent Instamart orders)")
    except SwiggyDiagnosticError as exc:
        report.check(False, f"{exc.stage}: {exc.detail or exc.error_type}")
    except Exception as exc:
        report.check(False, f"read failed: {type(exc).__name__}")
    finally:
        await adapter.aclose()


def main() -> None:
    load_dotenv()
    settings = load_settings()
    report = Report()
    report.note(
        f"backend={settings.commerce_backend} cart_writes={settings.allow_swiggy_cart_writes} "
        f"real_checkout={settings.allow_real_checkout} endpoint={settings.swiggy_mcp_url}"
    )
    try:
        unresolved = unresolved_attempts(FileOrderJournal(Path(settings.order_journal_path)))
        report.check(not unresolved, f"{len(unresolved)} unresolved checkout attempt(s) in journal")
    except JournalError:
        report.check(False, "order journal cannot be read")
    asyncio.run(run(settings, report))
    print(f"\n{report.blockers} blocker(s). This check placed no order and changed no cart.")
    sys.exit(1 if report.blockers else 0)


if __name__ == "__main__":
    main()
