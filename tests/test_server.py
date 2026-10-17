"""Server foundation tests: health, CORS, rate limiting and configuration."""

import pytest
from fastapi.testclient import TestClient

from api import config
from api.index import app
from jwt_helpers import bearer, make_token

client = TestClient(app)


class FakeAgent:
    def invoke(self, request):
        return {"messages": [type("Message", (), {"content": "ok"})()]}


@pytest.fixture(autouse=True)
def fake_agent(monkeypatch):
    monkeypatch.setattr("api.index.build_agent", lambda image_path: FakeAgent())


def _rate_limit_per_minute() -> int:
    return int(config.get_settings().ai_rate_limit.split("/")[0])


# --- health ---------------------------------------------------------------


def test_health_is_public_and_ok():
    result = client.get("/health")

    assert result.status_code == 200
    assert result.json() == {"status": "ok"}


def test_health_does_not_require_a_token():
    assert client.get("/health").status_code == 200


# --- CORS -----------------------------------------------------------------


def test_allowed_origin_is_echoed_back():
    result = client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert result.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_preflight_from_disallowed_origin_is_rejected():
    result = client.options(
        "/api/chat",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert result.status_code == 400
    assert "access-control-allow-origin" not in result.headers


# --- rate limiting --------------------------------------------------------


def test_ai_endpoint_returns_429_after_the_limit():
    limit = _rate_limit_per_minute()
    headers = bearer(make_token())

    codes = [
        client.post("/api/chat", json={"message": "hi"}, headers=headers).status_code
        for _ in range(limit + 1)
    ]

    assert codes[:limit] == [200] * limit
    assert codes[-1] == 429


def test_health_is_not_rate_limited():
    limit = _rate_limit_per_minute()

    codes = [client.get("/health").status_code for _ in range(limit + 5)]

    assert set(codes) == {200}


# --- configuration --------------------------------------------------------


def test_missing_required_key_fails_with_a_clear_message(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(config.ConfigurationError, match="OPENROUTER_API_KEY"):
        config.get_openrouter_api_key()


def test_configuration_rejects_wildcard_origin(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "*")
    config.get_settings.cache_clear()

    with pytest.raises(config.ConfigurationError):
        config.get_settings()
