import pytest

from app.commerce.mock import MockInstamart
from app.commerce.order_journal import FileOrderJournal, InMemoryOrderJournal
from app.commerce.runtime import (
    checkout_policy,
    commerce_adapter,
    dining_adapter,
    food_adapter,
    order_journal,
)
from app.commerce.swiggy import SwiggyInstamart
from app.config import ConfigurationError, load_settings
from app.dining.mock import MockDineout
from app.dining.swiggy import SwiggyDineout
from app.food.mock import MockFood
from app.food.swiggy import SwiggyFood


def test_configuration_defaults_to_mock_and_disables_real_checkout() -> None:
    settings = load_settings({})

    assert settings.commerce_backend == "mock"
    assert settings.allow_real_checkout is False
    with pytest.raises(ConfigurationError):
        settings.require_real_checkout()


def test_real_checkout_requires_swiggy_backend() -> None:
    with pytest.raises(ConfigurationError):
        load_settings({"ALLOW_REAL_CHECKOUT": "true"})


def test_live_swiggy_access_is_explicit_and_off_by_default() -> None:
    assert load_settings({}).swiggy_live_access is False
    with pytest.raises(ConfigurationError, match="COMMERCE_BACKEND=swiggy"):
        load_settings({"SWIGGY_LIVE_ACCESS": "true"})
    assert (
        load_settings(
            {"COMMERCE_BACKEND": "swiggy", "SWIGGY_LIVE_ACCESS": "true"}
        ).swiggy_live_access
        is True
    )


def test_swiggy_endpoint_is_fixed_to_official_instamart_server() -> None:
    with pytest.raises(ConfigurationError, match=r"https://mcp.swiggy.com/im"):
        load_settings({"COMMERCE_BACKEND": "swiggy", "SWIGGY_MCP_URL": "https://example.test/im"})


def test_swiggy_backend_remains_read_only_by_default() -> None:
    settings = load_settings({"COMMERCE_BACKEND": "swiggy"})

    assert settings.allow_real_checkout is False
    with pytest.raises(ConfigurationError):
        settings.require_real_checkout()


def test_gemini_timeout_defaults_to_sixty_seconds() -> None:
    assert load_settings({}).gemini_timeout_seconds == 60


def test_gemini_timeout_is_configurable_and_positive() -> None:
    assert load_settings({"GEMINI_TIMEOUT_SECONDS": "45"}).gemini_timeout_seconds == 45
    with pytest.raises(ConfigurationError):
        load_settings({"GEMINI_TIMEOUT_SECONDS": "0"})


def test_openrouter_fallback_settings_are_optional_and_secret_is_not_repr() -> None:
    defaults = load_settings({})
    assert defaults.openrouter_api_key is None
    assert defaults.openrouter_model == "nvidia/nemotron-3-ultra-550b-a55b:free"

    configured = load_settings(
        {
            "OPENROUTER_API_KEY": "test-openrouter-key",
            "OPENROUTER_MODEL": "nvidia/custom-model",
        }
    )
    assert configured.openrouter_api_key == "test-openrouter-key"
    assert configured.openrouter_model == "nvidia/custom-model"
    assert "test-openrouter-key" not in repr(configured)


def test_openrouter_model_cannot_be_empty() -> None:
    with pytest.raises(ConfigurationError, match="OPENROUTER_MODEL"):
        load_settings({"OPENROUTER_MODEL": "  "})


def test_groq_fallback_settings_are_optional_with_default_model() -> None:
    defaults = load_settings({})
    assert defaults.groq_api_key is None
    assert defaults.groq_model == "openai/gpt-oss-20b"

    configured = load_settings(
        {"GROQ_API_KEY": "test-groq-key", "GROQ_MODEL": "openai/custom-model"}
    )
    assert configured.groq_api_key == "test-groq-key"
    assert configured.groq_model == "openai/custom-model"
    assert "test-groq-key" not in repr(configured)


def test_groq_model_cannot_be_empty() -> None:
    with pytest.raises(ConfigurationError, match="GROQ_MODEL"):
        load_settings({"GROQ_MODEL": "  "})


REAL_ORDER = {
    "COMMERCE_BACKEND": "swiggy",
    "SWIGGY_LIVE_ACCESS": "true",
    "ALLOW_SWIGGY_CART_WRITES": "true",
    "ALLOW_REAL_CHECKOUT": "true",
    "MAX_REAL_ORDER_INR": "500",
}


def test_live_reads_do_not_imply_cart_writes() -> None:
    settings = load_settings({"COMMERCE_BACKEND": "swiggy", "SWIGGY_LIVE_ACCESS": "true"})
    assert settings.allow_swiggy_cart_writes is False
    assert settings.allow_real_checkout is False


