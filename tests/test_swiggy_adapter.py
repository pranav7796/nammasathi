from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, cast

import pytest
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.exceptions import OAuthTokenError
from mcp.shared.auth import AuthorizationCodeResult, OAuthToken
from mcp.types import TextContent

from app.commerce.base import (
    Cart,
    CartIssue,
    CartLine,
    CommerceAuthRequired,
    CommerceError,
    CommerceNotAttempted,
    CommerceRejected,
    CommerceWriteDisabled,
    OrderResult,
    PaymentMethod,
    RejectionKind,
)
from app.commerce.swiggy import (
    OAUTH_REDIRECT_URI,
    SWIGGY_MCP_URL,
    SWIGGY_STAGING_MCP_URL,
    KeyringTokenStorage,
    SecureCredentialStoreUnavailable,
    SwiggyDiagnosticError,
    SwiggyInstamart,
    diagnostic_from_exception,
    run_authorization_callback_safely,
)
from app.swiggy_auth import format_diagnostic, main, read_oauth_redirect_from_input


class FakeKeyring:
    priority = 1.0

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


class FailingKeyring(FakeKeyring):
    priority = 0.0


class FailSafeSecretServiceKeyring(FakeKeyring):
    priority = 5.0
    __module__ = "keyring.backends.SecretService"


def _adapter(*, checkout: bool = False, cart_writes: bool = True) -> SwiggyInstamart:
    return SwiggyInstamart(
        endpoint=SWIGGY_MCP_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(FakeKeyring()),
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
        allow_real_checkout=checkout,
        allow_cart_writes=cart_writes,
    )


async def _no_redirect(_: str) -> None:
    return None


async def _no_callback():
    raise AssertionError("OAuth callback is not expected in this test")


def test_keyring_token_storage_round_trips_credentials_without_repr_leak() -> None:
    backend = FakeKeyring()
    storage = KeyringTokenStorage(backend)
    token = OAuthToken(access_token="mock-access-token", expires_in=432000)

    asyncio.run(storage.set_tokens(token))
    restored = asyncio.run(storage.get_tokens())

    assert restored == token
    assert "mock-access-token" not in repr(storage)
    backend.set_password(
        storage.service_name,
        "oauth-tokens",
        '{"access_token":"sensitive-fragment","expires_in":"invalid"}',
    )
    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(storage.get_tokens())
    assert "sensitive-fragment" not in str(error.value)
    assert error.value.stage == "token storage"
    with pytest.raises(SwiggyDiagnosticError) as storage_error:
        asyncio.run(KeyringTokenStorage(FailingKeyring()).get_tokens())
    assert storage_error.value.error_type == "SecureCredentialStoreUnavailable"
    assert storage_error.value.__cause__ is not None
    assert isinstance(storage_error.value.__cause__, SecureCredentialStoreUnavailable)


def test_secretservice_compatible_priority_five_backend_is_accepted() -> None:
    backend = FailSafeSecretServiceKeyring()
    storage = KeyringTokenStorage(backend)
    token = OAuthToken(access_token="mock-secretservice-token", expires_in=300)

    asyncio.run(storage.set_tokens(token))

    assert asyncio.run(storage.get_tokens()) == token
    assert backend.priority == 5.0


def test_authorization_callback_failure_is_staged_and_sanitized() -> None:
    async def failing_callback() -> AuthorizationCodeResult:
        raise RuntimeError("code=private-code state=private-state")

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(run_authorization_callback_safely(failing_callback))

    assert error.value.stage == "authorization callback"
    assert error.value.error_type == "RuntimeError"
    assert isinstance(error.value.__cause__, RuntimeError)
    assert "private-code" not in format_diagnostic(error.value)
    assert "private-state" not in format_diagnostic(error.value)


def test_token_exchange_failure_preserves_cause_without_exposing_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_exchange(self: OAuthClientProvider, response: Any) -> None:
        del self, response
        raise OAuthTokenError("provider body access_token=secret-access-token")

    monkeypatch.setattr(OAuthClientProvider, "_handle_token_response", fail_exchange)
    adapter = _adapter()
    provider = cast(Any, adapter)._oauth

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(provider._handle_token_response(None))

    assert error.value.stage == "authorization-code exchange"
    assert error.value.error_type == "OAuthTokenError"
    assert isinstance(error.value.__cause__, OAuthTokenError)
    assert "secret-access-token" not in format_diagnostic(error.value)


def test_oauth_sdk_log_sanitizer_removes_exception_body_and_traceback() -> None:
    logger = logging.getLogger("mcp.client.auth.oauth2")
    record = logging.LogRecord(
        logger.name,
        logging.ERROR,
        __file__,
        1,
        "OAuth flow error: %s",
        ("access_token=secret-provider-body",),
        None,
    )
    try:
        raise RuntimeError("access_token=secret-provider-body")
    except RuntimeError:
        record.exc_info = sys.exc_info()

    assert logger.filter(record)
    assert record.getMessage() == "OAuth SDK detail suppressed"
    assert record.exc_info is None
    assert "secret-provider-body" not in record.getMessage()


def test_cli_prints_sanitized_stage_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail_run() -> None:
        cause = SecureCredentialStoreUnavailable("secret=do-not-print")
        raise diagnostic_from_exception("get_addresses", "token storage", cause) from cause

    monkeypatch.setattr("app.swiggy_auth._run", fail_run)

    main()

    output = capsys.readouterr().out
    assert "operation=get_addresses" in output
    assert "stage=token storage" in output
    assert "exception=SecureCredentialStoreUnavailable" in output
    assert "No secure OS credential store is available" in output
    assert "do-not-print" not in output


