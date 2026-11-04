"""Swiggy Instamart adapter behind the domain-level commerce interface.

Tool names and payload mappings in this module are based on the official
Builders Club references for authentication, Instamart search, cart, checkout,
orders, and tracking. The adapter never exposes MCP response shapes to the
state machine.

References:
https://mcp.swiggy.com/builders/docs/start/authenticate.md
https://mcp.swiggy.com/builders/docs/reference/instamart/get_addresses.md
https://mcp.swiggy.com/builders/docs/reference/instamart/search_products.md
https://mcp.swiggy.com/builders/docs/reference/instamart/get_cart.md
https://mcp.swiggy.com/builders/docs/reference/instamart/update_cart.md
https://mcp.swiggy.com/builders/docs/reference/instamart/checkout.md
https://mcp.swiggy.com/builders/docs/reference/instamart/get_orders.md
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Protocol, cast

import httpx2
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.auth.exceptions import OAuthTokenError
from mcp.client.auth.oauth2 import TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.types import CallToolResult, TextContent
from pydantic import AnyUrl, TypeAdapter

from app.commerce.base import (
    Address,
    Cart,
    CartItem,
    CartLine,
    CommerceAdapter,
    CommerceError,
    OrderResult,
    OrderSummary,
    OrderSummaryItem,
    OrderTracking,
    PaymentMethod,
    Product,
    SearchResult,
    Variant,
)

SWIGGY_MCP_URL = "https://mcp.swiggy.com/im"
_ANY_URL = TypeAdapter(AnyUrl)
OAUTH_REDIRECT_URI = "http://localhost"


class SecureCredentialStoreUnavailable(RuntimeError):
    """No approved OS credential backend is configured."""


class StoredOAuthCredentialInvalid(RuntimeError):
    """An OS-stored OAuth record could not be parsed safely."""


class SwiggyDiagnosticError(CommerceError):
    """Safe, stage-specific diagnostic; the original exception stays chained."""

    def __init__(
        self,
        *,
        operation: str,
        stage: str,
        error_type: str,
        detail: str | None = None,
    ) -> None:
        super().__init__(f"Swiggy {stage} failed")
        self.operation = operation
        self.stage = stage
        self.error_type = error_type
        self.detail = detail


def diagnostic_from_exception(
    operation: str, stage: str, error: Exception
) -> SwiggyDiagnosticError:
    nested = _find_diagnostic_error(error)
    if nested is not None:
        return SwiggyDiagnosticError(
            operation=operation,
            stage=nested.stage,
            error_type=nested.error_type,
            detail=nested.detail,
        )
    detail: str | None = None
    if isinstance(error, SecureCredentialStoreUnavailable):
        detail = "No secure OS credential store is available"
    elif isinstance(error, StoredOAuthCredentialInvalid):
        detail = "Stored OAuth credential record is invalid"
    elif isinstance(error, httpx2.TimeoutException):
        detail = "The network request timed out"
    elif isinstance(error, httpx2.HTTPStatusError):
        detail = f"The server returned HTTP {error.response.status_code}"
    elif isinstance(error, httpx2.RequestError):
        detail = "The network request failed"
    elif isinstance(error, OAuthTokenError):
        detail = "The authorization server rejected the token exchange"
    return SwiggyDiagnosticError(
        operation=operation,
        stage=stage,
        error_type=type(error).__name__,
        detail=detail,
    )


def _find_diagnostic_error(error: BaseException) -> SwiggyDiagnosticError | None:
    if isinstance(error, SwiggyDiagnosticError):
        return error
    if isinstance(error, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], error)
        nested_exceptions = group.exceptions
        for nested in nested_exceptions:
            result = _find_diagnostic_error(nested)
            if result is not None:
                return result
    return None


def _transport_stage(error: Exception, fallback: str) -> str:
    request = getattr(error, "request", None)
    url = getattr(request, "url", None)
    path = getattr(url, "path", "")
    if isinstance(path, str) and path.rstrip("/").endswith("/token"):
        return "authorization-code exchange"
    if isinstance(path, str) and path.rstrip("/").endswith("/register"):
        return "OAuth client registration"
    return fallback


class _OAuthSdkLogSanitizer(logging.Filter):
    """Drop exception text and URLs from SDK logs; the CLI emits safe diagnostics."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "mcp.client.auth.oauth2":
            record.msg = "OAuth SDK detail suppressed"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


logging.getLogger("mcp.client.auth.oauth2").addFilter(_OAuthSdkLogSanitizer())


