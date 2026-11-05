"""Run on the day Swiggy grants access, with the deployed server's environment.

    uv run python -m app.activation_check config
    uv run python -m app.activation_check reads --user you@example.com
    uv run python -m app.activation_check staging-journey --user you@example.com

`config` touches nothing outside the database. `reads` makes read-only calls with that person's own
connection, on all three servers. `staging-journey` runs cart, order, and booking on Swiggy's seeded
data, and refuses to run anywhere but the staging address. Nothing here can place a real order.

Output holds counts and tool names only: no addresses, tokens, or item details.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from decimal import Decimal

import httpx2
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.accounts.mailer import build_mailer
from app.accounts.service import AccountService, SwiggyStub, environment_name
from app.accounts.store import User
from app.accounts.swiggy_tokens import DelegatedAuth
from app.commerce.base import CartLine
from app.commerce.order_journal import AttemptStatus, new_attempt, resolve_attempt
from app.commerce.swiggy import (
    SWIGGY_STAGING_MCP_URL,
    SwiggyInstamart,
    food_or_dining_url,
)
from app.config import Settings, load_settings
from app.dining.swiggy import SwiggyDineout
from app.food.swiggy import SwiggyFood
from app.oauth_web import callback_url

Report = Callable[[str, bool, str], None]
_TEST_LATITUDE, _TEST_LONGITUDE = 12.9716, 77.5946  # central Bengaluru
_TIMEOUT = httpx2.Timeout(30.0, read=60.0)


def _reporter() -> tuple[Report, Callable[[], int]]:
    failures: list[str] = []

    def report(name: str, passed: bool, detail: str = "") -> None:
        print(
            f"{'PASS' if passed else 'FAIL'}  {name}{f': {detail}' if detail else ''}", flush=True
        )
        if not passed:
            failures.append(name)

    def finish() -> int:
        print("\nAll checks passed." if not failures else f"\n{len(failures)} checks FAILED.")
        return 1 if failures else 0

    return report, finish


def check_config(settings: Settings, service: AccountService, report: Report) -> None:
    report("multi-user live is on", settings.multi_user_live, "")
    report("server", True, environment_name(settings))
    report("redirect URI to allowlist", bool(settings.public_base_url), callback_url(settings))
    try:
        service.database.ping()
        report("database reachable", True, service.database.dialect)
    except Exception as exc:
        report("database reachable", False, type(exc).__name__)
    if settings.public_deployment:
        report("durable database for the order journal", service.database.dialect == "postgres", "")
    can_mail = service.mailer.can_send
    report("email can be sent", True, "yes" if can_mail else "no: use accounts_admin reset-link")
    report("cart writes", True, "on" if settings.allow_swiggy_cart_writes else "off")
    report("real checkout", True, "on" if settings.allow_real_checkout else "off")
    report("real booking", True, "on" if settings.allow_real_booking else "off")
    if settings.allow_real_checkout:
        limit = settings.max_real_order_inr
        report("order total limit", limit is not None, f"₹{limit}")
        report("orders per user", True, str(settings.real_order_limit))
        report("orders per user per day", settings.real_order_daily_limit is not None, "")
    gemini_or_groq = bool(settings.groq_api_key or settings.gemini_api_key)
    report("an LLM key is set", gemini_or_groq, "")
    report("Gnani key is set", bool(settings.gnani_api_key), "")


async def _tools(
    endpoint: str, auth: httpx2.Auth, transport: httpx2.AsyncBaseTransport | None
) -> set[str]:
    async with (
        httpx2.AsyncClient(auth=auth, timeout=_TIMEOUT, transport=transport) as client,
        streamable_http_client(endpoint, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        return {tool.name for tool in listed.tools}


def _adapters(
    service: AccountService, user: User, *, writes: bool = False
) -> tuple[SwiggyInstamart, SwiggyDineout]:
    settings = service.settings
    transport = service.stub.mcp_transport if service.stub else None
    grocery = SwiggyInstamart(
        endpoint=settings.swiggy_mcp_url,
        delegated_auth=DelegatedAuth(service.tokens, user.id),
        http_transport=transport,
        allow_real_checkout=writes,
        allow_cart_writes=writes,
    )
    dining = SwiggyDineout(
        endpoint=food_or_dining_url(settings.swiggy_mcp_url, "dineout"),
        delegated_auth=DelegatedAuth(service.tokens, user.id),
        http_transport=transport,
        allow_real_checkout=writes,
    )
    return grocery, dining


def _food(
    service: AccountService, user: User, address_id: str, *, writes: bool = False
) -> SwiggyFood:
    return SwiggyFood(
        address_id=address_id,
        endpoint=food_or_dining_url(service.settings.swiggy_mcp_url, "food"),
        delegated_auth=DelegatedAuth(service.tokens, user.id),
        http_transport=service.stub.mcp_transport if service.stub else None,
        allow_real_checkout=writes,
        allow_cart_writes=writes,
    )


async def check_reads(service: AccountService, user: User, report: Report) -> None:
    """Read-only. Stops early when there is no login, because nothing else can work."""
    if not await service.tokens.connected(user.id):
        report("this person has connected Swiggy", False, "connect on the site first")
        return
    report("this person has connected Swiggy", True, "")
    grocery, dining = _adapters(service, user)
    settings = service.settings
    transport = service.stub.mcp_transport if service.stub else None
    auth = DelegatedAuth(service.tokens, user.id)
    try:
        servers = (
            ("Instamart", settings.swiggy_mcp_url),
            ("Food", food_or_dining_url(settings.swiggy_mcp_url, "food")),
            ("Dineout", food_or_dining_url(settings.swiggy_mcp_url, "dineout")),
        )
        needed = {
            "Instamart": SwiggyInstamart.read_tools | SwiggyInstamart.write_tools,
            "Food": SwiggyFood.read_tools | SwiggyFood.write_tools,
            "Dineout": SwiggyDineout.read_tools | SwiggyDineout.write_tools,
        }
        for name, endpoint in servers:
            try:
                available = await _tools(endpoint, auth, transport)
            except Exception as exc:
                report(f"{name}: lists its tools", False, type(exc).__name__)
                continue
            missing = sorted(needed[name] - available)
            report(f"{name}: has every tool Saathi uses", not missing, ", ".join(missing))

        try:
            addresses = await grocery.get_addresses()
        except Exception as exc:
            report("addresses", False, type(exc).__name__)
            return
        report("addresses", bool(addresses), f"{len(addresses)} found")
        if not addresses:
            return
        address = addresses[0].id
        food = _food(service, user, address)

        async def step(name: str, action: Callable[[], Awaitable[object]]) -> None:
            try:
                await action()
            except Exception as exc:
                report(name, False, type(exc).__name__)
                return
            report(name, True, "")

        await step("Instamart: search", lambda: grocery.search_products(address, "milk"))
        await step("Instamart: read cart", grocery.get_cart)
        await step("Instamart: payment options", grocery.get_payment_options)
        await step("Instamart: order history", lambda: grocery.get_orders(count=5))
        await step("Food: search", lambda: food.search_products(address, "biryani"))
        await step("Food: read cart", food.get_cart)
        await step("Food: order history", lambda: food.get_orders(count=5))
        await step(
            "Dineout: search",
            lambda: dining.search("italian", _TEST_LATITUDE, _TEST_LONGITUDE),
        )
        await food.aclose()
    finally:
        await grocery.aclose()
        await dining.aclose()


async def staging_journey(service: AccountService, user: User, report: Report) -> None:
    """Orders on seeded data. Refused anywhere but staging, whatever the flags say."""
    if service.settings.swiggy_mcp_url != SWIGGY_STAGING_MCP_URL:
        report("this is the staging server", False, "refusing to run")
        return
    report("this is the staging server", True, "")
    grocery, dining = _adapters(service, user, writes=True)
    journal = service.journal(user.id)
    try:
        addresses = await grocery.get_addresses()
        address = addresses[0].id
        found = await grocery.search_products(address, "milk")
        product = next(p for p in found.products if any(v.in_stock for v in p.variants))
        variant = next(v for v in product.variants if v.in_stock)
        cart = await grocery.update_cart(
            address, [CartLine(spin_id=variant.spin_id, sku_id=variant.sku_id, quantity=1)]
        )
        report("Instamart: cart write", len(cart.items) == 1, "")
        options = await grocery.get_payment_options()
        report("Instamart: cash on delivery offered", options.cod_available, "")
        await grocery.get_cart()
        before = {order.order_id for order in await grocery.get_orders(count=20)}
        attempt = new_attempt(
            "staging-journey",
            str(cart.grand_total_inr),
            tuple(before),
            ("staging test item",),
        )
        journal.begin(attempt, lambda _: None)
        result = await grocery.checkout(address)
        report("Instamart: order returned an ID", bool(result.order_ids), "")
        history = {order.order_id for order in await grocery.get_orders(count=20)}
        verified = bool(result.order_ids) and set(result.order_ids) <= history
        report("Instamart: order is in history", verified, "")
        resolve_attempt(
            journal,
            attempt,
            AttemptStatus.PLACED if verified else AttemptStatus.UNKNOWN,
            order_ids=tuple(result.order_ids),
            note="staging activation check",
        )

        food = _food(service, user, address, writes=True)
        dishes = await food.search_products(address, "biryani")
        dish = next((p for p in dishes.products if any(v.in_stock for v in p.variants)), None)
        report("Food: a dish to order", dish is not None, "")
        if dish is not None:
            choice = next(v for v in dish.variants if v.in_stock)
            cart = await food.update_cart(
                address, [CartLine(spin_id=choice.spin_id, sku_id=choice.sku_id, quantity=1)]
            )
            report(
                "Food: cart write", len(cart.items) == 1 and cart.grand_total_inr > Decimal(0), ""
            )
            await food.get_payment_options()
            before = {order.order_id for order in await food.get_orders(count=15)}
            attempt = new_attempt(
                "staging-journey",
                str(cart.grand_total_inr),
                tuple(before),
                ("staging test dish",),
                domain="food",
            )
            journal.begin(attempt, lambda _: None)
            placed = await food.checkout(address)
            history = {order.order_id for order in await food.get_orders(count=15)}
            verified = bool(placed.order_ids) and set(placed.order_ids) <= history
            report("Food: order is in history", verified, "")
            resolve_attempt(
                journal,
                attempt,
                AttemptStatus.PLACED if verified else AttemptStatus.UNKNOWN,
                order_ids=tuple(placed.order_ids),
                note="staging activation check",
            )
        await food.aclose()

        restaurants = await dining.search("italian", _TEST_LATITUDE, _TEST_LONGITUDE)
        report("Dineout: restaurants found", bool(restaurants), "")
        if restaurants:
            day = (date.today() + timedelta(days=1)).isoformat()
            slots = await dining.free_slots(restaurants[0].id, day, _TEST_LATITUDE, _TEST_LONGITUDE)
            report("Dineout: free slots found", bool(slots), "")
            if slots:
                attempt = new_attempt("staging-journey", "0", (), ("table",), domain="dining")
                journal.begin(attempt, lambda _: None)
                booking = await dining.book(
                    restaurants[0].id, slots[0], 2, _TEST_LATITUDE, _TEST_LONGITUDE
                )
                status = await dining.status(booking.order_id)
                report("Dineout: booking has a status", bool(status.status), status.status)
                resolve_attempt(
                    journal,
                    attempt,
                    AttemptStatus.PLACED,
                    order_ids=(booking.order_id,),
                    note="staging activation check",
                )
    except Exception as exc:
        report("the journey finished", False, type(exc).__name__)
    finally:
        await grocery.aclose()
        await dining.aclose()


async def _main(args: argparse.Namespace, stub: SwiggyStub | None = None) -> int:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set.")
    service = await AccountService.start(settings, mailer=build_mailer(settings), stub=stub)
    report, finish = _reporter()
    try:
        if args.command == "config":
            check_config(settings, service, report)
            return finish()
        user = service.store.user_by_email(args.user)
        if user is None:
            raise SystemExit("No account with that email.")
        if args.command == "reads":
            await check_reads(service, user, report)
        else:
            await staging_journey(service, user, report)
        return finish()
    finally:
        await service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config")
    commands.add_parser("reads").add_argument("--user", required=True)
    commands.add_parser("staging-journey").add_argument("--user", required=True)
    load_dotenv()
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
