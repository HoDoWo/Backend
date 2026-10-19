from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api import ratings
from api.index import app
from api.ratings import ApplianceRef, appliance_id, build_rating_context
from jwt_helpers import bearer, make_token


client = TestClient(app, headers=bearer(make_token()))

VALID_RATING = {
    "question": "Why does it leak?",
    "answer": "Check the door seal.",
    "rating": 1,
    "comment": None,
    "appliance": {"name": "Washing machine", "brand": "Bosch", "model": "WAN28"},
}


class FakeStore:
    def __init__(self, records=None, fail=False):
        self.added = []
        self.records = records or []
        self.fail = fail
        self.read_keys = []

    def add(self, record):
        if self.fail:
            raise RuntimeError("firestore down")
        self.added.append(record)

    def recent_for(self, appliance_id, limit):
        if self.fail:
            raise RuntimeError("firestore down")
        self.read_keys.append(appliance_id)
        return self.records


class FakeAgent:
    def __init__(self):
        self.request = None

    def invoke(self, request):
        self.request = request
        return {"messages": [type("Message", (), {"content": "ok"})()]}


# ---------- POST /api/feedback ----------


def test_feedback_is_stored_with_the_user_from_the_token(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("api.index.get_store", lambda: store)

    result = client.post("/api/feedback", json=VALID_RATING)

    assert result.status_code == 201
    assert result.json() == {"status": "stored"}

    record = store.added[0]
    assert record["user_id"] == "user-123"
    assert record["appliance_id"] == "bosch/wan28"
    assert record["prompt_query"] == "Why does it leak?"
    assert record["ai_response_text"] == "Check the door seal."
    assert record["rating"] == 1
    assert record["feedback_text"] is None
    assert record["created_at"].tzinfo is not None


def test_dislike_keeps_the_trimmed_comment(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("api.index.get_store", lambda: store)

    body = {**VALID_RATING, "rating": -1, "comment": "  too generic  "}
    result = client.post("/api/feedback", json=body)

    assert result.status_code == 201
    assert store.added[0]["rating"] == -1
    assert store.added[0]["feedback_text"] == "too generic"


def test_feedback_rejects_invalid_rating(monkeypatch):
    monkeypatch.setattr("api.index.get_store", lambda: FakeStore())

    result = client.post("/api/feedback", json={**VALID_RATING, "rating": 5})

    assert result.status_code == 422


def test_feedback_rejects_missing_question(monkeypatch):
    monkeypatch.setattr("api.index.get_store", lambda: FakeStore())

    body = {key: value for key, value in VALID_RATING.items() if key != "question"}
    result = client.post("/api/feedback", json=body)

    assert result.status_code == 422


def test_feedback_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr("api.index.get_store", lambda: FakeStore())

    result = client.post(
        "/api/feedback",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )

    assert result.status_code == 422


def test_feedback_without_storage_answers_503(monkeypatch):
    monkeypatch.setattr("api.index.get_store", lambda: None)

    result = client.post("/api/feedback", json=VALID_RATING)

    assert result.status_code == 503


def test_feedback_storage_error_answers_502(monkeypatch):
    monkeypatch.setattr("api.index.get_store", lambda: FakeStore(fail=True))

    result = client.post("/api/feedback", json=VALID_RATING)

    assert result.status_code == 502


def test_feedback_requires_a_token():
    anonymous = TestClient(app)

    result = anonymous.post("/api/feedback", json=VALID_RATING)

    assert result.status_code == 401


# ---------- previous ratings in /api/chat ----------


def test_chat_adds_previous_ratings_for_the_same_appliance(monkeypatch):
    now = datetime.now(timezone.utc)
    store = FakeStore(
        records=[
            {
                "rating": 1,
                "prompt_query": "How do I clean the filter?",
                "ai_response_text": "Open the small door at the bottom.",
                "created_at": now,
            },
            {
                "rating": -1,
                "prompt_query": "Error E18?",
                "ai_response_text": "Call a technician.",
                "feedback_text": "too generic",
                "created_at": now - timedelta(days=1),
            },
        ]
    )
    agent = FakeAgent()
    monkeypatch.setattr("api.index.get_store", lambda: store)
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)

    result = client.post(
        "/api/chat",
        json={"message": "Why does it leak?", "appliance": VALID_RATING["appliance"]},
    )

    assert result.status_code == 200
    assert store.read_keys == ["bosch/wan28"]

    prompt = agent.request["messages"][0]["content"]
    assert "Rated helpful" in prompt
    assert "Open the small door at the bottom." in prompt
    assert 'Rated NOT helpful. Reason: "too generic"' in prompt
    assert prompt.endswith("User question: Why does it leak?")


def test_chat_with_photo_reads_the_appliance_form_field(monkeypatch):
    store = FakeStore()
    agent = FakeAgent()
    monkeypatch.setattr("api.index.get_store", lambda: store)
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)
    monkeypatch.setattr("api.index.ensure_image_overlay", lambda *args: None)

    result = client.post(
        "/api/chat",
        data={
            "message": "Why does it leak?",
            "appliance": '{"name": "Washing machine", "brand": "Bosch", "model": "WAN28"}',
        },
        files={"image": ("photo.jpg", b"fake-jpeg", "image/jpeg")},
    )

    assert result.status_code == 200
    assert store.read_keys == ["bosch/wan28"]


