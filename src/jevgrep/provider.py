"""Choose where Jev requests go (OpenRouter or TypeSafe) and which key they use."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# The TypeSafe SDK appends /v1/systemone to this base URL.
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
# OpenRouter's OpenAI-compatible chat API, used by --explain.
OPENROUTER_CHAT_BASE_URL = "https://openrouter.ai/api/v1"

OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
TYPESAFE_KEY_ENV = "TYPESAFE_API_KEY"

PROVIDERS = ("auto", "openrouter", "typesafe")

NO_KEY_MESSAGE = (
    f"no API key found. Set {OPENROUTER_KEY_ENV} (https://openrouter.ai/settings/keys) "
    f"or {TYPESAFE_KEY_ENV} (https://console.typesafe.ai/keys)."
)


class ConfigError(Exception):
    """A setup problem the user can fix: a missing key or a missing optional package."""


@dataclass(frozen=True)
class Provider:
    name: str  # "openrouter" or "typesafe"
    api_key: str
    base_url: str | None  # None lets the SDK use TYPESAFE_BASE_URL or its default


def env_key(name: str, env: Mapping[str, str] | None = None) -> str | None:
    value = (os.environ if env is None else env).get(name, "").strip()
    return value or None


def resolve_provider(requested: str = "auto", env: Mapping[str, str] | None = None) -> Provider:
    """Pick the provider; `auto` prefers OpenRouter when both keys are set."""
    openrouter_key = env_key(OPENROUTER_KEY_ENV, env)
    typesafe_key = env_key(TYPESAFE_KEY_ENV, env)
    if requested == "auto":
        if openrouter_key:
            requested = "openrouter"
        elif typesafe_key:
            requested = "typesafe"
        else:
            raise ConfigError(NO_KEY_MESSAGE)

    if requested == "openrouter":
        if not openrouter_key:
            raise ConfigError(f"--provider openrouter needs {OPENROUTER_KEY_ENV} to be set.")
        return Provider("openrouter", openrouter_key, OPENROUTER_BASE_URL)
    if requested == "typesafe":
        if not typesafe_key:
            raise ConfigError(f"--provider typesafe needs {TYPESAFE_KEY_ENV} to be set.")
        return Provider("typesafe", typesafe_key, None)
    raise ConfigError(f"unknown provider {requested!r}; choose from {', '.join(PROVIDERS)}.")