def test_nested_async_exception_group_keeps_the_original_failure_stage() -> None:
    storage_cause = SecureCredentialStoreUnavailable("sensitive cause text")
    storage_error = diagnostic_from_exception("OAuth", "token storage", storage_cause)
    grouped = ExceptionGroup("async transport failure", [RuntimeError("wrapper"), storage_error])

    diagnostic = diagnostic_from_exception("get_addresses", "MCP initialization", grouped)

    assert diagnostic.operation == "get_addresses"
    assert diagnostic.stage == "token storage"
    assert diagnostic.error_type == "SecureCredentialStoreUnavailable"
    assert diagnostic.detail == "No secure OS credential store is available"


def test_oauth_provider_uses_official_local_redirect_and_scope() -> None:
    adapter = _adapter()
    metadata = cast(Any, adapter)._oauth.context.client_metadata

    assert adapter.endpoint == SWIGGY_MCP_URL
    assert metadata.redirect_uris is not None
    redirect_uri = metadata.redirect_uris[0]
    assert redirect_uri.host == "localhost"
    assert redirect_uri.path in {"", "/"}
    assert metadata.scope == "mcp:tools"
    assert metadata.application_type == "native"
    assert metadata.grant_types == ["authorization_code"]


def test_oauth_callback_accepts_only_localhost_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_input(prompt: str) -> str:
        del prompt
        return "http://localhost/?code=one-time-code&state=state-value"

    monkeypatch.setattr("builtins.input", fake_input)

    result = asyncio.run(read_oauth_redirect_from_input())

    assert result.code == "one-time-code"
    assert result.state == "state-value"
    assert result.iss is None


def test_oauth_callback_rejects_external_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_input(prompt: str) -> str:
        del prompt
        return "https://attacker.example/?code=x&state=y"

    monkeypatch.setattr("builtins.input", fake_input)

    with pytest.raises(CommerceError, match="localhost"):
        asyncio.run(read_oauth_redirect_from_input())


class FakeMcpResult:
    def __init__(self, payload: object, *, is_error: bool = False) -> None:
        self.structured_content = payload
        self.is_error = is_error
        self.content: list[object] = []


class FakeMcpSession:
    def __init__(
        self,
        result: FakeMcpResult,
        calls: list[dict[str, object]],
        *,
        initialize_error: Exception | None = None,
        tool_error: Exception | None = None,
        tool_error_name: str | None = "checkout",
    ) -> None:
        self.result = result
        self.calls = calls
        self.initialize_error = initialize_error
        self.tool_error = tool_error
        self.tool_error_name = tool_error_name

    async def __aenter__(self) -> FakeMcpSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def initialize(self) -> None:
        if self.initialize_error is not None:
            raise self.initialize_error
        return None

    async def call_tool(
        self, name: str, arguments: dict[str, object], *, read_timeout_seconds: float
    ) -> FakeMcpResult:
        assert read_timeout_seconds > 5, "the 5 s default would turn slow checkouts into unknowns"
        self.calls.append({"name": name, "arguments": arguments})
        if self.tool_error is not None and name == self.tool_error_name:
            raise self.tool_error
        return self.result


class FakeAsyncContext:
    async def __aenter__(self) -> FakeAsyncContext:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


class FakeStreamContext:
    async def __aenter__(self) -> tuple[object, object]:
        return object(), object()

    async def __aexit__(self, *args: object) -> None:
        del args


def _patch_mcp(
    monkeypatch: pytest.MonkeyPatch,
    result: FakeMcpResult,
    *,
    initialize_error: Exception | None = None,
    tool_error: Exception | None = None,
    tool_error_name: str | None = "checkout",
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_async_client(**kwargs: object) -> FakeAsyncContext:
        del kwargs
        return FakeAsyncContext()

    def fake_streamable_http_client(endpoint: str, *, http_client: object) -> FakeStreamContext:
        del endpoint, http_client
        return FakeStreamContext()

    def fake_client_session(read_stream: object, write_stream: object) -> FakeMcpSession:
        del read_stream, write_stream
        return FakeMcpSession(
            result,
            calls,
            initialize_error=initialize_error,
            tool_error=tool_error,
            tool_error_name=tool_error_name,
        )

    monkeypatch.setattr(
        "app.commerce.swiggy.httpx2.AsyncClient",
        fake_async_client,
    )
    monkeypatch.setattr(
        "app.commerce.swiggy.streamable_http_client",
        fake_streamable_http_client,
    )
    monkeypatch.setattr(
        "app.commerce.swiggy.ClientSession",
        fake_client_session,
    )
    return calls


def test_mcp_call_uses_official_endpoint_tool_name_and_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "success": True,
        "data": {
            "addresses": [{"id": "addr-1", "addressLine": "12 Example Road", "addressTag": "Home"}],
            "pagination": {"hasMore": False, "total": 1},
            "resolution": {
                "defaultAddressId": "addr-1",
                "resolutionSource": "ranked_saved_address",
            },
        },
    }
    calls = _patch_mcp(monkeypatch, FakeMcpResult(data))

    addresses = asyncio.run(_adapter().get_addresses())

    assert addresses[0].id == "addr-1"
    assert addresses[0].full_address == "12 Example Road"
    assert calls == [{"name": "get_addresses", "arguments": {"page": 1, "pageSize": 10}}]


