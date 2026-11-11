"""Environment-backed configuration for the HoDoWo backend.

Every environment variable is read here, so the rest of the code never touches
``os.environ`` directly and a missing value fails with one clear message. In
production the values come from the Vercel project environment variables; in
development ``python-dotenv`` loads them from a local ``.env`` file.
"""

from __future__ import annotations

import dataclasses
import functools
import os

import dotenv
import fastapi
import langchain_openai
import slowapi
from slowapi import util


# Load a local .env when present. Vercel injects the values itself, so this is
# a no-op there. Existing variables are never overwritten.
dotenv.load_dotenv()

DEFAULT_ALLOWED_ORIGINS = "http://localhost:3000,https://app.hodowo.com"
DEFAULT_AI_RATE_LIMIT = "20/minute"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"

_VALID_ENVIRONMENTS = {"dev", "prod"}



class ConfigurationError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


@dataclasses.dataclass(frozen=True)
class Settings:
    """Non-secret settings validated once at startup."""

    environment: str
    allowed_origins: tuple[str, ...]
    ai_rate_limit: str


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@functools.lru_cache(maxsize=1)
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

def get_openrouter_llm() -> langchain_openai.ChatOpenAI:
    return langchain_openai.ChatOpenAI(
        model=get_openrouter_model(),
        api_key=get_openrouter_api_key(),
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
        # A stuck model call must not use up the whole 300 s of Vercel.
        timeout=60,
        max_retries=1,
    )
    

DEFAULT_STRIPE_CURRENCY = "eur"


@dataclasses.dataclass(frozen=True)
class StripeSettings:
    """Stripe keys for the marketplace checkout (#43).

    Without a secret key the checkout runs in simulated mode: orders are still
    recorded, but flagged ``simulated`` and no payment is taken.
    """

    secret_key: str | None
    webhook_secret: str | None
    currency: str

    @property
    def enabled(self) -> bool:
        return self.secret_key is not None


def get_stripe_settings() -> StripeSettings:
    """Return the Stripe settings, refusing anything that is not a test key."""
    secret_key = get_optional_env("STRIPE_SECRET_KEY") or None
    webhook_secret = get_optional_env("STRIPE_WEBHOOK_SECRET") or None
    currency = (get_optional_env("STRIPE_CURRENCY", DEFAULT_STRIPE_CURRENCY) or DEFAULT_STRIPE_CURRENCY).lower()

    # The marketplace is a demo: a live key must never reach this backend.
    if secret_key and not secret_key.startswith(("sk_test_", "rk_test_")):
        raise ConfigurationError(
            "STRIPE_SECRET_KEY must be a Stripe test-mode key (sk_test_... or rk_test_...)."
        )
    if secret_key and not webhook_secret:
        raise ConfigurationError(
            "STRIPE_WEBHOOK_SECRET is required when STRIPE_SECRET_KEY is set, "
            "otherwise paid orders are never confirmed."
        )
    return StripeSettings(secret_key=secret_key, webhook_secret=webhook_secret, currency=currency)


def _client_ip(request: fastapi.Request) -> str:
    """Return the caller IP, honouring the proxy header Vercel sets."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return util.get_remote_address(request)

def _user_or_ip(request: fastapi.Request) -> str:
    """Count per signed-in user (the JWT middleware runs first), else per IP."""
    user = getattr(request.state, "user", None) or {}
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    return f"user:{uid}" if uid else _client_ip(request)


limiter = slowapi.Limiter(key_func=_client_ip)
ai_rate_limit = limiter.limit(get_settings().ai_rate_limit)

# Forum posts, answers, votes and reports: enough for a person, not for a bot.
DEFAULT_FORUM_RATE_LIMIT = "10/minute"
forum_rate_limit = limiter.limit(
    os.getenv("FORUM_RATE_LIMIT", DEFAULT_FORUM_RATE_LIMIT).strip() or DEFAULT_FORUM_RATE_LIMIT,
    key_func=_user_or_ip,
)

# Messages and offers between buyers and sellers: a chat, but not spam.
DEFAULT_MESSAGES_RATE_LIMIT = "30/minute"
messages_rate_limit = limiter.limit(
    os.getenv("MESSAGES_RATE_LIMIT", DEFAULT_MESSAGES_RATE_LIMIT).strip() or DEFAULT_MESSAGES_RATE_LIMIT,
    key_func=_user_or_ip,
)
