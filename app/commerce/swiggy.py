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
https://mcp.swiggy.com/builders/docs/reference/instamart/get_payment_options.md
https://mcp.swiggy.com/builders/docs/reference/errors.md
Observed live response details are recorded in docs/swiggy-integration-notes.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from uuid import uuid4

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
    CartIssue,
    CartItem,
    CartLine,
    CommerceAuthRequired,
    CommerceError,
    CommerceNotAttempted,
    CommerceRejected,
    CommerceWriteDisabled,
    OrderResult,
    OrderSummary,
    OrderSummaryItem,
    OrderTracking,
    PaymentMethod,
    PaymentOptions,
    Product,
    RejectionKind,
    SearchResult,
    Variant,
    classify_rejection,
    same_address_id,
)
from app.commerce.units import parse_pack_size
from app.observability import log_event
from app.timing import stage

SWIGGY_MCP_URL = "https://mcp.swiggy.com/im"
# https://mcp.swiggy.com/builders/docs/operate/access.md documents
# mcp-staging.swiggy.com/{server}; confirm the Instamart path when access is granted.
SWIGGY_STAGING_MCP_URL = "https://mcp-staging.swiggy.com/im"
ALLOWED_MCP_URLS = frozenset({SWIGGY_MCP_URL, SWIGGY_STAGING_MCP_URL})
_ANY_URL = TypeAdapter(AnyUrl)
OAUTH_REDIRECT_URI = "http://localhost"

# The only tools this adapter may ever call. Address mutation, coupons, and UPI
# confirmation are deliberately absent. clear_cart is only reachable through update_cart
# with an empty list, so it needs the same cart-write switch.
_READ_TOOLS = frozenset(
    {
        "get_addresses",
        "search_products",
        "your_go_to_items",
        "get_cart",
        "get_payment_options",
        "get_orders",
        "track_order",
    }
)
_WRITE_TOOLS = frozenset({"update_cart", "clear_cart", "checkout"})
_TOOL_TIMEOUT_SECONDS = 30.0
_DEFAULT_RETRY_AFTER_SECONDS = 30.0
_CHECKOUT_TIMEOUT_SECONDS = 60.0
_SESSION_IDLE_SECONDS = 120.0
_TOKEN_EXPIRY_MARGIN_SECONDS = 60.0
# httpx2's default read timeout is 5 s, far too short for checkout; the SDK's own
# defaults are 30 s general / 300 s read. Tool calls are bounded by read_timeout_seconds.
_HTTP_TIMEOUT = httpx2.Timeout(30.0, read=90.0)


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


class SwiggyRejected(SwiggyDiagnosticError, CommerceRejected):
    """Swiggy answered and refused. The provider text is classified, never exposed by str()."""

    def __init__(
        self, *, operation: str, stage: str, kind: RejectionKind, provider_message: str = ""
    ) -> None:
        SwiggyDiagnosticError.__init__(
            self,
            operation=operation,
            stage=stage,
            error_type="SwiggyRejected",
            detail=f"Swiggy refused the request ({kind.value})",
        )
        self.kind = kind
        self.provider_message = provider_message


def diagnostic_from_exception(
    operation: str, stage: str, error: Exception
) -> SwiggyDiagnosticError:
    if isinstance(error, SwiggyRejected):
        return error
    nested = _find_diagnostic_error(error)
    if nested is not None:
        if isinstance(nested, SwiggyRejected):
            return nested
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
    try:
        request = getattr(error, "request", None)
    except RuntimeError:  # httpx raises when no request is attached to the error
        return fallback
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
        except (SwiggyDiagnosticError, CommerceAuthRequired):
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
    except (SwiggyDiagnosticError, CommerceAuthRequired):
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

    def __init__(
        self, backend: KeyringBackend | None = None, *, endpoint: str = SWIGGY_MCP_URL
    ) -> None:
        self._backend = backend
        if endpoint != SWIGGY_MCP_URL:  # never reuse production credentials on another server
            self.service_name = "ajji-mart.swiggy-mcp.staging"

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
        # The SDK does not restore token expiry from storage; keep it ourselves.
        lifetime = tokens.expires_in if tokens.expires_in is not None else 0
        await self._set("oauth-token-expires-at", str(time.time() + lifetime))

    async def token_expires_at(self) -> float | None:
        raw = await self._get("oauth-token-expires-at")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

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


