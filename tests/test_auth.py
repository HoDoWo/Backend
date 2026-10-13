import pytest
from fastapi.testclient import TestClient

from api import auth
from api.index import app
from jwt_helpers import TEST_SECRET, bearer, make_token

client = TestClient(app)


class FakeAgent:
    def invoke(self, request):
        return {"messages": [type("Message", (), {"content": "ok"})()]}


@pytest.fixture(autouse=True)
def fake_agent(monkeypatch):
    monkeypatch.setattr("api.index.build_agent", lambda image_path: FakeAgent())


def _post(headers=None):
    return client.post("/api/chat", json={"message": "Hello"}, headers=headers)


# --- middleware -----------------------------------------------------------


def test_request_without_token_is_rejected():
    result = _post()

    assert result.status_code == 401
    assert result.json() == {"detail": "missing bearer token"}
    assert result.headers["www-authenticate"] == "Bearer"


def test_valid_token_reaches_the_route():
    assert _post(bearer(make_token())).status_code == 200


def test_non_bearer_scheme_is_rejected():
    result = _post({"Authorization": "Basic dXNlcjpwYXNz"})

    assert result.status_code == 401
    assert result.json() == {"detail": "invalid authorization header"}


def test_expired_token_is_rejected():
    result = _post(bearer(make_token(exp=1)))

    assert result.status_code == 401
    assert result.json() == {"detail": "token expired"}


def test_token_signed_with_wrong_secret_is_rejected():
    result = _post(bearer(make_token(secret="another-secret-that-is-32-bytes-long!")))

    assert result.status_code == 401
    assert result.json() == {"detail": "invalid token"}


def test_token_without_expiry_is_rejected():
    assert _post(bearer(make_token(exp=None))).status_code == 401


def test_unsigned_token_is_rejected():
    import base64
    import json

    def part(data):
        raw = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=")
        return raw.decode()

    token = f"{part({'alg': 'none', 'typ': 'JWT'})}.{part({'sub': 'x', 'exp': 9999999999})}."

    assert _post(bearer(token)).status_code == 401


def test_options_preflight_does_not_require_a_token():
    result = client.options("/api/chat")

    assert result.status_code != 401


def test_missing_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("JWT_SECRET")

    result = _post(bearer(make_token()))

    assert result.status_code == 500
    assert result.json() == {"detail": "authentication is not configured"}


# --- audience / issuer ----------------------------------------------------


def test_audience_is_checked_when_configured(monkeypatch):
    monkeypatch.setenv("JWT_AUDIENCE", "hodowo")

    assert _post(bearer(make_token(aud="hodowo"))).status_code == 200
    assert _post(bearer(make_token(aud="someone-else"))).status_code == 401
    assert _post(bearer(make_token())).status_code == 401


def test_audience_claim_is_ignored_when_not_configured():
    assert _post(bearer(make_token(aud="anything"))).status_code == 200


def test_issuer_is_checked_when_configured(monkeypatch):
    monkeypatch.setenv("JWT_ISSUER", "https://auth.example.com")

    assert _post(bearer(make_token(iss="https://auth.example.com"))).status_code == 200
    assert _post(bearer(make_token(iss="https://evil.example.com"))).status_code == 401


# --- configuration validation --------------------------------------------


def test_secret_and_jwks_together_are_rejected(monkeypatch):
    monkeypatch.setenv("JWT_JWKS_URL", "https://auth.example.com/.well-known/jwks.json")

    with pytest.raises(auth.AuthError) as error:
        auth.load_config()
    assert error.value.status_code == 500


@pytest.mark.parametrize("algorithms", ["none", "RS256", "HS256,RS256"])
def test_secret_only_allows_hmac_algorithms(monkeypatch, algorithms):
    monkeypatch.setenv("JWT_ALGORITHMS", algorithms)

    with pytest.raises(auth.AuthError):
        auth.load_config()


def test_jwks_rejects_hmac_algorithms(monkeypatch):
    monkeypatch.delenv("JWT_SECRET")
    monkeypatch.setenv("JWT_JWKS_URL", "https://auth.example.com/.well-known/jwks.json")
    monkeypatch.setenv("JWT_ALGORITHMS", "HS256")

    with pytest.raises(auth.AuthError):
        auth.load_config()


def test_decode_token_returns_claims():
    claims = auth.decode_token(make_token(sub="abc", role="admin"))

    assert claims["sub"] == "abc"
    assert claims["role"] == "admin"