def test_cart_writes_require_live_access() -> None:
    with pytest.raises(ConfigurationError, match="SWIGGY_LIVE_ACCESS"):
        load_settings({"COMMERCE_BACKEND": "swiggy", "ALLOW_SWIGGY_CART_WRITES": "true"})
    with pytest.raises(ConfigurationError):
        load_settings({"ALLOW_SWIGGY_CART_WRITES": "true"})


@pytest.mark.parametrize("missing", ["ALLOW_SWIGGY_CART_WRITES", "MAX_REAL_ORDER_INR"])
def test_real_checkout_needs_every_explicit_switch(missing: str) -> None:
    values = {key: value for key, value in REAL_ORDER.items() if key != missing}
    with pytest.raises(ConfigurationError, match="ALLOW_REAL_CHECKOUT"):
        load_settings(values)


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "NaN", "Infinity"])
def test_money_limit_must_be_a_positive_number(amount: str) -> None:
    with pytest.raises(ConfigurationError, match="MAX_REAL_ORDER_INR"):
        load_settings({**REAL_ORDER, "MAX_REAL_ORDER_INR": amount})


def test_fully_specified_real_checkout_loads_with_one_order_limit() -> None:
    settings = load_settings(REAL_ORDER)
    assert settings.allow_real_checkout is True
    assert settings.real_order_limit == 1
    assert settings.max_real_order_inr == 500


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_order_limit_must_be_positive(value: str) -> None:
    with pytest.raises(ConfigurationError, match="REAL_ORDER_LIMIT"):
        load_settings({"REAL_ORDER_LIMIT": value})


def test_staging_endpoint_is_accepted() -> None:
    settings = load_settings(
        {"COMMERCE_BACKEND": "swiggy", "SWIGGY_MCP_URL": "https://mcp-staging.swiggy.com/im"}
    )
    assert settings.swiggy_mcp_url == "https://mcp-staging.swiggy.com/im"


def test_flags_reject_anything_but_true_or_false() -> None:
    with pytest.raises(ConfigurationError):
        load_settings({"ALLOW_SWIGGY_CART_WRITES": "yes"})


def test_mock_mode_never_builds_a_swiggy_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("mock mode constructed a real client")

    monkeypatch.setattr("app.commerce.runtime.SwiggyInstamart", forbidden)
    monkeypatch.setattr("app.commerce.runtime.KeyringTokenStorage", forbidden)

    adapter = commerce_adapter(load_settings({}))

    assert isinstance(adapter, MockInstamart)
    assert isinstance(order_journal(load_settings({})), InMemoryOrderJournal)


def test_real_mode_policy_carries_limits_and_real_delays() -> None:
    settings = load_settings(REAL_ORDER)
    policy = checkout_policy(settings)
    assert policy.max_orders == 1
    assert policy.max_total_inr == 500
    assert sum(policy.reconcile_delays) <= 30
    assert policy.reconcile_delays[0] >= 2
    assert isinstance(order_journal(settings), FileOrderJournal)


def test_real_adapter_receives_each_switch_separately() -> None:
    read_only = load_settings({"COMMERCE_BACKEND": "swiggy", "SWIGGY_LIVE_ACCESS": "true"})
    adapter = commerce_adapter(read_only)
    assert isinstance(adapter, SwiggyInstamart)
    assert adapter.allow_cart_writes is False
    assert adapter.allow_real_checkout is False


def test_real_booking_is_its_own_switch() -> None:
    assert load_settings({}).allow_real_booking is False
    assert load_settings(REAL_ORDER).allow_real_booking is False
    with pytest.raises(ConfigurationError, match="ALLOW_REAL_BOOKING"):
        load_settings({"ALLOW_REAL_BOOKING": "true"})
    read_only = {"COMMERCE_BACKEND": "swiggy", "SWIGGY_LIVE_ACCESS": "true"}
    assert load_settings({**read_only, "ALLOW_REAL_BOOKING": "true"}).allow_real_booking is True


def test_live_food_and_dining_clients_get_only_their_own_switches() -> None:
    settings = load_settings({"COMMERCE_BACKEND": "swiggy", "SWIGGY_LIVE_ACCESS": "true"})
    food = food_adapter(settings, "addr")
    dining = dining_adapter(settings)
    assert isinstance(food, SwiggyFood)
    assert food.endpoint == "https://mcp.swiggy.com/food"
    assert (food.allow_cart_writes, food.allow_real_checkout) == (False, False)
    assert isinstance(dining, SwiggyDineout)
    assert dining.endpoint == "https://mcp.swiggy.com/dineout"
    assert dining.allow_real_checkout is False


def test_mock_mode_never_builds_live_food_or_dining_clients() -> None:
    settings = load_settings({})
    assert isinstance(food_adapter(settings, "addr"), MockFood)
    assert isinstance(dining_adapter(settings), MockDineout)
