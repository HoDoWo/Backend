from pathlib import Path

from fastapi.testclient import TestClient

from api.index import app


client = TestClient(app)


class FakeAgent:
    def __init__(self, response: str = "Hello from the agent"):
        self.response = response
        self.request = None

    def invoke(self, request):
        self.request = request
        return {"messages": [type("Message", (), {"content": self.response})()]}


def test_chat_sends_text_to_langchain_agent(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)

    result = client.post("/api/chat", json={"message": "Hello"})

    assert result.status_code == 200
    assert result.json() == {"answer": "Hello from the agent"}
    assert agent.request["messages"][0]["content"] == "Hello"


def test_chat_accepts_image_without_text(monkeypatch):
    agent = FakeAgent()
    captured = {}

    def fake_build_agent(image_path):
        captured["image_path"] = image_path
        return agent

    monkeypatch.setattr("api.index.build_agent", fake_build_agent)

    result = client.post(
        "/api/chat",
        files={"image": ("appliance.jpg", b"image-content", "image/jpeg")},
    )

    assert result.status_code == 200
    assert result.json() == {"answer": "Hello from the agent"}
    assert "Uploaded image path:" in agent.request["messages"][0]["content"]
    assert not Path(captured["image_path"]).exists()


def test_chat_accepts_text_and_image(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)

    result = client.post(
        "/api/chat",
        data={"message": "How do I clean it?"},
        files={"image": ("appliance.jpg", b"image-content", "image/jpeg")},
    )

    assert result.status_code == 200
    assert result.json() == {"answer": "Hello from the agent"}
    assert "How do I clean it?" in agent.request["messages"][0]["content"]


def test_chat_accepts_image_with_nonstandard_field_name(monkeypatch):
    agent = FakeAgent()
    captured = {}

    def fake_build_agent(image_path):
        captured["image_path"] = image_path
        return agent

    monkeypatch.setattr("api.index.build_agent", fake_build_agent)

    result = client.post(
        "/api/chat",
        data={"text": "How do I prepare coffee?"},
        files={"photo": ("coffee-maker.jpg", b"image-content", "image/jpeg")},
    )

    assert result.status_code == 200
    assert result.json() == {"answer": "Hello from the agent"}
    assert "Uploaded image path:" in agent.request["messages"][0]["content"]
    assert not Path(captured["image_path"]).exists()


def test_chat_requires_text_or_image(monkeypatch):
    monkeypatch.setattr("api.index.build_agent", lambda image_path: FakeAgent())

    result = client.post("/api/chat", json={})

    assert result.status_code == 422
    assert result.json() == {
        "detail": "provide at least one of image or message",
    }