@dataclass
class _ToolRequest:
    name: str
    arguments: dict[str, object]
    timeout: float
    future: asyncio.Future[CallToolResult]


class SwiggyMcpClient:
    """One Swiggy MCP server over OAuth 2.1 + PKCE, limited to an allowlist of tools.

    One long-lived MCP session serves all tool calls, as required by
    https://mcp.swiggy.com/builders/docs/operate/rate-limits.md. Its transport
    must be entered and exited by a single task, so a worker task owns it and
    request handlers talk to that worker through a queue.
    """

    allowed_endpoints: frozenset[str] = ALLOWED_MCP_URLS
    read_tools: frozenset[str] = _READ_TOOLS
    write_tools: frozenset[str] = _WRITE_TOOLS

    def __init__(
        self,
        *,
        endpoint: str = SWIGGY_MCP_URL,
        redirect_uri: str,
        storage: TokenStorage,
        redirect_handler: Callable[[str], Awaitable[None]],
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
        allow_real_checkout: bool = False,
        allow_cart_writes: bool | None = None,
    ) -> None:
        if endpoint not in self.allowed_endpoints:
            raise ValueError(f"Swiggy endpoint must be one of {sorted(self.allowed_endpoints)}")
        if redirect_uri != OAUTH_REDIRECT_URI:
            raise ValueError("Local Swiggy OAuth redirect must be http://localhost")
        self.endpoint = endpoint
        self.allow_real_checkout = allow_real_checkout
        self.allow_cart_writes = (
            allow_real_checkout if allow_cart_writes is None else (allow_cart_writes)
        )
        self._storage = storage
        self._available_payment_methods: set[str] = set()
        self._last_cart_address_id: str | None = None
        self._queue: asyncio.Queue[_ToolRequest] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._rate_limited_until = 0.0
        metadata = OAuthClientMetadata(
            client_name="Namma Saathi",
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

    async def aclose(self) -> None:
        """Close the MCP session owned by the worker task, if it is running."""
        worker = self._worker
        self._worker = None
        self._queue = None
        if worker is None or worker.done():
            return
        if self._worker_loop is asyncio.get_running_loop():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def _call(self, name: str, arguments: dict[str, object]) -> Mapping[str, object]:
        data, _ = await self._call_with_message(name, arguments)
        return data

    def _ensure_worker(self) -> asyncio.Queue[_ToolRequest]:
        loop = asyncio.get_running_loop()
        if (
            self._queue is None
            or self._worker is None
            or self._worker.done()
            or self._worker_loop is not loop
        ):
            self._queue = asyncio.Queue()
            self._worker_loop = loop
            self._worker = loop.create_task(self._run_session(self._queue))
        return self._queue

    async def _run_session(self, queue: asyncio.Queue[_ToolRequest]) -> None:
        """Own one MCP session; any failure fails queued calls and ends this session."""
        current: _ToolRequest | None = None
        try:
            async with (
                httpx2.AsyncClient(auth=self._oauth, timeout=_HTTP_TIMEOUT) as client,
                streamable_http_client(self.endpoint, http_client=client) as (
                    read_stream,
                    write_stream,
                ),
                ClientSession(read_stream, write_stream) as session,
            ):
                try:
                    await session.initialize()
                except (SwiggyDiagnosticError, CommerceAuthRequired):
                    raise
                except Exception as exc:
                    raise diagnostic_from_exception(
                        "MCP session", _transport_stage(exc, "MCP initialization"), exc
                    ) from exc
                while True:
                    try:
                        current = await asyncio.wait_for(queue.get(), _SESSION_IDLE_SECONDS)
                    except TimeoutError:
                        return
                    try:
                        result = await session.call_tool(
                            current.name, current.arguments, read_timeout_seconds=current.timeout
                        )
                    except Exception as exc:
                        # The session state is unknown now; report this call and reconnect later.
                        if not current.future.done():
                            current.future.set_exception(exc)
                        current = None
                        return
                    if not current.future.done():
                        current.future.set_result(result)
                    current = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_requests(queue, current, exc)
        finally:
            # Calls never sent are safe to resend; `_send` does that, except for checkout.
            self._fail_requests(queue, current, CommerceError("Swiggy session closed"))

    @staticmethod
    def _fail_requests(
        queue: asyncio.Queue[_ToolRequest], current: _ToolRequest | None, error: Exception
    ) -> None:
        pending = [current] if current is not None else []
        while not queue.empty():
            pending.append(queue.get_nowait())
        for request in pending:
            if not request.future.done():
                request.future.set_exception(error)

    async def _check_token_fresh(self) -> None:
        if not isinstance(self._storage, KeyringTokenStorage):
            return
        expires_at = await self._storage.token_expires_at()
        if expires_at is not None and time.time() >= expires_at - _TOKEN_EXPIRY_MARGIN_SECONDS:
            raise CommerceAuthRequired(
                "Swiggy login is missing or expired. Run `uv run python -m app.swiggy_auth`."
            )

    async def _send(
        self, name: str, arguments: dict[str, object], timeout: float
    ) -> CallToolResult:
        for attempt in (1, 2):
            # A worker can end between calls (idle timeout, prior failure); that is
            # not an outcome of this call, because the request was never sent.
            queue = self._ensure_worker()
            future: asyncio.Future[CallToolResult] = asyncio.get_running_loop().create_future()
            queue.put_nowait(_ToolRequest(name, arguments, timeout, future))
            try:
                return await future
            except CommerceError as exc:
                if str(exc) != "Swiggy session closed" or attempt == 2 or name == "checkout":
                    raise
        raise CommerceError("Swiggy session closed")

    async def _call_with_message(
        self, name: str, arguments: dict[str, object]
    ) -> tuple[Mapping[str, object], str]:
        data, message, _ = await self._invoke(name, arguments)
        return data, message

    async def _invoke(
        self, name: str, arguments: dict[str, object]
    ) -> tuple[Mapping[str, object], str, str]:
        """Call a tool; returns (data, message, text). Some live tools only answer in text."""
        if name not in self.read_tools and name not in self.write_tools:
            raise CommerceNotAttempted(f"Swiggy tool is not allowed: {name}")
        if time.monotonic() < self._rate_limited_until:
            # Nothing is sent, so this is safe even for checkout: no order can result from it.
            log_event("mcp_tool_call", tool=name, status="rate_limit_wait")
            raise CommerceNotAttempted("Swiggy asked us to wait before calling again")
        await self._check_token_fresh()
        correlation_id = uuid4().hex
        started = time.perf_counter()
        status = "error"
        timeout = _CHECKOUT_TIMEOUT_SECONDS if name == "checkout" else _TOOL_TIMEOUT_SECONDS
        try:
            with stage("provider"):
                result = await self._send(name, arguments, timeout)
            data, message = _parse_result(name, result)
            status = "ok"
            return data, message, _result_text(result)
        except SwiggyRejected:
            status = "rejected"
            raise
        except SwiggyDiagnosticError as exc:
            self._note_rate_limit(exc)
            raise diagnostic_from_exception(name, exc.stage, exc) from exc
        except TimeoutError as exc:
            status = "timeout"
            if name == "checkout":
                raise TimeoutError(
                    "Swiggy checkout outcome is unknown; check order status"
                ) from exc
            raise diagnostic_from_exception(name, f"MCP operation {name}", exc) from exc
        except httpx2.TimeoutException as exc:
            status = "timeout"
            if name == "checkout":
                raise TimeoutError(
                    "Swiggy checkout outcome is unknown; check order status"
                ) from exc
            raise diagnostic_from_exception(name, _operation_stage(exc, name), exc) from exc
        except httpx2.RequestError as exc:
            raise diagnostic_from_exception(name, _operation_stage(exc, name), exc) from exc
        except CommerceError:
            raise
        except Exception as exc:
            if _requires_login(exc):
                raise CommerceAuthRequired(
                    "Swiggy login is missing or expired. Run `uv run python -m app.swiggy_auth`."
                ) from exc
            self._note_rate_limit(exc)
            raise diagnostic_from_exception(name, _operation_stage(exc, name), exc) from exc
        finally:
            log_event(
                "mcp_tool_call",
                tool=name,
                correlation_id=correlation_id,
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )

    def _note_rate_limit(self, error: BaseException) -> None:
        wait = _retry_after_seconds(error)
        if wait is not None:
            self._rate_limited_until = time.monotonic() + wait

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


class SwiggyInstamart(SwiggyMcpClient):
    """Instamart behind the domain-level commerce interface."""

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
        if not self.allow_cart_writes:
            raise CommerceWriteDisabled("Changing the real Swiggy cart is disabled")
        if not items:
            return await self._clear_cart()
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
        self._remember_payment_methods(data)
        self._last_cart_address_id = cart.address_id or address_id
        return cart

    async def _clear_cart(self) -> Cart:
        # Live: update_cart with an empty list fails during cancel; clear_cart is the
        # documented way to empty a cart. Only the following read proves it worked.
        await self._call("clear_cart", {})
        cart = await self.get_cart()
        if cart.items:
            raise CommerceError("Swiggy cart was not empty after clear_cart")
        return cart

    async def get_cart(self) -> Cart:
        data = await self._call("get_cart", {})
        self._remember_payment_methods(data)
        cart = _cart(data)
        self._last_cart_address_id = cart.address_id
        return cart

    def _remember_payment_methods(self, data: Mapping[str, object]) -> None:
        names = _payment_method_names(data)
        if names:
            self._available_payment_methods = names

    async def get_payment_options(self) -> PaymentOptions:
        """Live cash-on-delivery availability; live get_cart omits availablePaymentMethods."""
        data = await self._call("get_payment_options", {})
        raw = data.get("cod")
        cod: Mapping[str, object] = (
            cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}
        )
        available = cod.get("available") is True
        cod_id = _optional_str(cod.get("id"))
        if available:
            self._available_payment_methods = {*self._available_payment_methods, cod_id or "COD"}
        else:
            self._available_payment_methods -= {"COD", "Cash", cod_id or "COD"}
        return PaymentOptions(cod_available=available, cod_id=cod_id)

    async def checkout(
        self,
        address_id: str,
        payment_method: PaymentMethod = PaymentMethod.COD,
        confirm_duplicate: bool = False,
    ) -> OrderResult:
        if not self.allow_real_checkout:
            raise CommerceNotAttempted("Real Swiggy checkout is disabled by configuration")
        if confirm_duplicate:
            raise CommerceNotAttempted(
                "Duplicate checkout requires a separate confirmed recovery flow"
            )
        if payment_method is not PaymentMethod.COD:
            raise CommerceNotAttempted("Saathi only places cash-on-delivery orders")
        if not same_address_id(address_id, self._last_cart_address_id):
            raise CommerceNotAttempted(
                "Refresh the cart for the selected delivery address before checkout"
            )
        if not _payment_method_is_available("Cash", self._available_payment_methods):
            raise CommerceNotAttempted(
                "The selected payment method is not available for the current cart"
            )
        # The live checkout description says to pass paymentMethod="Cash" for cash on delivery.
        arguments: dict[str, object] = {"addressId": address_id, "paymentMethod": "Cash"}
        data, message = await self._call_with_message("checkout", arguments)
        return _order_result(data, message)

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


