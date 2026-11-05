"""A stand-in for Swiggy: OAuth, and MCP over HTTP, with separate state for each account.

The real adapters and the real MCP SDK talk to it unchanged. It follows Swiggy's published contracts
(authenticate.md, delegated-auth.md, errors.md, ship-to-production.md, the tool references) and the
shapes recorded in docs/swiggy-integration-notes.md.

What it proves: our plumbing, safety rules, and isolation. What it cannot prove: how real Swiggy
behaves. It lives under tests/ and is not in the Docker image.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

Json = dict[str, Any]

_PRODUCTS: tuple[Json, ...] = (
    {
        "productId": "p-rice",
        "displayName": "Sona Masoori Rice",
        "brand": "Fresho",
        "variations": [
            {"spinId": "r1", "skuId": "sku-r1", "desc": "1 kg", "price": 85},
            {"spinId": "r5", "skuId": "sku-r5", "desc": "5 kg", "price": 399},
        ],
    },
    {
        "productId": "p-milk",
        "displayName": "Toned Milk",
        "brand": "Nandini",
        "variations": [
            {"spinId": "m5", "skuId": "sku-m5", "desc": "500 ml", "price": 32},
            {"spinId": "m1", "skuId": "sku-m1", "desc": "1 ltr", "price": 62},
        ],
    },
)
_DISHES: tuple[Json, ...] = (
    {
        "menu_item_id": "d-biryani",
        "name": "Chicken Biryani",
        "price": 299,
        "restaurant_id": "r-meghana",
        "restaurant_name": "Meghana Foods",
        "inStock": 1,
    },
    {
        "menu_item_id": "d-dosa",
        "name": "Masala Dosa",
        "price": 110,
        "restaurant_id": "r-mtr",
        "restaurant_name": "MTR",
        "inStock": 1,
    },
)
_RESTAURANTS: tuple[Json, ...] = (
    {"id": "176866", "name": "Toscano", "locality": "Indiranagar", "costForTwo": "₹1800 for two"},
    {"id": "176867", "name": "Olive Bistro", "locality": "Indiranagar", "costForTwo": "₹2200"},
)
_TOOLS: dict[str, tuple[str, ...]] = {
    "im": (
        "get_addresses",
        "search_products",
        "your_go_to_items",
        "get_cart",
        "get_payment_options",
        "get_orders",
        "track_order",
        "update_cart",
        "clear_cart",
        "checkout",
    ),
    "food": (
        "get_addresses",
        "search_menu",
        "get_food_cart",
        "get_payment_options",
        "get_food_orders",
        "track_food_order",
        "update_food_cart",
        "flush_food_cart",
        "place_food_order",
    ),
    "dineout": (
        "search_restaurants_dineout",
        "render_restaurants_dineout",
        "get_available_slots",
        "get_booking_status",
        "book_table",
    ),
}
CODE_LIFETIME_SECONDS = 120


@dataclass
class Account:
    name: str
    address_id: str
    label: str
    line: str
    cart: list[Json] = field(default_factory=list[Json])
    food_cart: list[Json] = field(default_factory=list[Json])
    food_restaurant: Json | None = None
    orders: list[Json] = field(default_factory=list[Json])
    food_orders: list[Json] = field(default_factory=list[Json])
    bookings: dict[str, str] = field(default_factory=dict[str, str])


@dataclass
class Call:
    account: str
    server: str
    tool: str
    arguments: Json


@dataclass
class _Code:
    account: str
    client_id: str
    redirect_uri: str
    challenge: str
    issued: float


def _money(value: int) -> str:
    return f"₹{value}"


class FakeSwiggy:
    def __init__(
        self,
        allowed_redirects: set[str],
        *,
        access_lifetime: int = 432000,
    ) -> None:
        self.allowed_redirects = allowed_redirects
        self.access_lifetime = access_lifetime
        self.accounts = {
            "asha": Account("asha", "asha-home__tok1", "Home", "12 CMH Road, Indiranagar"),
            "ravi": Account("ravi", "ravi-flat__tok2", "Flat", "9 Temple Street, Malleshwaram"),
        }
        self.calls: list[Call] = []
        self.clients: dict[str, set[str]] = {}
        self.registrations = 0
        self.refreshes = 0
        self.logouts: list[str] = []
        self.refresh_down = False
        self._codes: dict[str, _Code] = {}
        self._access: dict[str, str] = {}
        self._refresh: dict[str, str] = {}
        self._failures: dict[str, deque[str]] = {}
        self._sequence = 0
        self.app = self._build()

    # Controls for tests

    def fail_next(self, tool: str, mode: str) -> None:
        """Modes: http_401, http_419, http_429, http_500, lost_after_effect, refuse:<message>."""
        self._failures.setdefault(tool, deque()).append(mode)

    def revoke(self, account: str) -> None:
        """A security event at Swiggy: every token this account has stops working."""
        for table in (self._access, self._refresh):
            for token, owner in list(table.items()):
                if owner == account:
                    del table[token]

    def expire_access_tokens(self, account: str) -> None:
        for token, owner in list(self._access.items()):
            if owner == account:
                del self._access[token]

    def tools_called(self, account: str | None = None) -> list[str]:
        return [c.tool for c in self.calls if account is None or c.account == account]

    # Tokens

    def _issue(self, account: str) -> Json:
        access = f"fake-access-{secrets.token_urlsafe(16)}"
        refresh = f"fake-refresh-{secrets.token_urlsafe(16)}"
        self._access[access] = account
        self._refresh[refresh] = account
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.access_lifetime,
            "scope": "mcp:tools mcp:resources mcp:prompts",
        }

    def _next_id(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}-{self._sequence:05}"

    # Tools

    def _address(self, account: Account) -> Json:
        return {
            "id": account.address_id,
            "addressTag": account.label,
            "addressLine": account.line,
        }

    def _owned(self, account: Account, address_id: object) -> bool:
        return address_id == account.address_id

    def _variant(self, spin_id: object) -> tuple[Json, Json] | None:
        for product in _PRODUCTS:
            for variation in product["variations"]:
                if variation["spinId"] == spin_id:
                    return product, variation
        return None

    def _im_cart(self, account: Account) -> Json:
        if not account.cart:
            return {"cartAbsent": True, "items": []}
        items: list[Json] = []
        item_total = 0
        for line in account.cart:
            found = self._variant(line["spinId"])
            assert found is not None
            product, variation = found
            item_total += variation["price"] * line["quantity"]
            items.append(
                {
                    "spinId": line["spinId"],
                    "skuId": line["skuId"],
                    "itemName": product["displayName"],
                    "itemVariant": variation["desc"],
                    "quantity": line["quantity"],
                    "discountedFinalPrice": variation["price"],
                    "isInStockAndAvailable": True,
                    "storeId": "store-1",
                }
            )
        bare = account.address_id.split("__", maxsplit=1)[0]
        return {
            "selectedAddress": account.label,
            "selectedAddressDetails": {
                "id": bare,
                "category": account.label,
                "address": account.line,
                "lat": 12.97,
                "lng": 77.64,
            },
            "items": items,
            "billBreakdown": {
                "lineItems": [
                    {"label": "Item Total", "value": _money(item_total)},
                    {"label": "Handling Fee", "value": _money(5)},
                ],
                "toPay": {"value": _money(item_total + 5)},
            },
        }

    def _food_cart(self, account: Account) -> Json:
        if not account.food_cart or account.food_restaurant is None:
            return {"data": None, "statusCode": 0}
        items: list[Json] = []
        total = 0
        for line in account.food_cart:
            dish = next(d for d in _DISHES if d["menu_item_id"] == line["menu_item_id"])
            cost = dish["price"] * line["quantity"]
            total += cost
            items.append(
                {
                    "menu_item_id": dish["menu_item_id"],
                    "name": dish["name"],
                    "quantity": line["quantity"],
                    "total": cost,
                    "in_stock": True,
                    "variants": [],
                    "addons": [],
                }
            )
        return {
            "success": True,
            "data": {
                "restaurant": account.food_restaurant,
                "items": items,
                "pricing": {
                    "item_total": total,
                    "delivery_charge": 30,
                    "taxes_and_charges": 10,
                    "to_pay": total + 40,
                },
            },
        }

    def _slots(self, restaurant_id: str, date: str) -> str:
        moment = int(time.mktime(time.strptime(f"{date} 19:00", "%Y-%m-%d %H:%M")))
        return (
            f"Found 3 slot(s) across 1 day(s). Availability per date:\n"
            f"  {date}: Dinner (07:00 PM to 08:00 PM, 3 slots)\n\n"
            f"Restaurant ID: {restaurant_id}\n\n"
            "Booking params per date (slotId/itemId differ by date AND by deal):\n"
            f'  {date} [FREE]: slotId=1, itemId="{restaurant_id}-47409"\n'
            f'  {date} [PAID ₹25 Pre-Book]: slotId=1, itemId="{restaurant_id}-1135"\n\n'
            f"Slots for {date}:\n"
            f"  Dinner: 07:00 PM→{moment}, 07:30 PM→{moment + 1800}, 08:00 PM→{moment + 3600}\n"
        )

    def _run(self, server: str, account: Account, tool: str, args: Json) -> Json | str:
        """A mapping is structured content. A string is a text-only reply, as Dineout's are."""
        if tool == "get_addresses":
            return {
                "success": True,
                "data": {
                    "addresses": [self._address(account)],
                    "resolution": {"defaultAddressId": account.address_id},
                    "pagination": {"hasMore": False, "total": 1},
                },
            }
        if tool == "get_payment_options":
            has_items = account.cart if server == "im" else account.food_cart
            cod = {"available": bool(has_items) or server == "im", "id": "COD"}
            return {"success": True, "data": {"cod": cod}}
        handler = getattr(self, f"_tool_{tool}", None)
        if handler is None:
            return {"success": False, "error": {"message": f"Invalid tool {tool}"}}
        return cast(Json | str, handler(account, args))

    def _tool_search_products(self, account: Account, args: Json) -> Json:
        if not self._owned(account, args.get("addressId")):
            return {"success": False, "error": {"message": "Invalid addressId"}}
        words = str(args.get("query", "")).casefold().split()
        found: list[Json] = []
        for product in _PRODUCTS:
            if any(word in product["displayName"].casefold() for word in words):
                found.append(
                    {
                        "productId": product["productId"],
                        "displayName": product["displayName"],
                        "brand": product["brand"],
                        "variations": [
                            {
                                "spinId": v["spinId"],
                                "skuId": v["skuId"],
                                "displayName": product["displayName"],
                                "quantityDescription": v["desc"],
                                "price": {"offerPrice": v["price"]},
                                "isInStockAndAvailable": True,
                                "maxQuantity": 10,
                            }
                            for v in product["variations"]
                        ],
                    }
                )
        return {"success": True, "data": {"products": found, "nextOffset": None}}

    def _tool_your_go_to_items(self, account: Account, args: Json) -> Json:
        del account, args
        return {"success": True, "data": {"products": []}}

    def _tool_get_cart(self, account: Account, args: Json) -> Json:
        del args
        return {"success": True, "data": self._im_cart(account)}

    def _tool_update_cart(self, account: Account, args: Json) -> Json:
        if not self._owned(account, args.get("selectedAddressId")):
            return {"success": False, "error": {"message": "Invalid selectedAddressId"}}
        lines = cast(list[Json], args.get("items") or [])
        if not lines:
            return {"success": False, "error": {"message": "Missing items"}}
        if any(self._variant(line.get("spinId")) is None for line in lines):
            return {"success": False, "error": {"message": "Invalid spinId"}}
        account.cart = [dict(line) for line in lines]
        return {"success": True, "data": self._im_cart(account)}

    def _tool_clear_cart(self, account: Account, args: Json) -> Json:
        del args
        account.cart = []
        return {"success": True, "data": {}}

    def _tool_checkout(self, account: Account, args: Json) -> Json:
        if not self._owned(account, args.get("addressId")):
            return {"success": False, "error": {"message": "Invalid addressId"}}
        if args.get("paymentMethod") != "Cash":
            return {"success": False, "error": {"message": "Invalid paymentMethod"}}
        if not account.cart:
            return {"success": False, "error": {"message": "Cart is empty"}}
        cart = self._im_cart(account)
        total = Decimal(cart["billBreakdown"]["toPay"]["value"].removeprefix("₹"))
        order_id = self._next_id("IM")
        account.orders.append(
            {
                "orderId": order_id,
                "items": [
                    {"name": i["itemName"], "quantity": i["quantity"], "itemId": i["skuId"]}
                    for i in cart["items"]
                ],
                "totalAmount": int(total),
                "currentStatus": "CONFIRMED",
            }
        )
        account.cart = []
        return {
            "success": True,
            "message": "Order placed",
            "data": {"orderId": order_id, "status": "CONFIRMED", "cartTotal": int(total)},
        }

    def _tool_get_orders(self, account: Account, args: Json) -> Json:
        del args
        return {"success": True, "data": {"orders": list(reversed(account.orders))}}

    def _tool_track_order(self, account: Account, args: Json) -> Json:
        for order in account.orders:
            if order["orderId"] == args.get("orderId"):
                return {
                    "success": True,
                    "data": {
                        "orderId": order["orderId"],
                        "status": {"statusMessage": "On the way", "etaMinutes": 12},
                    },
                }
        return {"success": False, "error": {"message": "Invalid orderId"}}

    def _tool_search_menu(self, account: Account, args: Json) -> Json:
        if not self._owned(account, args.get("addressId")):
            return {"success": False, "error": {"message": "Invalid addressId"}}
        words = str(args.get("query", "")).casefold().split()
        scope = args.get("restaurantIdOfAddedItem")
        items = [
            dict(dish)
            for dish in _DISHES
            if any(word in dish["name"].casefold() for word in words)
            and (not scope or dish["restaurant_id"] == scope)
        ]
        return {"success": True, "data": {"items": items}}

    def _tool_get_food_cart(self, account: Account, args: Json) -> Json:
        del args
        return self._food_cart(account)

    def _tool_update_food_cart(self, account: Account, args: Json) -> Json:
        if not self._owned(account, args.get("addressId")):
            return {"success": False, "error": {"message": "Invalid addressId"}}
        account.food_restaurant = {
            "id": args.get("restaurantId"),
            "name": args.get("restaurantName"),
        }
        account.food_cart = [dict(line) for line in cast(list[Json], args.get("cartItems") or [])]
        return {"success": True, "data": {}}

    def _tool_flush_food_cart(self, account: Account, args: Json) -> Json:
        del args
        account.food_cart = []
        account.food_restaurant = None
        return {"success": True, "data": {}}

    def _tool_place_food_order(self, account: Account, args: Json) -> Json:
        if args.get("paymentMethod") != "Cash" or not account.food_cart:
            return {"success": False, "error": {"message": "Invalid order"}}
        cart = self._food_cart(account)["data"]
        order_id = self._next_id("FD")
        total = cart["pricing"]["to_pay"]
        account.food_orders.append(
            {
                "orderId": order_id,
                "orderedItems": ", ".join(i["name"] for i in cart["items"]),
                "orderTotal": total,
                "orderStatus": "CONFIRMED",
            }
        )
        account.food_cart = []
        account.food_restaurant = None
        return {
            "success": True,
            "data": {"orderId": order_id, "status": "CONFIRMED", "totalAmount": total},
        }

    def _tool_get_food_orders(self, account: Account, args: Json) -> Json:
        del args
        return {"success": True, "data": {"orders": list(reversed(account.food_orders))}}

    def _tool_track_food_order(self, account: Account, args: Json) -> Json:
        del args
        return {"success": True, "data": {"orders": list(reversed(account.food_orders))}}

    def _tool_search_restaurants_dineout(self, account: Account, args: Json) -> str:
        del account, args
        return "\n".join(f"{r['name']} (ID: {r['id']})" for r in _RESTAURANTS)

    def _tool_render_restaurants_dineout(self, account: Account, args: Json) -> Json:
        del account
        wanted = cast(list[str], args.get("restaurantIds") or [])
        return {"restaurants": [r for r in _RESTAURANTS if r["id"] in wanted]}

    def _tool_get_available_slots(self, account: Account, args: Json) -> str:
        del account
        return self._slots(str(args.get("restaurantId")), str(args.get("date")))

    def _tool_book_table(self, account: Account, args: Json) -> Json:
        if not str(args.get("itemId", "")).endswith("-47409"):
            return {"success": False, "error": {"message": "Paid deals are rejected"}}
        order_id = self._next_id("DO")
        account.bookings[order_id] = "CONFIRMED"
        return {"success": True, "data": {"orderId": order_id, "status": "CONFIRMED"}}

    def _tool_get_booking_status(self, account: Account, args: Json) -> Json:
        status = account.bookings.get(str(args.get("orderId")))
        if status is None:
            return {"success": False, "error": {"message": "Invalid orderId"}}
        return {"success": True, "data": {"status": status}}

    # HTTP

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.post("/auth/register")
        async def register(request: Request) -> JSONResponse:
            body = cast(Json, await request.json())
            redirects = set(cast(list[str], body.get("redirect_uris") or []))
            if not redirects or not redirects <= self.allowed_redirects:
                return JSONResponse(status_code=400, content={"error": "invalid_redirect_uri"})
            client_id = f"client-{secrets.token_urlsafe(8)}"
            self.clients[client_id] = redirects
            self.registrations += 1
            return JSONResponse({"client_id": client_id})

        @app.get("/auth/authorize")
        async def authorize(request: Request) -> Response:
            query = request.query_params
            client = self.clients.get(query.get("client_id", ""))
            redirect = query.get("redirect_uri", "")
            if client is None or redirect not in client:
                return HTMLResponse("Bad redirect_uri", status_code=400)
            if query.get("code_challenge_method") != "S256" or not query.get("code_challenge"):
                return HTMLResponse("PKCE S256 is required", status_code=400)
            account = query.get("account")
            if account is None:
                links = "".join(
                    f'<p><a href="?{html.escape(urlencode({**query, "account": name}))}">'
                    f"Continue as {name}</a></p>"
                    for name in self.accounts
                )
                return HTMLResponse(
                    "<!doctype html><title>Fake Swiggy</title><h1>Stand-in Swiggy sign-in</h1>"
                    f"<p>This is a test harness, not Swiggy.</p>{links}"
                )
            code = secrets.token_urlsafe(16)
            self._codes[code] = _Code(
                account,
                query.get("client_id", ""),
                redirect,
                query.get("code_challenge", ""),
                time.time(),
            )
            target = urlencode({"code": code, "state": query.get("state", "")})
            return RedirectResponse(f"{redirect}?{target}", status_code=302)

        @app.post("/auth/token")
        async def token(request: Request) -> JSONResponse:
            kind = request.headers.get("content-type", "")
            is_json = kind.startswith("application/json")
            raw = await request.json() if is_json else dict(await request.form())
            body = cast(Json, raw)
            if body.get("grant_type") == "refresh_token":
                if not is_json:  # the docs specify a JSON body for refresh
                    return JSONResponse(status_code=400, content={"error": "invalid_request"})
                if self.refresh_down:
                    return JSONResponse(status_code=500, content={"error": "server_error"})
                old = str(body.get("refresh_token", ""))
                account = self._refresh.pop(old, None)
                if account is None:
                    return JSONResponse(status_code=400, content={"error": "invalid_grant"})
                self.refreshes += 1
                return JSONResponse(self._issue(account))
            code = self._codes.pop(str(body.get("code", "")), None)
            if code is None or time.time() - code.issued > CODE_LIFETIME_SECONDS:
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
            digest = hashlib.sha256(str(body.get("code_verifier", "")).encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if (
                challenge != code.challenge
                or body.get("redirect_uri") != code.redirect_uri
                or body.get("client_id") != code.client_id
            ):
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
            return JSONResponse(self._issue(code.account))

        @app.post("/auth/logout")
        async def logout(request: Request) -> JSONResponse:
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            account = self._access.get(bearer)
            if account is None:
                return JSONResponse(status_code=401, content={})
            self.logouts.append(account)
            self.revoke(account)
            return JSONResponse({"status": "ok"})

        @app.get("/favicon.ico")
        async def favicon() -> Response:
            return Response(status_code=204)

        @app.api_route("/{server}", methods=["GET", "DELETE"])
        async def not_streaming(server: str) -> Response:
            del server
            return Response(status_code=405)

        @app.post("/{server}")
        async def mcp(server: str, request: Request) -> Response:
            if server not in _TOOLS:
                return Response(status_code=404)
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            name = self._access.get(bearer)
            if name is None:
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
            message = cast(Json, await request.json())
            method = message.get("method")
            request_id = message.get("id")
            if request_id is None:
                return Response(status_code=202)
            if method == "initialize":
                params = cast(Json, message.get("params") or {})
                result: Json = {
                    "protocolVersion": params.get("protocolVersion"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": f"fake-swiggy-{server}", "version": "0"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {"name": tool, "inputSchema": {"type": "object"}} for tool in _TOOLS[server]
                    ]
                }
            elif method == "ping":
                result = {}
            elif method == "tools/call":
                params = cast(Json, message.get("params") or {})
                return self._tool_call(server, name, params, request_id)
            else:
                error = {"code": -32601, "message": "Method not found"}
                return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error})
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

        return app

    def _tool_call(self, server: str, name: str, params: Json, request_id: object) -> Response:
        tool = str(params.get("name"))
        arguments = cast(Json, params.get("arguments") or {})
        if tool not in _TOOLS[server]:
            error = {"code": -32601, "message": "Unknown tool"}
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error})
        account = self.accounts[name]
        self.calls.append(Call(name, server, tool, arguments))
        pending = self._failures.get(tool)
        mode = pending.popleft() if pending else ""
        if mode == "http_401":
            self.expire_access_tokens(name)
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        if mode == "http_419":
            self.revoke(name)
            return JSONResponse(status_code=419, content={"error": "revoked"})
        if mode == "http_429":
            return Response(status_code=429, headers={"Retry-After": "30"})
        if mode == "http_500":
            return JSONResponse(status_code=500, content={"error": "internal"})
        if mode.startswith("refuse:"):
            payload: Json | str = {"success": False, "error": {"message": mode[len("refuse:") :]}}
        else:
            payload = self._run(server, account, tool, arguments)
        if mode == "lost_after_effect":
            # The worst case for an order: it went through and the reply never arrived.
            return JSONResponse(status_code=500, content={"error": "lost"})
        if isinstance(payload, str):
            # Live Dineout: useful text, empty structured content.
            result: Json = {
                "content": [{"type": "text", "text": payload}],
                "structuredContent": {},
                "isError": False,
            }
        else:
            result = {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "structuredContent": payload,
                "isError": False,
            }
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