def test_get_addresses_accepts_direct_tool_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    direct_payload = {
        "addresses": [{"id": "addr-1", "addressLine": "12 Example Road", "addressTag": "Home"}],
        "pagination": {"hasMore": False, "total": 1},
        "resolution": {"defaultAddressId": "addr-1"},
        "total": 1,
        "imWidgetV2Eligible": True,
    }
    calls = _patch_mcp(monkeypatch, FakeMcpResult(direct_payload))

    addresses = asyncio.run(_adapter().get_addresses())

    assert len(addresses) == 1
    assert addresses[0].id == "addr-1"
    assert calls == [{"name": "get_addresses", "arguments": {"page": 1, "pageSize": 10}}]


def test_empty_direct_payload_is_not_rejected_for_missing_data_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mcp(monkeypatch, FakeMcpResult({}))
    adapter = cast(Any, _adapter())

    payload, message = asyncio.run(adapter._call_with_message("get_cart", {}))

    assert payload == {}
    assert message == ""


def test_search_products_maps_documented_variant_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "success": True,
        "data": {
            "products": [
                {
                    "productId": "product-1",
                    "displayName": "Nandini Milk",
                    "brand": "Nandini",
                    "inStock": True,
                    "isAvail": True,
                    "variations": [
                        {
                            "spinId": "spin-1",
                            "skuId": "sku-1",
                            "quantityDescription": "500 ml",
                            "displayName": "500 ml",
                            "price": {"mrp": 32, "offerPrice": 28},
                            "isInStockAndAvailable": True,
                        }
                    ],
                }
            ],
            "nextOffset": "10",
        },
    }
    calls = _patch_mcp(monkeypatch, FakeMcpResult(data))

    result = asyncio.run(_adapter().search_products("addr-1", "milk"))

    assert result.products[0].variants[0].sku_id == "sku-1"
    assert result.products[0].variants[0].price_inr == 28
    assert result.next_offset == 10
    assert calls[0] == {
        "name": "search_products",
        "arguments": {"query": "milk", "addressId": "addr-1", "offset": 0},
    }


def _cart_payload() -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "selectedAddressDetails": {
                "id": "addr-1",
                "address": "12 Example Road",
                "area": "Indiranagar",
                "category": "Home",
            },
            "cartTotalAmount": "70",
            "items": [
                {
                    "spinId": "spin-1",
                    "skuId": "sku-1",
                    "productId": "product-1",
                    "itemName": "Nandini Milk",
                    "itemVariant": "500 ml",
                    "quantity": 2,
                    "isInStockAndAvailable": True,
                    "mrp": 32,
                    "discountedFinalPrice": 28,
                }
            ],
            "billBreakdown": {
                "lineItems": [{"label": "Delivery fee", "value": "₹14"}],
                "toPay": {"label": "To pay", "value": "₹70"},
            },
            "availablePaymentMethods": ["Cash"],
        },
    }


def test_update_cart_uses_documented_full_replace_arguments_and_maps_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))

    cart = asyncio.run(
        _adapter().update_cart("addr-1", [CartLine(spin_id="spin-1", sku_id="sku-1", quantity=2)])
    )

    assert cart.address_id == "addr-1"
    assert cart.delivery_address == "12 Example Road, Indiranagar"
    assert cart.item_total_inr == 56
    assert cart.delivery_fee_inr == 14
    assert cart.grand_total_inr == 70
    assert cart.payment_methods == [PaymentMethod.COD]
    assert calls[0] == {
        "name": "update_cart",
        "arguments": {
            "selectedAddressId": "addr-1",
            "items": [{"spinId": "spin-1", "skuId": "sku-1", "quantity": 2}],
        },
    }


@pytest.mark.parametrize(
    ("result", "tool_error", "error_stage"),
    [
        (FakeMcpResult(_cart_payload(), is_error=True), None, "MCP operation update_cart"),
        (FakeMcpResult(None), None, "MCP operation update_cart response"),
    ],
)
def test_update_cart_mcp_error_or_malformed_response_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    result: FakeMcpResult,
    tool_error: Exception | None,
    error_stage: str,
) -> None:
    calls = _patch_mcp(monkeypatch, result, tool_error=tool_error, tool_error_name="update_cart")

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(
            _adapter().update_cart(
                "addr-1", [CartLine(spin_id="spin-1", sku_id="sku-1", quantity=2)]
            )
        )

    assert error.value.stage == error_stage
    assert [call["name"] for call in calls] == ["update_cart"]
    assert calls[0]["arguments"] == {
        "selectedAddressId": "addr-1",
        "items": [{"spinId": "spin-1", "skuId": "sku-1", "quantity": 2}],
    }


def test_update_cart_network_timeout_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    calls = _patch_mcp(
        monkeypatch,
        FakeMcpResult(_cart_payload()),
        tool_error=httpx2.ReadTimeout("private transport detail"),
        tool_error_name="update_cart",
    )

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(
            _adapter().update_cart(
                "addr-1", [CartLine(spin_id="spin-1", sku_id="sku-1", quantity=2)]
            )
        )

    assert error.value.error_type == "ReadTimeout"
    assert error.value.operation == "update_cart"
    assert len(calls) == 1
    assert "private transport detail" not in str(error.value)


def test_get_cart_uses_empty_arguments_and_current_cart_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))

    cart = asyncio.run(_adapter().get_cart())

    assert cart.items[0].name == "Nandini Milk"
    assert cart.grand_total_inr == 70
    assert calls == [{"name": "get_cart", "arguments": {}}]