def _result_text(result: CallToolResult) -> str:
    return "\n".join(content.text for content in result.content if isinstance(content, TextContent))


def _operation_stage(error: Exception, name: str) -> str:
    return _transport_stage(error, f"MCP operation {name}")


def _requires_login(error: BaseException) -> bool:
    """Find a login failure inside SDK exception groups or JSON-RPC errors (-32001)."""
    if isinstance(error, CommerceAuthRequired):
        return True
    if isinstance(error, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], error)
        return any(_requires_login(nested) for nested in group.exceptions)
    details = getattr(error, "error", None)
    code = getattr(details, "code", getattr(error, "code", None))
    return code == -32001


def _retry_after_seconds(error: BaseException, seen: int = 0) -> float | None:
    """How long Swiggy asked us to wait, or None if this was not a rate limit.

    https://mcp.swiggy.com/builders/docs/operate/rate-limits.md: the gateway returns 429 with
    Retry-After, which surfaces at the MCP layer as JSON-RPC -32000. Default from its example.
    """
    if seen > 8:
        return None
    if isinstance(error, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], error)
        for nested in group.exceptions:
            wait = _retry_after_seconds(nested, seen + 1)
            if wait is not None:
                return wait
        return None
    if isinstance(error, httpx2.HTTPStatusError) and error.response.status_code == 429:
        try:
            return max(float(error.response.headers.get("retry-after", "")), 0.0)
        except ValueError:
            return _DEFAULT_RETRY_AFTER_SECONDS
    details = getattr(error, "error", None)
    if getattr(details, "code", getattr(error, "code", None)) == -32000:
        return _DEFAULT_RETRY_AFTER_SECONDS
    cause = error.__cause__ or error.__context__
    return _retry_after_seconds(cause, seen + 1) if cause is not None else None