class _SwiggyOAuthClientProvider(OAuthClientProvider):
    """Add safe stage boundaries around the SDK's callback and token exchange."""

    async def _initialize(self) -> None:
        try:
            await super()._initialize()
        except SwiggyDiagnosticError:
            raise
        except Exception as exc:
            raise diagnostic_from_exception("OAuth", "token storage", exc) from exc

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        try:
            return await super()._perform_authorization_code_grant()
        except SwiggyDiagnosticError:
            raise
        except Exception as exc:
            raise diagnostic_from_exception("OAuth", "authorization callback", exc) from exc

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        try:
            await super()._handle_token_response(response)
        except SwiggyDiagnosticError:
            raise
        except Exception as exc:
            raise diagnostic_from_exception("OAuth", "authorization-code exchange", exc) from exc


async def run_authorization_callback_safely(
    callback: Callable[[], Awaitable[AuthorizationCodeResult]],
) -> AuthorizationCodeResult:
    try:
        return await callback()
    except SwiggyDiagnosticError:
        raise
    except Exception as exc:
        raise diagnostic_from_exception("OAuth", "authorization callback", exc) from exc


class KeyringBackend(Protocol):
    priority: float

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class KeyringTokenStorage:
    """Persist MCP OAuth credentials in the OS credential store, never app files."""

    service_name = "ajji-mart.swiggy-mcp"

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        self._backend = backend

    def _get_backend(self) -> KeyringBackend:
        if self._backend is not None:
            backend = self._backend
        else:
            import keyring

            backend = cast(KeyringBackend, keyring.get_keyring())
        backend_name = f"{type(backend).__module__}.{type(backend).__name__}".casefold()
        insecure_backend = (
            type(backend).__module__.casefold().startswith("keyring.backends.fail")
            or "plaintext" in backend_name
        )
        if backend.priority <= 0 or insecure_backend:
            raise SecureCredentialStoreUnavailable(
                "No secure OS credential store is available for Swiggy OAuth"
            )
        return backend

    async def _get(self, username: str) -> str | None:
        try:
            backend = self._get_backend()
            return backend.get_password(self.service_name, username)
        except Exception as exc:
            raise diagnostic_from_exception("OAuth", "token storage", exc) from exc

    async def _set(self, username: str, value: str) -> None:
        try:
            backend = self._get_backend()
            backend.set_password(self.service_name, username, value)
        except Exception as exc:
            raise diagnostic_from_exception("OAuth", "token storage", exc) from exc

    async def get_tokens(self) -> OAuthToken | None:
        raw = await self._get("oauth-tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate_json(raw)
        except Exception as exc:
            error = StoredOAuthCredentialInvalid("Stored Swiggy OAuth token is invalid")
            raise diagnostic_from_exception("OAuth", "token storage", error) from exc

    async def set_tokens(self, tokens: OAuthToken) -> None:
        await self._set("oauth-tokens", tokens.model_dump_json())

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = await self._get("oauth-client")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(raw)
        except Exception as exc:
            error = StoredOAuthCredentialInvalid(
                "Stored Swiggy OAuth client registration is invalid"
            )
            raise diagnostic_from_exception("OAuth", "token storage", error) from exc

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._set("oauth-client", client_info.model_dump_json())


class SwiggyInstamart(CommerceAdapter):
    """MCP-backed Instamart adapter with OAuth 2.1 + PKCE."""

    def __init__(
        self,
        *,
        endpoint: str = SWIGGY_MCP_URL,
        redirect_uri: str,
        storage: TokenStorage,
        redirect_handler: Callable[[str], Awaitable[None]],
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
        allow_real_checkout: bool = False,
    ) -> None:
        if endpoint != SWIGGY_MCP_URL:
            raise ValueError("Swiggy Instamart endpoint must be https://mcp.swiggy.com/im")
        if redirect_uri != OAUTH_REDIRECT_URI:
            raise ValueError("Local Swiggy OAuth redirect must be http://localhost")
        self.endpoint = endpoint
        self.allow_real_checkout = allow_real_checkout
        self._available_payment_methods: set[str] = set()
        self._last_cart_address_id: str | None = None
        metadata = OAuthClientMetadata(
            client_name="Ajji Mart",
            redirect_uris=[_ANY_URL.validate_python(redirect_uri)],
            scope="mcp:tools",
            token_endpoint_auth_method="none",
            application_type="native",
            grant_types=["authorization_code"],
        )
        self._oauth = _SwiggyOAuthClientProvider(
            endpoint,
            metadata,
            storage,
            redirect_handler=redirect_handler,
            callback_handler=lambda: run_authorization_callback_safely(callback_handler),
        )

    async def _call(self, name: str, arguments: dict[str, object]) -> Mapping[str, object]:
        data, _ = await self._call_with_message(name, arguments)
        return data

    async def _call_with_message(
        self, name: str, arguments: dict[str, object]
    ) -> tuple[Mapping[str, object], str]:
        try:
            async with (
                httpx2.AsyncClient(auth=self._oauth) as client,
                streamable_http_client(self.endpoint, http_client=client) as (
                    read_stream,
                    write_stream,
                ),
                ClientSession(read_stream, write_stream) as session,
            ):
                try:
                    await session.initialize()
                except SwiggyDiagnosticError:
                    raise
                except Exception as exc:
                    raise diagnostic_from_exception(
                        name, _transport_stage(exc, "MCP initialization"), exc
                    ) from exc
                try:
                    result = await session.call_tool(name, arguments)
                except httpx2.TimeoutException as exc:
                    if name == "checkout":
                        raise TimeoutError(
                            "Swiggy checkout outcome is unknown; check order status"
                        ) from exc
                    raise diagnostic_from_exception(name, f"MCP operation {name}", exc) from exc
                except SwiggyDiagnosticError:
                    raise
                except Exception as exc:
                    raise diagnostic_from_exception(name, f"MCP operation {name}", exc) from exc
        except SwiggyDiagnosticError as exc:
            raise diagnostic_from_exception(name, exc.stage, exc) from exc
        except httpx2.TimeoutException as exc:
            if name == "checkout":
                raise TimeoutError(
                    "Swiggy checkout outcome is unknown; check order status"
                ) from exc
            raise diagnostic_from_exception(
                name, _transport_stage(exc, "MCP initialization"), exc
            ) from exc
        except httpx2.RequestError as exc:
            raise diagnostic_from_exception(
                name, _transport_stage(exc, "MCP initialization"), exc
            ) from exc
        except TimeoutError:
            raise
        except Exception as exc:
            raise diagnostic_from_exception(
                name, _transport_stage(exc, "MCP initialization"), exc
            ) from exc
        if result.is_error:
            error = CommerceError("Swiggy MCP tool returned an error result")
            raise diagnostic_from_exception(name, f"MCP operation {name}", error) from error
        try:
            payload = _result_mapping(result)
            if payload.get("success") is False:
                raise CommerceError(f"Swiggy {name} request was rejected")
            data = payload.get("data") if "data" in payload else payload
            if not isinstance(data, Mapping):
                raise CommerceError(f"Invalid data in Swiggy {name} response")
        except Exception as exc:
            raise diagnostic_from_exception(name, f"MCP operation {name} response", exc) from exc
        message = _optional_str(payload.get("message")) or ""
        return cast(Mapping[str, object], data), message

    async def get_addresses(self) -> list[Address]:
        addresses: list[Address] = []
        page = 1
        while True:
            data = await self._call("get_addresses", {"page": page, "pageSize": 10})
            try:
                raw_addresses = _list(data.get("addresses"), "addresses")
                resolution = _mapping(data.get("resolution"), "resolution")
                default_address_id = _optional_str(resolution.get("defaultAddressId"))
                for raw in raw_addresses:
                    addresses.append(
                        Address(
                            id=_required_str(raw, "id"),
                            label=str(
                                raw.get("addressTag") or raw.get("addressCategory") or "Address"
                            ),
                            full_address=_required_str(raw, "addressLine"),
                            is_default=_required_str(raw, "id") == default_address_id,
                        )
                    )
                pagination = _mapping(data.get("pagination"), "pagination")
                if not bool(pagination.get("hasMore")):
                    return addresses
                if not raw_addresses and _optional_int(pagination.get("total")):
                    raise CommerceError("Swiggy returned an empty address page with more results")
            except SwiggyDiagnosticError:
                raise
            except Exception as exc:
                raise diagnostic_from_exception(
                    "get_addresses", "get_addresses response mapping", exc
                ) from exc
            page += 1

    async def search_products(self, address_id: str, query: str, offset: int = 0) -> SearchResult:
        if not query.strip():
            raise CommerceError("Search query cannot be empty")
        data = await self._call(
            "search_products",
            {"query": query, "addressId": address_id, "offset": offset},
        )
        return SearchResult(
            products=_products(data.get("products")),
            similar_products=_products(data.get("similarProducts"), allow_missing=True),
            next_offset=_optional_int(data.get("nextOffset")),
        )

    async def go_to_items(self, address_id: str) -> list[Product]:
        data = await self._call("your_go_to_items", {"addressId": address_id})
        return _products(data.get("products"))

    async def update_cart(self, address_id: str, items: list[CartLine]) -> Cart:
        data = await self._call(
            "update_cart",
            {
                "selectedAddressId": address_id,
                "items": [
                    {"spinId": item.spin_id, "skuId": item.sku_id, "quantity": item.quantity}
                    for item in items
                ],
            },
        )
        cart = _cart(data)
        self._available_payment_methods = _payment_method_names(data)
        self._last_cart_address_id = cart.address_id or address_id
        return cart

    async def get_cart(self) -> Cart:
        data = await self._call("get_cart", {})
        self._available_payment_methods = _payment_method_names(data)
        cart = _cart(data)
        self._last_cart_address_id = cart.address_id
        return cart

    async def checkout(
        self,
        address_id: str,
        payment_method: PaymentMethod = PaymentMethod.COD,
        confirm_duplicate: bool = False,
    ) -> OrderResult:
        if not self.allow_real_checkout:
            raise CommerceError("Real Swiggy checkout is disabled by configuration")
        if confirm_duplicate:
            raise CommerceError("Duplicate checkout requires a separate confirmed recovery flow")
        if address_id != self._last_cart_address_id:
            raise CommerceError(
                "Refresh the cart for the selected delivery address before checkout"
            )
        selected_method = "Cash" if payment_method is PaymentMethod.COD else payment_method.value
        if not _payment_method_is_available(selected_method, self._available_payment_methods):
            raise CommerceError("The selected payment method is not available for the current cart")
        arguments: dict[str, object] = {"addressId": address_id, "paymentMethod": selected_method}
        data, message = await self._call_with_message(
            "checkout",
            arguments,
        )
        direct_order_id = data.get("orderId")
        if isinstance(direct_order_id, str):
            return OrderResult(success=True, order_ids=[direct_order_id], message=message)
        orders = _list(data.get("orders"), "orders", allow_missing=True)
        order_ids = [
            value for value in (_optional_str(item.get("orderId")) for item in orders) if value
        ]
        all_succeeded = data.get("allSucceeded")
        success = bool(all_succeeded) if isinstance(all_succeeded, bool) else bool(order_ids)
        return OrderResult(success=success, order_ids=order_ids, message=message)

    async def get_orders(self, count: int = 10) -> list[OrderSummary]:
        if not 1 <= count <= 20:
            raise CommerceError("Order count must be between 1 and 20")
        data = await self._call("get_orders", {"count": count, "orderType": "INSTAMART"})
        return [_order_summary(raw) for raw in _list(data.get("orders"), "orders")]

    async def track_order(self, order_id: str, lat: float, lng: float) -> OrderTracking:
        data = await self._call("track_order", {"orderId": order_id, "lat": lat, "lng": lng})
        status = _mapping(data.get("status"), "status")
        return OrderTracking(
            order_id=_required_str(data, "orderId"),
            status_message=_required_str(status, "statusMessage"),
            eta_text=_optional_str(status.get("etaText")),
            eta_minutes=_optional_int(status.get("etaMinutes")),
        )


def _result_mapping(result: CallToolResult) -> Mapping[str, object]:
    structured = cast(object, result.structured_content)
    if isinstance(structured, Mapping):
        return cast(Mapping[str, object], structured)
    for content in result.content:
        if isinstance(content, TextContent):
            try:
                decoded = json.loads(content.text)
            except json.JSONDecodeError as exc:
                raise CommerceError("Invalid JSON in Swiggy response") from exc
            if isinstance(decoded, Mapping):
                return cast(Mapping[str, object], decoded)
    raise CommerceError("Missing structured data in Swiggy response")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CommerceError(f"Missing {label} in Swiggy response")
    return cast(Mapping[str, object], value)


def _list(value: object, label: str, *, allow_missing: bool = False) -> list[Mapping[str, object]]:
    if value is None and allow_missing:
        return []
    if not isinstance(value, list):
        raise CommerceError(f"Missing {label} in Swiggy response")
    items = cast(list[object], value)
    return [_mapping(item, label) for item in items]


def _required_str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise CommerceError(f"Missing {key} in Swiggy response")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _number(value: object, label: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            return int(float(match.group(0)))
    raise CommerceError(f"Missing numeric {label} in Swiggy response")


def _products(value: object, *, allow_missing: bool = False) -> list[Product]:
    return [_product(raw) for raw in _list(value, "products", allow_missing=allow_missing)]


def _product(raw: Mapping[str, object]) -> Product:
    variations = _list(raw.get("variations"), "variations")
    return Product(
        id=_required_str(raw, "productId"),
        display_name=_required_str(raw, "displayName"),
        brand=str(raw.get("brand") or ""),
        variants=[
            Variant(
                spin_id=_required_str(variation, "spinId"),
                sku_id=_required_str(variation, "skuId"),
                pack_size=str(
                    variation.get("displayName") or variation.get("quantityDescription") or ""
                ),
                price_inr=_number(
                    _mapping(variation.get("price"), "price").get("offerPrice"),
                    "offerPrice",
                ),
                in_stock=bool(variation.get("isInStockAndAvailable")),
            )
            for variation in variations
        ],
    )


def _cart(data: Mapping[str, object]) -> Cart:
    raw_items = _list(data.get("items"), "items")
    details = data.get("selectedAddressDetails")
    address_details: Mapping[str, object] = (
        _mapping(details, "selectedAddressDetails") if details is not None else {}
    )
    address_id = _optional_str(address_details.get("id"))
    breakdown = _mapping(data.get("billBreakdown"), "billBreakdown")
    payable = _mapping(breakdown.get("toPay"), "billBreakdown.toPay")
    total = _number(payable.get("value"), "billBreakdown.toPay.value")
    items = [
        CartItem(
            spin_id=_required_str(item, "spinId"),
            sku_id=_required_str(item, "skuId"),
            name=_required_str(item, "itemName"),
            pack_size=str(item.get("itemVariant") or ""),
            quantity=_number(item.get("quantity"), "quantity"),
            unit_price_inr=_number(item.get("discountedFinalPrice"), "discountedFinalPrice"),
        )
        for item in raw_items
    ]
    item_total = sum(item.quantity * item.unit_price_inr for item in items)
    payment_names = _payment_method_names(data)
    payments = [
        method
        for name, method in (
            ("Cash", PaymentMethod.COD),
            ("COD", PaymentMethod.COD),
            ("UPI", PaymentMethod.UPI),
            ("SwiggyPay", PaymentMethod.SWIGGY_PAY),
        )
        if name in payment_names
    ]
    address_parts = [
        _optional_str(address_details.get("address")),
        _optional_str(address_details.get("area")),
    ]
    address_text = ", ".join(part for part in address_parts if part)
    errors = [
        value
        for value in (
            _optional_str(data.get("gpoError")),
            _optional_str(data.get("addressWarning")),
            _mapping_text(data.get("cartWarning"), "") if data.get("cartWarning") else None,
        )
        if value
    ]
    if data.get("unserviceableItems"):
        errors.append("Some cart items are not serviceable at this address")
    return Cart(
        address_id=address_id,
        delivery_label=str(
            address_details.get("category")
            or address_details.get("annotation")
            or data.get("selectedAddress")
            or ""
        ),
        delivery_address=address_text or None,
        items=items,
        item_total_inr=item_total,
        delivery_fee_inr=max(total - item_total, 0),
        grand_total_inr=total,
        payment_methods=payments,
        errors=errors,
    )


def _order_summary(raw: Mapping[str, object]) -> OrderSummary:
    created = raw.get("createdAt")
    created_at = datetime.fromisoformat(created) if isinstance(created, str) else None
    items = _list(raw.get("items"), "items")
    return OrderSummary(
        order_id=_required_str(raw, "orderId"),
        created_at=created_at,
        items=[
            OrderSummaryItem(
                name=_required_str(item, "name"),
                quantity=_number(item.get("quantity"), "quantity"),
                item_id=_optional_str(item.get("itemId")),
            )
            for item in items
        ],
        total_inr=_number(raw.get("totalAmount"), "totalAmount"),
        status=_required_str(raw, "currentStatus"),
        delivery_address_id=(
            _optional_str(_mapping(raw.get("deliveryAddress"), "deliveryAddress").get("id"))
            if raw.get("deliveryAddress")
            else None
        ),
    )


def _mapping_text(value: object, fallback: str) -> str:
    if isinstance(value, Mapping):
        message = cast(Mapping[str, object], value).get("message")
        if isinstance(message, str) and message:
            return message
    return fallback


def _payment_method_names(data: Mapping[str, object]) -> set[str]:
    value = data.get("availablePaymentMethods")
    if not isinstance(value, list):
        return set()
    return {item for item in cast(list[object], value) if isinstance(item, str)}


def _payment_method_is_available(method: str, available: set[str]) -> bool:
    if method == "Cash":
        return bool({"Cash", "COD"} & available)
    return method in available
