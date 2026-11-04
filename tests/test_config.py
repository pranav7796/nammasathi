import pytest

from app.config import ConfigurationError, load_settings


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