def _provider_error_text(payload: Mapping[str, object]) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = cast(Mapping[str, object], error).get("message")
        if isinstance(message, str):
            return message
    if isinstance(error, str):
        return error
    message = payload.get("message")
    return message if isinstance(message, str) else ""


def _raise_provider_failure(name: str, stage: str, text: str) -> None:
    kind = classify_rejection(text) if text else None
    log_event(
        "provider_failure",
        tool=name,
        kind=kind.value if kind else "unclassified",
        # Swiggy's wording is what support needs; digits are masked in case it quotes an address.
        reason=re.sub(r"\d", "#", text)[:160],
    )
    if kind is not None:
        raise SwiggyRejected(operation=name, stage=stage, kind=kind, provider_message=text)
    cause = CommerceError("Swiggy MCP tool returned an error result")
    raise diagnostic_from_exception(name, stage, cause) from cause


def _parse_result(name: str, result: CallToolResult) -> tuple[Mapping[str, object], str]:
    """Apply the documented envelope; failures keep only a classified kind, not provider text."""
    if result.is_error:
        text = ""
        try:
            text = _provider_error_text(_result_mapping(result))
        except CommerceError:
            for content in result.content:
                if isinstance(content, TextContent):
                    text = content.text
                    break
        _raise_provider_failure(name, f"MCP operation {name}", text)
    response_stage = f"MCP operation {name} response"
    try:
        payload = _result_mapping(result)
    except Exception as exc:
        raise diagnostic_from_exception(name, response_stage, exc) from exc
    if payload.get("success") is False:
        _raise_provider_failure(name, response_stage, _provider_error_text(payload))
    # Live: an empty Food cart is {"data": null, "statusCode": ...}, so the payload itself is used.
    data = payload.get("data")
    if not isinstance(data, Mapping):
        data = payload
    message = _optional_str(payload.get("message")) or ""
    return cast(Mapping[str, object], data), message