def test_chat_ignores_an_appliance_field_that_is_not_json(monkeypatch):
    store = FakeStore()
    agent = FakeAgent()
    monkeypatch.setattr("api.index.get_store", lambda: store)
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)
    monkeypatch.setattr("api.index.ensure_image_overlay", lambda *args: None)

    result = client.post(
        "/api/chat",
        data={"message": "Hello", "appliance": "not json"},
        files={"image": ("photo.jpg", b"fake-jpeg", "image/jpeg")},
    )

    assert result.status_code == 200
    assert store.read_keys == []


def test_chat_without_appliance_does_not_read_ratings(monkeypatch):
    store = FakeStore()
    agent = FakeAgent()
    monkeypatch.setattr("api.index.get_store", lambda: store)
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)

    result = client.post("/api/chat", json={"message": "Hello"})

    assert result.status_code == 200
    assert store.read_keys == []
    assert agent.request["messages"][0]["content"] == "Hello"


def test_chat_still_works_when_ratings_cannot_be_read(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr("api.index.get_store", lambda: FakeStore(fail=True))
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)

    result = client.post(
        "/api/chat",
        json={"message": "Hello", "appliance": VALID_RATING["appliance"]},
    )

    assert result.status_code == 200
    assert agent.request["messages"][0]["content"] == "Hello"


def test_chat_ignores_an_invalid_appliance(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr("api.index.get_store", lambda: FakeStore())
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)

    result = client.post("/api/chat", json={"message": "Hello", "appliance": "oven"})

    assert result.status_code == 200
    assert agent.request["messages"][0]["content"] == "Hello"


# ---------- helpers ----------


def test_appliance_id_prefers_brand_and_model():
    assert appliance_id(ApplianceRef(name="Washing machine", brand="Bosch", model="WAN 28")) == "bosch/wan-28"
    assert appliance_id(ApplianceRef(name="Washing machine", brand="Bosch")) == "bosch/washing-machine"
    assert appliance_id(ApplianceRef(name="Oven")) == "oven"
    assert appliance_id(ApplianceRef(name="")) == ""
    assert appliance_id(None) == ""


def test_rating_context_limits_the_number_of_examples():
    records = [
        {"rating": 1, "prompt_query": f"q{i}", "ai_response_text": f"a{i}"} for i in range(10)
    ] + [
        {"rating": -1, "prompt_query": f"bad{i}", "ai_response_text": f"no{i}"} for i in range(10)
    ]

    context = build_rating_context(records)

    assert context.count("Rated helpful") == ratings.MAX_HELPFUL_IN_PROMPT
    assert context.count("Rated NOT helpful") == ratings.MAX_UNHELPFUL_IN_PROMPT


def test_rating_context_is_empty_without_ratings():
    assert build_rating_context([]) == ""


def test_long_texts_are_shortened():
    record = {"rating": 1, "prompt_query": "q", "ai_response_text": "x" * 2000}

    context = build_rating_context([record])

    assert "x" * ratings.EXCERPT_LENGTH not in context
    assert "..." in context


def test_store_is_disabled_without_service_account(monkeypatch):
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT", raising=False)
    ratings.get_store.cache_clear()

    assert ratings.get_store() is None

    ratings.get_store.cache_clear()


def test_firestore_store_uses_the_collection_and_sorts_newest_first():
    now = datetime.now(timezone.utc)

    class Snapshot:
        def __init__(self, data):
            self.data = data

        def to_dict(self):
            return self.data

    class Query:
        def __init__(self, collection):
            self.collection = collection

        def limit(self, limit):
            self.collection.limit_value = limit
            return self

        def stream(self):
            return [Snapshot(record) for record in self.collection.records]

    class Collection:
        def __init__(self):
            self.added = []
            self.records = [
                {"prompt_query": "old", "created_at": now - timedelta(days=2)},
                {"prompt_query": "new", "created_at": now},
                {"prompt_query": "no date"},
            ]
            self.filter = None
            self.limit_value = None

        def add(self, record):
            self.added.append(record)

        def where(self, filter):
            self.filter = filter
            return Query(self)

    class Client:
        def __init__(self):
            self.collection_name = None
            self.the_collection = Collection()

        def collection(self, name):
            self.collection_name = name
            return self.the_collection

    client_double = Client()
    store = ratings.FirestoreRatingsStore(client_double)

    store.add({"rating": 1})
    records = store.recent_for("bosch/wan28", 50)

    assert client_double.collection_name == "ai_response_ratings"
    assert client_double.the_collection.added == [{"rating": 1}]
    assert client_double.the_collection.limit_value == 50
    assert [r["prompt_query"] for r in records] == ["new", "old", "no date"]
