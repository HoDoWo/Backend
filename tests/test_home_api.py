"""Endpoints used by the Home screen: recent manuals and the daily tip."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import firebase_store
from endpoints import environmental_tip
from index import app
from jwt_helpers import bearer, make_token

client = TestClient(app, headers=bearer(make_token()))

NOW = datetime.now(timezone.utc)


# ---------- fake Firestore ----------


class FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return self._data


class FakeQuery:
    def __init__(self, docs, field, value):
        self.docs = docs
        self.field = field
        self.value = value
        self.limit_value = None

    def limit(self, value):
        self.limit_value = value
        return self

    def stream(self):
        hits = [
            FakeSnapshot(doc_id, data)
            for doc_id, data in self.docs.items()
            if data.get(self.field) == self.value
        ]
        return hits[: self.limit_value]


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def where(self, filter):
        return FakeQuery(self.docs, filter.field_path, filter.value)


class FakeClient:
    def __init__(self, docs):
        self.docs = docs

    def collection(self, name):
        assert name == "manuals"
        return FakeCollection(self.docs)


def manual(user, title, created, bookmarked=False):
    return {
        "user_uid": user,
        "appliance_name": "Bosch Washer",
        "issue_title": title,
        "overview": f"About {title}",
        "steps": [{"number": 1, "title": "Step 1", "description": "Do it"}],
        "warnings": [],
        "image_url": None,
        "is_bookmarked": bookmarked,
        "created_at": created,
    }


@pytest.fixture
def firestore(monkeypatch):
    docs = {
        "old": manual("user-123", "Old guide", NOW - timedelta(days=3)),
        "new": manual("user-123", "New guide", NOW, bookmarked=True),
        "mid": manual("user-123", "Middle guide", NOW - timedelta(hours=2)),
        "other": manual("someone-else", "Not mine", NOW),
    }
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: FakeClient(docs))
    return docs


# ---------- GET /api/manuals ----------


def test_recent_manuals_are_the_users_own_newest_first(firestore):
    result = client.get("/api/manuals")

    assert result.status_code == 200
    manuals = result.json()["manuals"]
    assert [m["issue_title"] for m in manuals] == ["New guide", "Middle guide", "Old guide"]
    assert manuals[0]["id"] == "new"
    assert manuals[0]["created_at"] == NOW.isoformat()
    assert manuals[0]["created_at_ms"] == int(NOW.timestamp() * 1000)
    assert manuals[0]["steps"][0]["title"] == "Step 1"


def test_recent_manuals_respect_the_limit(firestore):
    result = client.get("/api/manuals?limit=2")

    assert [m["issue_title"] for m in result.json()["manuals"]] == ["New guide", "Middle guide"]


def test_limit_is_clamped(firestore):
    assert len(client.get("/api/manuals?limit=0").json()["manuals"]) == 1
    assert len(client.get("/api/manuals?limit=999").json()["manuals"]) == 3


def test_only_bookmarked_manuals_on_request(firestore):
    result = client.get("/api/manuals?bookmarked=true")

    assert [m["issue_title"] for m in result.json()["manuals"]] == ["New guide"]


def test_recent_manuals_require_a_token(firestore):
    assert TestClient(app).get("/api/manuals").status_code == 401


def test_recent_manuals_answer_503_without_firestore(monkeypatch):
    def broken():
        raise firebase_store.FirebaseConfigurationError("not configured")

    monkeypatch.setattr(firebase_store, "get_firestore_client", broken)

    assert client.get("/api/manuals").status_code == 503


# ---------- GET /api/appliances/environmental-tip ----------


class FakeLlm:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return type("Answer", (), {"content": self.answer})()


@pytest.fixture(autouse=True)
def empty_tip_cache():
    environmental_tip._tip_cache.clear()
    yield
    environmental_tip._tip_cache.clear()


def test_tip_is_returned_as_clean_text(monkeypatch):
    llm = FakeLlm('  "Run the dishwasher only when it is full."  ')
    monkeypatch.setattr(config, "get_openrouter_llm", lambda: llm)

    result = client.get("/api/appliances/environmental-tip")

    assert result.status_code == 200
    assert result.json() == {"tip": "Run the dishwasher only when it is full."}


def test_tip_is_cached_for_the_day(monkeypatch):
    llm = FakeLlm("Wash at 30 degrees.")
    monkeypatch.setattr(config, "get_openrouter_llm", lambda: llm)

    client.get("/api/appliances/environmental-tip")
    client.get("/api/appliances/environmental-tip")

    assert llm.calls == 1


def test_tip_failure_answers_502(monkeypatch):
    monkeypatch.setattr(config, "get_openrouter_llm", lambda: FakeLlm(RuntimeError("down")))

    assert client.get("/api/appliances/environmental-tip").status_code == 502


def test_empty_tip_answers_502(monkeypatch):
    monkeypatch.setattr(config, "get_openrouter_llm", lambda: FakeLlm("   "))

    assert client.get("/api/appliances/environmental-tip").status_code == 502


def test_long_tips_are_shortened():
    tip = environmental_tip.clean_tip("word " * 200)

    assert len(tip) <= environmental_tip.MAX_TIP_LENGTH
    assert tip.endswith("...")


def test_tip_requires_a_token():
    assert TestClient(app).get("/api/appliances/environmental-tip").status_code == 401
