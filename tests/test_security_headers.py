"""Security headers added after the OWASP ZAP scan of Sprint 1 (#44)."""

import pytest
from fastapi.testclient import TestClient

from api.index import SECURITY_HEADERS, app
from jwt_helpers import bearer, make_token

client = TestClient(app)


class FakeAgent:
    def invoke(self, request):
        return {"messages": [type("Message", (), {"content": "ok"})()]}


@pytest.fixture(autouse=True)
def fake_agent(monkeypatch):
    monkeypatch.setattr("api.index.build_agent", lambda image_path: FakeAgent())


def _assert_security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["Cache-Control"] == "no-store"


def test_public_health_has_security_headers():
    _assert_security_headers(client.get("/health"))


def test_authenticated_chat_has_security_headers():
    response = client.post(
        "/api/chat",
        json={"message": "Hello"},
        headers=bearer(make_token()),
    )

    assert response.status_code == 200
    _assert_security_headers(response)


def test_rejected_request_has_security_headers():
    response = client.post("/api/chat", json={"message": "Hello"})

    assert response.status_code == 401
    _assert_security_headers(response)


def test_unknown_path_has_security_headers():
    response = client.get("/does-not-exist", headers=bearer(make_token()))

    assert response.status_code == 404
    _assert_security_headers(response)


def test_cors_preflight_has_security_headers():
    response = client.options(
        "/api/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    _assert_security_headers(response)
