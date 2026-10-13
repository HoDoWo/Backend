"""JWT verification for the HoDoWo API.

Tokens are only *verified* here (signature, expiry, optional audience/issuer);
issuing them is the job of whatever identity provider the frontend logs into.

Configuration (environment variables) - set exactly one of the two key sources:

    JWT_SECRET      Shared secret for HMAC tokens (HS256 by default).
    JWT_JWKS_URL    JWKS endpoint for asymmetric tokens (RS256 by default).

Optional:

    JWT_ALGORITHMS  Comma-separated allow-list. Defaults to HS256 with
                    JWT_SECRET and RS256 with JWT_JWKS_URL.
    JWT_AUDIENCE    Expected "aud" claim. When unset, "aud" is not checked.
    JWT_ISSUER      Expected "iss" claim. When unset, "iss" is not checked.
    JWT_LEEWAY      Clock-skew tolerance in seconds (default 10).

If no key source is configured, every protected request is rejected with a
500: authentication fails closed rather than silently turning itself off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt

if __package__:
    from .logging_config import get_logger
else:
    from logging_config import get_logger

logger = get_logger(__name__)

# Paths reachable without a token. CORS preflight (OPTIONS) is always allowed.
PUBLIC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})


class AuthError(Exception):
    """Authentication failure carrying the HTTP response to send."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

    @property
    def headers(self) -> dict[str, str]:
        return {"WWW-Authenticate": "Bearer"} if self.status_code == 401 else {}


@dataclass(frozen=True)
class AuthConfig:
    secret: str | None
    jwks_url: str | None
    algorithms: list[str]
    audience: str | None
    issuer: str | None
    leeway: int


def _misconfigured(reason: str) -> AuthError:
    logger.error("JWT authentication is misconfigured: %s", reason)
    return AuthError(500, "authentication is not configured")


def load_config() -> AuthConfig:
    """Read and validate the JWT settings from the environment."""
    secret = os.getenv("JWT_SECRET", "").strip() or None
    jwks_url = os.getenv("JWT_JWKS_URL", "").strip() or None

    if secret and jwks_url:
        raise _misconfigured("set only one of JWT_SECRET and JWT_JWKS_URL")
    if not secret and not jwks_url:
        raise _misconfigured("neither JWT_SECRET nor JWT_JWKS_URL is set")

    default_algorithm = "HS256" if secret else "RS256"
    raw_algorithms = os.getenv("JWT_ALGORITHMS", default_algorithm)
    algorithms = [a.strip() for a in raw_algorithms.split(",") if a.strip()]
    if not algorithms:
        raise _misconfigured("JWT_ALGORITHMS is empty")

    # Block algorithm-confusion attacks: a shared secret may only be used with
    # HMAC algorithms, and a public key may never be used as an HMAC secret.
    for algorithm in algorithms:
        is_hmac = algorithm.upper().startswith("HS")
        if algorithm.lower() == "none":
            raise _misconfigured("the 'none' algorithm is not allowed")
        if secret and not is_hmac:
            raise _misconfigured("JWT_SECRET requires HS* algorithms")
        if jwks_url and is_hmac:
            raise _misconfigured("JWT_JWKS_URL requires asymmetric algorithms")

    try:
        leeway = int(os.getenv("JWT_LEEWAY", "10"))
    except ValueError:
        raise _misconfigured("JWT_LEEWAY must be an integer") from None

    return AuthConfig(
        secret=secret,
        jwks_url=jwks_url,
        algorithms=algorithms,
        audience=os.getenv("JWT_AUDIENCE", "").strip() or None,
        issuer=os.getenv("JWT_ISSUER", "").strip() or None,
        leeway=leeway,
    )


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # One client per URL so fetched signing keys stay cached between requests.
    return jwt.PyJWKClient(url, cache_keys=True)


def extract_bearer_token(authorization: str | None) -> str:
    """Return the token from an 'Authorization: Bearer <token>' header."""
    if not authorization:
        raise AuthError(401, "missing bearer token")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError(401, "invalid authorization header")
    return token.strip()


def decode_token(token: str) -> dict[str, Any]:
    """Verify a JWT and return its claims, or raise AuthError."""
    config = load_config()

    try:
        if config.jwks_url:
            key: Any = _jwks_client(config.jwks_url).get_signing_key_from_jwt(token).key
        else:
            key = config.secret

        return jwt.decode(
            token,
            key,
            algorithms=config.algorithms,
            audience=config.audience,
            issuer=config.issuer,
            leeway=config.leeway,
            options={
                "require": ["exp"],
                "verify_aud": config.audience is not None,
            },
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "token expired") from None
    except jwt.PyJWTError as exc:
        # Covers bad signature, malformed token, wrong aud/iss, unknown key id
        # and JWKS fetch failures. Details go to the log, not to the client.
        logger.warning("Rejected JWT: %s: %s", type(exc).__name__, exc)
        raise AuthError(401, "invalid token") from None


def authenticate_request(authorization: str | None) -> dict[str, Any]:
    """Validate an Authorization header value and return the token claims."""
    return decode_token(extract_bearer_token(authorization))