def test_mcp_domain_error_fails_without_exposing_provider_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_mcp(
        monkeypatch,
        FakeMcpResult({"success": False, "error": {"message": "Address not serviceable"}}),
    )

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(_adapter().get_cart())
    assert error.value.stage == "MCP operation get_cart response"
    assert "Address not serviceable" not in str(error.value)
    assert calls == [{"name": "get_cart", "arguments": {}}]


def test_mcp_is_error_result_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult({}, is_error=True))

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(_adapter().get_addresses())

    assert error.value.stage == "MCP operation get_addresses"
    assert calls == [{"name": "get_addresses", "arguments": {"page": 1, "pageSize": 10}}]


def test_malformed_mcp_response_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    populated = {"items": [cast(dict[str, Any], _cart_payload()["data"])["items"][0]]}
    _patch_mcp(monkeypatch, FakeMcpResult({"success": True, "data": populated}))

    with pytest.raises(CommerceError, match="billBreakdown"):
        asyncio.run(_adapter().get_cart())


def test_missing_structured_and_text_content_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mcp(monkeypatch, FakeMcpResult(None))

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(_adapter().get_addresses())

    assert error.value.stage == "MCP operation get_addresses response"


def test_authentication_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mcp(
        monkeypatch,
        FakeMcpResult({}),
        initialize_error=RuntimeError("401 access_token=secret-value"),
    )

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(_adapter().get_cart())
    assert error.value.stage == "MCP initialization"
    assert error.value.error_type == "RuntimeError"
    assert "secret-value" not in format_diagnostic(error.value)
    assert isinstance(error.value.__cause__, SwiggyDiagnosticError)


def test_get_addresses_failure_is_staged_and_does_not_print_sensitive_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mcp(
        monkeypatch,
        FakeMcpResult({}),
        tool_error=RuntimeError("provider response had customer address and token=private"),
        tool_error_name="get_addresses",
    )

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(_adapter().get_addresses())

    assert error.value.operation == "get_addresses"
    assert error.value.stage == "MCP operation get_addresses"
    assert error.value.error_type == "RuntimeError"
    assert "customer address" not in format_diagnostic(error.value)
    assert "private" not in format_diagnostic(error.value)


def test_token_endpoint_transport_failure_is_classified_without_url_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    token_request = httpx2.Request(
        "POST", "https://mcp.swiggy.com/token?code=private-code&state=private-state"
    )
    _patch_mcp(
        monkeypatch,
        FakeMcpResult({}),
        initialize_error=httpx2.ConnectError("access_token=private-token", request=token_request),
    )

    with pytest.raises(SwiggyDiagnosticError) as error:
        asyncio.run(_adapter().get_addresses())

    assert error.value.stage == "authorization-code exchange"
    assert error.value.error_type == "ConnectError"
    output = format_diagnostic(error.value)
    assert "private-code" not in output
    assert "private-state" not in output
    assert "private-token" not in output


def test_checkout_fails_closed_without_explicit_live_checkout_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    adapter = _adapter(checkout=False)
    asyncio.run(adapter.get_cart())

    with pytest.raises(CommerceError, match="disabled by configuration"):
        asyncio.run(adapter.checkout("addr-1", PaymentMethod.COD))

    assert [call["name"] for call in calls] == ["get_cart"]


def test_live_checkout_requires_fresh_cart_and_available_payment_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    adapter = _adapter(checkout=True)

    with pytest.raises(CommerceError, match="Refresh the cart"):
        asyncio.run(adapter.checkout("addr-1", PaymentMethod.COD))
    assert calls == []


def test_checkout_rejects_automatic_duplicate_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    adapter = _adapter(checkout=True)
    asyncio.run(adapter.get_cart())

    with pytest.raises(CommerceError, match="separate confirmed recovery flow"):
        asyncio.run(adapter.checkout("addr-1", confirm_duplicate=True))
    assert [call["name"] for call in calls] == ["get_cart"]


def test_checkout_timeout_is_not_retried_and_reports_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    calls = _patch_mcp(
        monkeypatch,
        FakeMcpResult(_cart_payload()),
        tool_error=httpx2.ReadTimeout("provider timed out"),
    )
    adapter = _adapter(checkout=True)
    asyncio.run(adapter.get_cart())

    with pytest.raises(TimeoutError, match="outcome is unknown"):
        asyncio.run(adapter.checkout("addr-1", PaymentMethod.COD))
    assert [call["name"] for call in calls] == ["get_cart", "checkout"]


# ------------------------------------------------- shapes observed from the live server
def _live_search_payload() -> dict[str, object]:
    def variation(spin: str, pack: str, price: object = 58) -> dict[str, object]:
        return {
            "spinId": spin,
            "skuId": f"sku-{spin}",
            "displayName": "Amul Gold Pasteurised Full Cream Milk",  # live: repeats the product
            "quantityDescription": pack,
            "brandName": "Amul",
            "price": {"mrp": 60, "offerPrice": price},
            "isInStockAndAvailable": True,
            "maxQuantity": 6,
        }

    return {
        "success": True,
        "data": {
            "nextOffset": "20",
            "products": [
                {
                    "productId": "p1",
                    "displayName": "Amul Gold Pasteurised Full Cream Milk",
                    "brand": "Amul",
                    "isPromoted": True,
                    "variations": [
                        variation("a4", "500 ml x 4", 118),
                        variation("a1", "500 ml", 30.5),
                        variation("odd", "family bottle"),
                    ],
                }
            ],
        },
    }


