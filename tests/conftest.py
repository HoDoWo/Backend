import pytest

from jwt_helpers import TEST_SECRET


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