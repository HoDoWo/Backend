import os

import pytest

from jwt_helpers import TEST_SECRET

# Pin the non-secret settings before the app is imported, so tests do not
# depend on a developer's local .env. load_dotenv() never overwrites values
# that are already present in the environment.
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000,https://app.hodowo.com"
os.environ["AI_RATE_LIMIT"] = "20/minute"


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    """Give every test a known JWT configuration, ignoring any local .env."""
    monkeypatch.setenv("JWT_SECRET", TEST_SECRET)
    for name in (
        "JWT_JWKS_URL",
        "JWT_ALGORITHMS",
        "JWT_AUDIENCE",
        "JWT_ISSUER",
        "JWT_LEEWAY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear the in-memory rate-limit counters so tests are independent."""
    from api import config, index

    config.get_settings.cache_clear()
    index.limiter.reset()
    yield
    index.limiter.reset()
