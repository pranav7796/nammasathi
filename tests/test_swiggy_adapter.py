from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, cast

import pytest
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.exceptions import OAuthTokenError
from mcp.shared.auth import AuthorizationCodeResult, OAuthToken

from app.commerce.base import CartLine, CommerceError, PaymentMethod
from app.commerce.swiggy import (
    OAUTH_REDIRECT_URI,
    SWIGGY_MCP_URL,
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


def _adapter(*, checkout: bool = False) -> SwiggyInstamart:
    return SwiggyInstamart(
        endpoint=SWIGGY_MCP_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(FakeKeyring()),
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
        allow_real_checkout=checkout,
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

    async def call_tool(self, name: str, arguments: dict[str, object]) -> FakeMcpResult:
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

    payload, message = asyncio.run(adapter._call_with_message("example_tool", {}))

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
    _patch_mcp(monkeypatch, FakeMcpResult({"success": True, "data": {"items": []}}))

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
