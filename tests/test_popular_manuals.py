"""GET /api/manuals/popular: most searched and most saved guides of all users."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import firebase_store
from endpoints import manuals as manuals_endpoint
from index import app
from jwt_helpers import bearer, make_token

client = TestClient(app, headers=bearer(make_token()))

NOW = datetime.now(timezone.utc)
STEPS = [{"number": 1, "title": "Step 1", "description": "Do it"}]


class FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return self._data


class FakeQuery:
    def __init__(self, docs):
        self.docs = docs
        self.limit_value = None
        self.ordered_by = None

    def order_by(self, field, direction=None):
        self.ordered_by = (field, direction)
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def stream(self):
        items = sorted(self.docs.items(), key=lambda item: item[1]["created_at"], reverse=True)
        return [FakeSnapshot(doc_id, data) for doc_id, data in items][: self.limit_value]


class FakeClient:
    def __init__(self, docs):
        self.query = FakeQuery(docs)

    def collection(self, name):
        assert name == "manuals"
        return self.query


def manual(user, appliance, title, minutes_ago, bookmarked=False, image="https://img.test/a.png", steps=STEPS):
    return {
        "user_uid": user,
        "appliance_name": appliance,
        "appliance_key": firebase_store.appliance_key(appliance),
        "issue_title": title,
        "overview": f"About {title}",
        "steps": steps,
        "warnings": [],
        "image_url": image,
        "is_bookmarked": bookmarked,
        "created_at": NOW - timedelta(minutes=minutes_ago),
    }


@pytest.fixture
def firestore(monkeypatch):
    docs = {
        # Bosch E18: searched 3 times, saved once -> score 5
        "e18-a": manual("u1", "Bosch Washer", "Error E18 drain pump", 50),
        "e18-b": manual("u2", "Bosch Washer", "Error E18: Drain pump!", 30, bookmarked=True),
        "e18-c": manual("u3", "Bosch Washer", "error e18 drain pump", 10, image="data:image/png;base64,AAA"),
        # Oven 180: searched once, saved twice is impossible, so 1 search + 1 save -> score 3
        "oven": manual("u1", "Indesit Oven", "Set 180 degrees with fan", 5, bookmarked=True),
        # Descaling: 1 search -> score 1
        "descale": manual("u4", "DeLonghi Coffee Maker", "Descaling routine", 1),
        # Never ranked: unrecognised appliance, meaningless title, no steps
        "generic": manual("u5", "Household Appliance", "How do I clean it", 2),
        "short": manual("u5", "Bosch Washer", "?", 3),
        "nosteps": manual("u5", "Bosch Washer", "Strange noise", 4, steps=[]),
    }
    fake = FakeClient(docs)
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: fake)
    manuals_endpoint._popular_cache.clear()
    yield fake
    manuals_endpoint._popular_cache.clear()


def test_popular_manuals_are_ranked_by_searches_and_saves(firestore):
    result = client.get("/api/manuals/popular")

    assert result.status_code == 200
    manuals = result.json()["manuals"]
    assert [m["issue_title"] for m in manuals] == [
        "Error E18: Drain pump!",
        "Set 180 degrees with fan",
        "Descaling routine",
    ]
    assert manuals[0]["search_count"] == 3
    assert manuals[0]["save_count"] == 1
    assert manuals[1]["search_count"] == 1
    assert manuals[1]["save_count"] == 1


def test_the_newest_manual_with_a_web_image_represents_the_group(firestore):
    top = client.get("/api/manuals/popular").json()["manuals"][0]

    # e18-c is newer, but its image is a data URI (a copy of a user's photo).
    assert top["id"] == "e18-b"
    assert top["image_url"] == "https://img.test/a.png"
    assert top["created_at_ms"] == int((NOW - timedelta(minutes=10)).timestamp() * 1000)


def test_popular_manuals_share_nothing_about_users(firestore):
    for manual_json in client.get("/api/manuals/popular").json()["manuals"]:
        assert "user_uid" not in manual_json
        assert manual_json["is_bookmarked"] is False


def test_popular_manuals_respect_the_limit(firestore):
    assert len(client.get("/api/manuals/popular?limit=2").json()["manuals"]) == 2
    assert len(client.get("/api/manuals/popular?limit=0").json()["manuals"]) == 1


def test_only_the_newest_manuals_are_read(firestore):
    client.get("/api/manuals/popular")

    assert firestore.query.limit_value == firebase_store.POPULAR_MANUALS_TO_READ
    assert firestore.query.ordered_by[0] == "created_at"


def test_popular_manuals_are_cached(firestore, monkeypatch):
    client.get("/api/manuals/popular")

    def broken():
        raise AssertionError("Firestore should not be read again")

    monkeypatch.setattr(firebase_store, "get_firestore_client", broken)

    assert client.get("/api/manuals/popular").status_code == 200


def test_popular_manuals_require_a_token(firestore):
    assert TestClient(app).get("/api/manuals/popular").status_code == 401


def test_popular_manuals_answer_503_without_firestore(monkeypatch):
    manuals_endpoint._popular_cache.clear()

    def broken():
        raise firebase_store.FirebaseConfigurationError("not configured")

    monkeypatch.setattr(firebase_store, "get_firestore_client", broken)

    assert client.get("/api/manuals/popular").status_code == 503