def _live_cart_payload() -> dict[str, object]:
    """get_cart as observed: no availablePaymentMethods, multiple fees, paise, coordinates."""
    return {
        "success": True,
        "data": {
            "selectedAddress": "Home",
            "selectedAddressDetails": {
                "id": "addr-1",
                "address": "12 Example Road",
                "area": "Indiranagar",
                "category": "Home",
                "lat": 12.97,
                "lng": 77.64,
            },
            "cartId": "cart-1",
            "cartTotalAmount": "₹87",
            "items": [
                {
                    "spinId": "s1",
                    "skuId": "k1",
                    "productId": "p1",
                    "itemName": "Amul Milk",
                    "itemVariant": "500 ml",
                    "quantity": 2,
                    "isInStockAndAvailable": True,
                    "mrp": 32,
                    "discountedFinalPrice": 30,
                    "storeId": 111,
                }
            ],
            "billBreakdown": {
                "lineItems": [
                    {"label": "Item Total", "value": "₹60"},
                    {"label": "Handling Fee", "value": "₹9.50"},
                    {"label": "Delivery Partner Fee", "value": "₹14"},
                    {"label": "Rain Fee", "value": "₹3"},
                ],
                "toPay": {"label": "To Pay", "value": "₹86.50"},
            },
        },
    }


def test_live_variant_uses_quantity_description_and_parses_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mcp(monkeypatch, FakeMcpResult(_live_search_payload()))

    product = asyncio.run(_adapter().search_products("addr-1", "milk")).products[0]

    four, one, odd = product.variants
    assert four.pack_size == "500 ml x 4"
    assert (four.quantity_value, four.unit) == (2000.0, "ml")
    assert four.max_quantity == 6
    assert (one.quantity_value, one.unit) == (500.0, "ml")
    assert str(one.price_inr) == "30.5", "paise are kept"
    assert (odd.quantity_value, odd.unit) == (None, None), "unknown sizes are not guessed"
    assert product.promoted is True


def test_live_cart_maps_fees_paise_coordinates_and_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mcp(monkeypatch, FakeMcpResult(_live_cart_payload()))

    cart = asyncio.run(_adapter().get_cart())

    assert str(cart.grand_total_inr) == "86.50"
    assert cart.items[0].unit_price_inr == 30
    assert cart.item_total_inr == 60
    assert str(cart.delivery_fee_inr) == "26.50"
    assert (cart.lat, cart.lng) == (12.97, 77.64)
    assert cart.store_count == 1
    assert cart.issues == []


def test_line_total_style_price_is_detected_from_the_bill(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _live_cart_payload()
    data = cast(dict[str, Any], payload["data"])
    data["items"][0]["discountedFinalPrice"] = 60  # 2 packs billed as one line total
    _patch_mcp(monkeypatch, FakeMcpResult(payload))

    cart = asyncio.run(_adapter().get_cart())

    assert cart.items[0].unit_price_inr == 30
    assert cart.item_total_inr == 60


def test_multi_store_cart_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _live_cart_payload()
    data = cast(dict[str, Any], payload["data"])
    data["items"].append({**data["items"][0], "spinId": "s2", "skuId": "k2", "storeId": 222})
    _patch_mcp(monkeypatch, FakeMcpResult(payload))

    assert asyncio.run(_adapter().get_cart()).store_count == 2


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("unserviceableItems", [{"spinId": "x"}], CartIssue.NOT_SERVICEABLE),
        ("addressWarning", "Delivery may not reach you", CartIssue.OTHER),
        ("cartWarning", {"statusCode": 1, "message": "Minimum order not met"}, CartIssue.MIN_ORDER),
    ],
)
def test_cart_warnings_become_blocking_issues(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object, issue: CartIssue
) -> None:
    payload = _live_cart_payload()
    cast(dict[str, Any], payload["data"])[field] = value
    _patch_mcp(monkeypatch, FakeMcpResult(payload))

    assert issue in asyncio.run(_adapter().get_cart()).issues


def test_out_of_stock_cart_line_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _live_cart_payload()
    cast(dict[str, Any], payload["data"])["items"][0]["isInStockAndAvailable"] = False
    _patch_mcp(monkeypatch, FakeMcpResult(payload))

    assert CartIssue.OUT_OF_STOCK in asyncio.run(_adapter().get_cart()).issues


@pytest.mark.parametrize("data", [{"cartAbsent": True}, {"items": []}])
def test_empty_cart_shapes_read_as_empty(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, object]
) -> None:
    _patch_mcp(monkeypatch, FakeMcpResult({"success": True, "data": data}))

    cart = asyncio.run(_adapter().get_cart())

    assert cart.items == []
    assert cart.grand_total_inr == 0


