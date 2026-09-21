import pytest

from jevgrep.provider import OPENROUTER_BASE_URL, ConfigError, resolve_provider

BOTH = {"OPENROUTER_API_KEY": "sk-or", "TYPESAFE_API_KEY": "ts-key"}


def test_auto_prefers_openrouter_when_both_keys_are_set():
    provider = resolve_provider("auto", BOTH)
    assert (provider.name, provider.api_key, provider.base_url) == (
        "openrouter",
        "sk-or",
        OPENROUTER_BASE_URL,
    )


def test_auto_falls_back_to_typesafe():
    provider = resolve_provider("auto", {"TYPESAFE_API_KEY": "ts-key"})
    assert (provider.name, provider.api_key, provider.base_url) == ("typesafe", "ts-key", None)


def test_explicit_provider_wins_over_auto_detection():
    assert resolve_provider("typesafe", BOTH).name == "typesafe"


def test_missing_keys_name_both_variables():
    with pytest.raises(ConfigError) as error:
        resolve_provider("auto", {"OPENROUTER_API_KEY": "   "})
    assert "OPENROUTER_API_KEY" in str(error.value)
    assert "TYPESAFE_API_KEY" in str(error.value)


@pytest.mark.parametrize(
    ("provider", "env", "variable"),
    [
        ("openrouter", {"TYPESAFE_API_KEY": "ts-key"}, "OPENROUTER_API_KEY"),
        ("typesafe", {"OPENROUTER_API_KEY": "sk-or"}, "TYPESAFE_API_KEY"),
    ],
)
def test_explicit_provider_needs_its_own_key(provider, env, variable):
    with pytest.raises(ConfigError, match=variable):
        resolve_provider(provider, env)