def _order_result(data: Mapping[str, object], message: str) -> OrderResult:
    """Map the three documented checkout shapes without losing placed order IDs."""
    status = _optional_str(data.get("status")) or ""
    total = _optional_money(data.get("cartTotal"))
    direct_order_id = data.get("orderId")
    if isinstance(direct_order_id, str) and direct_order_id:
        return OrderResult(
            success=status.upper() != "PENDING_PAYMENT",
            order_ids=[direct_order_id],
            message=message,
            status=status,
            total_inr=total,
        )
    orders = _list(data.get("orders"), "orders", allow_missing=True)
    order_ids = [
        value for value in (_optional_str(item.get("orderId")) for item in orders) if value
    ]
    all_succeeded = data.get("allSucceeded")
    if isinstance(all_succeeded, bool):
        success = all_succeeded and bool(order_ids)
    else:
        success = bool(order_ids) and not any(item.get("error") for item in orders)
    return OrderResult(
        success=success,
        partial=bool(order_ids) and not success,
        order_ids=order_ids,
        message=message,
        status=status,
        total_inr=total,
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


def _optional_money(value: object) -> Decimal | None:
    """Exact rupees and paise from a number or a display string like "₹1,234.50"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            try:
                return Decimal(match.group(0))
            except InvalidOperation:
                return None
    return None


def _money(value: object, label: str) -> Decimal:
    amount = _optional_money(value)
    if amount is None:
        raise CommerceError(f"Missing numeric {label} in Swiggy response")
    return amount


def _products(value: object, *, allow_missing: bool = False) -> list[Product]:
    return [_product(raw) for raw in _list(value, "products", allow_missing=allow_missing)]


def _variant(variation: Mapping[str, object]) -> Variant:
    # Live data: displayName repeats the product name; quantityDescription holds
    # the pack ("500 ml", "1 ltr", "500 ml x 4"). A pack we cannot parse stays None
    # so resolution must ask the user instead of treating it as a match.
    description = _optional_str(variation.get("quantityDescription")) or ""
    label = description or str(variation.get("displayName") or "")
    amount = parse_pack_size(description) if description else None
    return Variant(
        spin_id=_required_str(variation, "spinId"),
        sku_id=_required_str(variation, "skuId"),
        pack_size=label,
        quantity_value=float(amount.value) if amount else None,
        unit=amount.measure if amount else None,
        price_inr=_money(
            _mapping(variation.get("price"), "price").get("offerPrice"),
            "offerPrice",
        ),
        in_stock=variation.get("isInStockAndAvailable") is True,
        max_quantity=_optional_int(variation.get("maxQuantity")),
    )


def _product(raw: Mapping[str, object]) -> Product:
    return Product(
        id=_required_str(raw, "productId"),
        display_name=_required_str(raw, "displayName"),
        brand=str(raw.get("brand") or ""),
        variants=[_variant(variation) for variation in _list(raw.get("variations"), "variations")],
        promoted=raw.get("isPromoted") is True,
    )


def food_or_dining_url(instamart_url: str, server: str) -> str:
    """The same environment's URL for another Swiggy server (/food or /dineout)."""
    return instamart_url.rsplit("/", maxsplit=1)[0] + f"/{server}"


def _item_total_from_bill(breakdown: Mapping[str, object]) -> Decimal | None:
    for line in _list(breakdown.get("lineItems"), "lineItems", allow_missing=True):
        if str(line.get("label") or "").casefold() == "item total":
            return _optional_money(line.get("value"))
    return None


def _unit_prices(
    raw_items: list[Mapping[str, object]], billed_item_total: Decimal | None
) -> list[Decimal]:
    """The docs do not say if discountedFinalPrice is per unit, so check it against the bill."""
    finals = [
        _money(item.get("discountedFinalPrice"), "discountedFinalPrice") for item in raw_items
    ]
    quantities = [_number(item.get("quantity"), "quantity") for item in raw_items]
    per_unit_total = sum(
        (price * qty for price, qty in zip(finals, quantities, strict=True)), Decimal(0)
    )
    if (
        billed_item_total is not None
        and per_unit_total != billed_item_total
        and sum(finals, Decimal(0)) == billed_item_total
    ):
        return [price / qty for price, qty in zip(finals, quantities, strict=True)]
    return finals


def _bill_adjustments(
    breakdown: Mapping[str, object], item_total: Decimal, total: Decimal
) -> tuple[Decimal, Decimal]:
    """(fees, savings) from the bill's lines; live lines include "-₹49.95" and "FREE"."""
    lines = _list(breakdown.get("lineItems"), "lineItems", allow_missing=True)
    fees = savings = Decimal(0)
    for line in lines:
        if str(line.get("label") or "").casefold() == "item total":
            continue
        raw = str(line.get("value") or "")
        amount = _optional_money(raw)
        if amount is None:
            continue
        if raw.strip().startswith("-"):
            savings += amount
        else:
            fees += amount
    if not lines:
        fees = max(total - item_total, Decimal(0))
    return fees, savings


def _cart_issues(
    data: Mapping[str, object], raw_items: list[Mapping[str, object]]
) -> tuple[list[CartIssue], list[str]]:
    issues: list[CartIssue] = []
    errors: list[str] = []

    def record(issue: CartIssue, text: str) -> None:
        if issue not in issues:
            issues.append(issue)
        errors.append(text)

    if data.get("unserviceableItems"):
        record(CartIssue.NOT_SERVICEABLE, "Some cart items are not serviceable at this address")
    if any(item.get("isInStockAndAvailable") is False for item in raw_items):
        record(CartIssue.OUT_OF_STOCK, "Some cart items are out of stock")
    warning = _optional_str(data.get("addressWarning"))
    if warning:
        record(CartIssue.OTHER, warning)
    cart_warning = _mapping_text(data.get("cartWarning"), "") if data.get("cartWarning") else ""
    if cart_warning:
        kind = classify_rejection(cart_warning)
        if kind is RejectionKind.MIN_ORDER:
            record(CartIssue.MIN_ORDER, cart_warning)
        elif kind is RejectionKind.NOT_SERVICEABLE:
            record(CartIssue.NOT_SERVICEABLE, cart_warning)
        elif kind is RejectionKind.OUT_OF_STOCK:
            record(CartIssue.OUT_OF_STOCK, cart_warning)
        else:
            record(CartIssue.OTHER, cart_warning)
    gpo_error = _optional_str(data.get("gpoError"))
    if gpo_error:
        errors.append(gpo_error)  # payment-options fetch trouble; checked via get_payment_options
    return issues, errors


def _cart(data: Mapping[str, object]) -> Cart:
    absent = data.get("cartAbsent") is True
    raw_items = _list(data.get("items"), "items", allow_missing=absent)
    details = data.get("selectedAddressDetails")
    address_details: Mapping[str, object] = (
        _mapping(details, "selectedAddressDetails") if details is not None else {}
    )
    address_id = _optional_str(address_details.get("id"))
    if not raw_items and "billBreakdown" not in data:
        # An empty cart has nothing to bill; the shape of a populated cart stays strict.
        breakdown: Mapping[str, object] = {}
        total = Decimal(0)
    else:
        breakdown = _mapping(data.get("billBreakdown"), "billBreakdown")
        payable = _mapping(breakdown.get("toPay"), "billBreakdown.toPay")
        total = _money(payable.get("value"), "billBreakdown.toPay.value")
    prices = _unit_prices(raw_items, _item_total_from_bill(breakdown))
    items = [
        CartItem(
            spin_id=_required_str(item, "spinId"),
            sku_id=_required_str(item, "skuId"),
            name=_required_str(item, "itemName"),
            pack_size=str(item.get("itemVariant") or ""),
            quantity=_number(item.get("quantity"), "quantity"),
            unit_price_inr=price,
        )
        for item, price in zip(raw_items, prices, strict=True)
    ]
    item_total = sum((item.line_total_inr for item in items), Decimal(0))
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
    fees, savings = _bill_adjustments(breakdown, item_total, total)
    # The flat number is what tells a delivery partner which home this is.
    address_parts = [
        _optional_str(address_details.get("flatNo")),
        _optional_str(address_details.get("address")),
        _optional_str(address_details.get("area")),
    ]
    address_text = ", ".join(part for part in address_parts if part)
    issues, errors = _cart_issues(data, raw_items)
    stores = {item.get("storeId") for item in raw_items if item.get("storeId") is not None}
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
        delivery_fee_inr=fees,
        savings_inr=savings,
        grand_total_inr=total,
        payment_methods=payments,
        errors=errors,
        issues=issues,
        store_count=max(len(stores), 1),
        lat=_optional_float(address_details.get("lat")),
        lng=_optional_float(address_details.get("lng")),
    )


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _order_summary(raw: Mapping[str, object]) -> OrderSummary:
    created = raw.get("createdAt")
    created_at: datetime | None = None
    if isinstance(created, str):
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError:
            created_at = None  # not relied on: reconciliation compares order IDs
    items = _list(raw.get("items"), "items", allow_missing=True)
    return OrderSummary(
        order_id=_required_str(raw, "orderId"),
        created_at=created_at,
        items=[
            OrderSummaryItem(
                name=str(item.get("name") or ""),
                quantity=_optional_int(item.get("quantity")) or 0,
                item_id=_optional_str(item.get("itemId")),
            )
            for item in items
        ],
        total_inr=_optional_money(raw.get("totalAmount")) or Decimal(0),
        status=_optional_str(raw.get("currentStatus")) or _optional_str(raw.get("status")) or "",
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