def test_live_bill_separates_savings_fees_and_free_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observed live: fees and a discount together made the total less than the items."""
    payload = _live_cart_payload()
    data = cast(dict[str, Any], payload["data"])
    data["billBreakdown"] = {
        "lineItems": [
            {"label": "Item Total", "value": "₹60.00"},
            {"label": "Extra Offer Savings", "value": "-₹9.95"},
            {"label": "Handling Fee", "value": "₹5.00"},
            {"label": "Delivery Partner Fee", "value": "FREE"},
        ],
        "toPay": {"label": "To Pay", "value": "₹55"},
    }
    _patch_mcp(monkeypatch, FakeMcpResult(payload))

    cart = asyncio.run(_adapter().get_cart())

    assert cart.grand_total_inr == 55
    assert cart.delivery_fee_inr == 5
    assert str(cart.savings_inr) == "9.95"


def test_flat_number_is_part_of_the_spoken_address(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _live_cart_payload()
    cast(dict[str, Any], payload["data"])["selectedAddressDetails"]["flatNo"] = "1004"
    _patch_mcp(monkeypatch, FakeMcpResult(payload))

    assert asyncio.run(_adapter().get_cart()).delivery_address == (
        "1004, 12 Example Road, Indiranagar"
    )


def test_emptying_the_cart_uses_clear_cart_and_proves_it(monkeypatch: pytest.MonkeyPatch) -> None:
    empty: dict[str, object] = {"success": True, "data": {"cartAbsent": True}}
    calls = _scripted(
        monkeypatch,
        clear_cart=FakeMcpResult({"success": True, "data": {"verified": True}}),
        get_cart=FakeMcpResult(empty),
    )

    cart = asyncio.run(_adapter().update_cart("addr-1", []))

    assert cart.items == []
    assert [call["name"] for call in calls] == ["clear_cart", "get_cart"]


def test_cart_still_full_after_clear_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(
        monkeypatch,
        clear_cart=FakeMcpResult(
            {"success": True, "data": {"verified": None, "acknowledged": True}}
        ),
        get_cart=FakeMcpResult(_live_cart_payload()),
    )

    with pytest.raises(CommerceError, match="not empty"):
        asyncio.run(_adapter().update_cart("addr-1", []))


def test_clearing_needs_the_cart_write_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult({}))

    with pytest.raises(CommerceWriteDisabled):
        asyncio.run(_adapter(cart_writes=False).update_cart("addr-1", []))

    assert calls == []


def test_cart_writes_are_off_unless_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    adapter = SwiggyInstamart(
        endpoint=SWIGGY_MCP_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(FakeKeyring()),
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
    )

    with pytest.raises(CommerceWriteDisabled):
        asyncio.run(adapter.update_cart("addr-1", [CartLine(spin_id="s", sku_id="k", quantity=1)]))

    assert calls == [], "nothing was sent"


@pytest.mark.parametrize(
    "tool", ["delete_address", "create_address", "confirm_order", "apply_coupon"]
)
def test_tools_outside_the_allowlist_cannot_be_called(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult({}))
    adapter = cast(Any, _adapter(checkout=True))

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(adapter._call_with_message(tool, {}))

    assert calls == []


def test_tool_calls_share_one_mcp_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docs: "Never create a new initialize handshake per tool invocation"."""
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    sessions: list[object] = []
    real = FakeMcpSession.initialize

    async def counting(self: FakeMcpSession) -> None:
        sessions.append(self)
        await real(self)

    monkeypatch.setattr(FakeMcpSession, "initialize", counting)
    adapter = _adapter()

    async def several() -> None:
        await adapter.get_cart()
        await adapter.get_cart()
        await adapter.get_cart()
        await adapter.aclose()

    asyncio.run(several())

    assert len(calls) == 3
    assert len(sessions) == 1


def test_session_reconnects_after_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(
        monkeypatch,
        FakeMcpResult(_cart_payload()),
        tool_error=RuntimeError("stream closed"),
        tool_error_name="search_products",
    )
    adapter = _adapter()

    async def scenario() -> Cart:
        with pytest.raises(SwiggyDiagnosticError):
            await adapter.search_products("addr-1", "milk")
        return await adapter.get_cart()

    assert asyncio.run(scenario()).grand_total_inr == 70
    assert [call["name"] for call in calls] == ["search_products", "get_cart"]


def test_idle_session_closes_and_the_next_call_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    monkeypatch.setattr("app.commerce.swiggy._SESSION_IDLE_SECONDS", 0.01)
    adapter = _adapter()

    async def scenario() -> None:
        await adapter.get_cart()
        await asyncio.sleep(0.05)
        await adapter.get_cart()
        await asyncio.wait_for(adapter.aclose(), 1)

    asyncio.run(asyncio.wait_for(scenario(), 2))

    assert len(calls) == 2


def test_calls_queued_while_the_session_closes_never_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(
        monkeypatch,
        FakeMcpResult(_cart_payload()),
        tool_error=RuntimeError("stream closed"),
        tool_error_name="search_products",
    )
    adapter = _adapter()

    async def scenario() -> tuple[object, object]:
        return await asyncio.gather(
            adapter.search_products("addr-1", "milk"),
            adapter.get_cart(),
            return_exceptions=True,
        )

    failed, cart = asyncio.run(asyncio.wait_for(scenario(), 2))

    assert isinstance(failed, SwiggyDiagnosticError)
    assert isinstance(cart, Cart), "the unsent call was resent on a new session"
    assert [call["name"] for call in calls] == ["search_products", "get_cart"]


