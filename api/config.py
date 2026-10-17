"""Environment-backed configuration for the HoDoWo backend.

Every environment variable is read here, so the rest of the code never touches
``os.environ`` directly and a missing value fails with one clear message. In
production the values come from the Vercel project environment variables; in
development ``python-dotenv`` loads them from a local ``.env`` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

# Load a local .env when present. Vercel injects the values itself, so this is
# a no-op there. Existing variables are never overwritten.
load_dotenv()

DEFAULT_ALLOWED_ORIGINS = "http://localhost:3000,https://app.hodowo.com"
DEFAULT_AI_RATE_LIMIT = "20/minute"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"

_VALID_ENVIRONMENTS = {"dev", "prod"}


class ConfigurationError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Non-secret settings validated once at startup."""

    environment: str
    allowed_origins: tuple[str, ...]
    ai_rate_limit: str


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return and validate the non-secret settings from the environment."""
    environment = os.getenv("HODOWO_ENV", "dev").strip().lower()
    if environment not in _VALID_ENVIRONMENTS:
        raise ConfigurationError(
            "HODOWO_ENV must be either 'dev' or 'prod', "
            f"got {environment!r}."
        )

    allowed_origins = _split_csv(
        os.getenv("ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
    )
    if not allowed_origins:
        raise ConfigurationError(
            "ALLOWED_ORIGINS must list at least one browser origin."
        )
    if "*" in allowed_origins:
        raise ConfigurationError(
            "ALLOWED_ORIGINS must list explicit origins; '*' is not allowed."
        )

    ai_rate_limit = os.getenv("AI_RATE_LIMIT", DEFAULT_AI_RATE_LIMIT).strip()
    if not ai_rate_limit:
        raise ConfigurationError("AI_RATE_LIMIT must not be empty.")

    return Settings(
        environment=environment,
        allowed_origins=allowed_origins,
        ai_rate_limit=ai_rate_limit,
    )


def get_required_env(name: str) -> str:
    """Return a required environment variable or fail with an actionable error."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(
            f"Missing required environment variable {name}. "
            "Set it in .env locally or in the Vercel project environment variables."
        )
    return value


def get_optional_env(name: str, default: str = "") -> str:
    """Return an optional environment variable, trimmed."""
    return os.getenv(name, default).strip()


def get_openrouter_api_key() -> str:
    """Return the OpenRouter API key used by every AI call."""
    return get_required_env("OPENROUTER_API_KEY")


def get_openrouter_model() -> str:
    """Return the OpenRouter model id, falling back to the free default."""
    model = get_optional_env("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    return model or DEFAULT_OPENROUTER_MODEL
