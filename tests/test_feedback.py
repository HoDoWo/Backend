"""POST /api/feedback validates its body (#159): never a 500, never the user's text in the logs."""

import logging

from fastapi.testclient import TestClient
from index import app
from jwt_helpers import bearer, make_token

client = TestClient(app, headers=bearer(make_token()))


def test_a_rating_is_acknowledged_without_logging_the_text(caplog):
    with caplog.at_level(logging.INFO):
        response = client.post("/api/feedback", json={"rating": 4, "comment": "my secret address", "other": 1})

    assert response.json() == {"status": "ok"}
    assert "my secret address" not in caplog.text
    assert "comment_chars=17" in caplog.text


def test_an_invalid_body_is_a_422_not_a_500():
    assert client.post("/api/feedback", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    assert client.post("/api/feedback", json={"rating": 9}).status_code == 422
    assert client.post("/api/feedback", json={"comment": "x" * 1001}).status_code == 422