def test_checkout_that_may_have_been_sent_is_never_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(
        monkeypatch,
        get_cart=FakeMcpResult(_live_cart_payload()),
        get_payment_options=FakeMcpResult(_payment_payload(True)),
    )
    original = _ScriptedSession.call_tool

    async def lost(
        self: _ScriptedSession,
        name: str,
        arguments: dict[str, object],
        *,
        read_timeout_seconds: float,
    ) -> FakeMcpResult:
        if name == "checkout":
            calls.append({"name": name, "arguments": arguments})
            raise RuntimeError("connection lost mid-request")
        return await original(self, name, arguments, read_timeout_seconds=read_timeout_seconds)

    monkeypatch.setattr(_ScriptedSession, "call_tool", lost)
    adapter = _adapter(checkout=True)

    async def scenario() -> None:
        await adapter.get_cart()
        await adapter.get_payment_options()
        await adapter.checkout("addr-1")

    with pytest.raises(CommerceError):
        asyncio.run(scenario())
    assert [call["name"] for call in calls].count("checkout") == 1


def test_http_client_uses_long_enough_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))

    def capture(**kwargs: object) -> FakeAsyncContext:
        seen.update(kwargs)
        return FakeAsyncContext()

    monkeypatch.setattr("app.commerce.swiggy.httpx2.AsyncClient", capture)
    asyncio.run(_adapter().get_cart())

    timeout = cast(Any, seen["timeout"])
    assert timeout.read >= 60


# ------------------------------------------------- COD is verified, not assumed
def _payment_payload(available: bool, cod_id: str = "COD") -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "cod": {"available": available, "id": cod_id, "displayName": "Pay on delivery"},
            "allMethods": [],
            "agenticPaymentEligible": False,
            "placeOrderToolName": "checkout",
        },
    }


class _ScriptedSession(FakeMcpSession):
    responses: dict[str, FakeMcpResult] = {}  # noqa: RUF012

    async def call_tool(
        self, name: str, arguments: dict[str, object], *, read_timeout_seconds: float
    ) -> FakeMcpResult:
        await super().call_tool(name, arguments, read_timeout_seconds=read_timeout_seconds)
        return self.responses[name]


def _scripted(
    monkeypatch: pytest.MonkeyPatch, **responses: FakeMcpResult
) -> list[dict[str, object]]:
    calls = _patch_mcp(monkeypatch, FakeMcpResult({}))
    _ScriptedSession.responses = responses

    def client_session(read_stream: object, write_stream: object) -> FakeMcpSession:
        del read_stream, write_stream
        return _ScriptedSession(FakeMcpResult({}), calls)

    monkeypatch.setattr("app.commerce.swiggy.ClientSession", client_session)
    return calls


def _checkout_with(
    monkeypatch: pytest.MonkeyPatch, checkout: dict[str, object], *, cod: bool = True
) -> tuple[OrderResult, list[dict[str, object]]]:
    calls = _scripted(
        monkeypatch,
        get_cart=FakeMcpResult(_live_cart_payload()),
        get_payment_options=FakeMcpResult(_payment_payload(cod)),
        checkout=FakeMcpResult(checkout),
    )
    adapter = _adapter(checkout=True)

    async def scenario() -> OrderResult:
        await adapter.get_cart()
        await adapter.get_payment_options()
        return await adapter.checkout("addr-1", PaymentMethod.COD)

    return asyncio.run(scenario()), calls


def test_checkout_works_when_live_cart_omits_payment_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _checkout_with(
        monkeypatch,
        {
            "success": True,
            "message": "Instamart order placed successfully",
            "data": {
                "orderId": "O1",
                "status": "PLACED",
                "paymentMethod": "COD",
                "cartTotal": 86.5,
            },
        },
    )

    assert result.success is True
    assert result.order_ids == ["O1"]
    assert result.message == "Instamart order placed successfully"
    assert str(result.total_inr) == "86.5"
    assert calls[-1] == {
        "name": "checkout",
        "arguments": {"addressId": "addr-1", "paymentMethod": "Cash"},
    }


def test_checkout_is_refused_locally_when_cod_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(
        monkeypatch,
        get_cart=FakeMcpResult(_live_cart_payload()),
        get_payment_options=FakeMcpResult(_payment_payload(False)),
    )
    adapter = _adapter(checkout=True)

    async def scenario() -> None:
        await adapter.get_cart()
        assert (await adapter.get_payment_options()).cod_available is False
        await adapter.checkout("addr-1", PaymentMethod.COD)

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(scenario())
    assert "checkout" not in [call["name"] for call in calls]


def test_only_cash_on_delivery_can_be_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    adapter = _adapter(checkout=True)
    asyncio.run(adapter.get_cart())

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(adapter.checkout("addr-1", PaymentMethod.UPI))
    assert [call["name"] for call in calls] == ["get_cart"]


def test_pending_payment_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _checkout_with(
        monkeypatch,
        {"success": True, "data": {"orderId": "O1", "status": "PENDING_PAYMENT"}},
    )
    assert result.success is False
    assert result.order_ids == ["O1"]


def test_partial_multi_store_result_keeps_the_placed_order(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _checkout_with(
        monkeypatch,
        {
            "success": True,
            "data": {
                "orders": [{"orderId": "A", "status": "PLACED"}, {"error": "store closed"}],
                "orderCount": 2,
                "successCount": 1,
                "failureCount": 1,
                "allSucceeded": False,
            },
        },
    )
    assert result.success is False
    assert result.partial is True
    assert result.order_ids == ["A"]


def test_full_multi_store_result_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _checkout_with(
        monkeypatch,
        {
            "success": True,
            "data": {"orders": [{"orderId": "A"}, {"orderId": "B"}], "allSucceeded": True},
        },
    )
    assert result.success is True
    assert result.partial is False
    assert result.order_ids == ["A", "B"]


# ------------------------------------------------- provider failures stay meaningful
@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Minimum order not met", RejectionKind.MIN_ORDER),
        ("Item out of stock at this address", RejectionKind.OUT_OF_STOCK),
        ("Address not serviceable", RejectionKind.NOT_SERVICEABLE),
        ("Cart expired", RejectionKind.CART_EXPIRED),
        ("Probable duplicate order", RejectionKind.DUPLICATE),
        ("Swiggy Money balance is too low", RejectionKind.PAYMENT),
        ("This order is not permitted for this account", RejectionKind.NOT_PERMITTED),
        ("user is not whitelisted", RejectionKind.NOT_PERMITTED),
        ("Something unexpected", RejectionKind.OTHER),
    ],
)
def test_refusal_message_is_classified_but_never_exposed(
    monkeypatch: pytest.MonkeyPatch, message: str, kind: RejectionKind
) -> None:
    _patch_mcp(monkeypatch, FakeMcpResult({"success": False, "error": {"message": message}}))

    with pytest.raises(CommerceRejected) as error:
        asyncio.run(_adapter().get_cart())

    assert error.value.kind is kind
    assert message not in str(error.value)
    assert error.value.provider_message == message


@pytest.mark.parametrize("message", ["Upstream timeout", "Internal error", "Bad gateway"])
def test_transient_failure_is_not_a_refusal(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    _patch_mcp(monkeypatch, FakeMcpResult({"success": False, "error": {"message": message}}))

    with pytest.raises(CommerceError) as error:
        asyncio.run(_adapter().get_cart())

    assert not isinstance(error.value, CommerceRejected)


def test_only_specific_refusals_prove_no_order_exists() -> None:
    assert RejectionKind.MIN_ORDER.is_definitive
    assert not RejectionKind.DUPLICATE.is_definitive
    assert not RejectionKind.OTHER.is_definitive


def test_error_result_text_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    result = FakeMcpResult(None, is_error=True)
    result.content = [TextContent(type="text", text="Address not serviceable")]
    _patch_mcp(monkeypatch, result)

    with pytest.raises(CommerceRejected) as error:
        asyncio.run(_adapter().get_cart())

    assert error.value.kind is RejectionKind.NOT_SERVICEABLE


# ------------------------------------------------- order history tolerates unknown formats
def _orders_payload(**order: object) -> dict[str, object]:
    orders: list[dict[str, object]] = [{"orderId": "O1", **order}]
    return {"success": True, "data": {"orders": orders, "hasMore": False}}


def test_unparsable_order_date_does_not_break_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mcp(
        monkeypatch,
        FakeMcpResult(
            _orders_payload(createdAt="yesterday-ish", totalAmount=86.5, currentStatus="X")
        ),
    )

    [order] = asyncio.run(_adapter().get_orders())

    assert order.order_id == "O1"
    assert order.created_at is None
    assert order.status == "X"


def test_order_without_optional_fields_still_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mcp(monkeypatch, FakeMcpResult(_orders_payload(status="CONFIRMED")))

    [order] = asyncio.run(_adapter().get_orders())

    assert order.status == "CONFIRMED"
    assert order.items == []


def test_order_without_an_id_is_an_error_not_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    no_id: dict[str, object] = {"success": True, "data": {"orders": [dict[str, object]()]}}
    _patch_mcp(monkeypatch, FakeMcpResult(no_id))

    with pytest.raises(CommerceError):
        asyncio.run(_adapter().get_orders())


# ------------------------------------------------- login handling
def test_expired_token_stops_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    backend = FakeKeyring()
    storage = KeyringTokenStorage(backend)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="t", expires_in=1)))
    adapter = SwiggyInstamart(
        endpoint=SWIGGY_MCP_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=storage,
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
    )

    with pytest.raises(CommerceAuthRequired):
        asyncio.run(adapter.get_cart())

    assert calls == []


def test_fresh_token_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult(_cart_payload()))
    storage = KeyringTokenStorage(FakeKeyring())
    asyncio.run(storage.set_tokens(OAuthToken(access_token="t", expires_in=432000)))
    adapter = SwiggyInstamart(
        endpoint=SWIGGY_MCP_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=storage,
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
    )

    asyncio.run(adapter.get_cart())

    assert len(calls) == 1


def test_server_auth_error_asks_for_login(monkeypatch: pytest.MonkeyPatch) -> None:
    class RpcError(Exception):
        code = -32001

    _patch_mcp(
        monkeypatch,
        FakeMcpResult({}),
        tool_error=RpcError("expired"),
        tool_error_name="get_cart",
    )

    with pytest.raises(CommerceAuthRequired):
        asyncio.run(_adapter().get_cart())


# ------------------------------------------------- environment separation
def test_staging_is_allowed_and_keeps_separate_credentials() -> None:
    production = KeyringTokenStorage(FakeKeyring())
    staging = KeyringTokenStorage(FakeKeyring(), endpoint=SWIGGY_STAGING_MCP_URL)
    assert staging.service_name != production.service_name
    adapter = SwiggyInstamart(
        endpoint=SWIGGY_STAGING_MCP_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=staging,
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
    )
    assert adapter.endpoint == SWIGGY_STAGING_MCP_URL


def test_unknown_endpoint_is_refused() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        SwiggyInstamart(
            endpoint="https://example.test/im",
            redirect_uri=OAUTH_REDIRECT_URI,
            storage=KeyringTokenStorage(FakeKeyring()),
            redirect_handler=_no_redirect,
            callback_handler=_no_callback,
        )
