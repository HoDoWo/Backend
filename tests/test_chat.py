from pathlib import Path

from fastapi.testclient import TestClient

from api.index import app
from jwt_helpers import bearer, make_token


client = TestClient(app, headers=bearer(make_token()))


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
    monkeypatch.setattr("api.index.ensure_image_overlay", lambda *args: None)

    result = client.post("/api/chat", json={"message": "Hello"})

    assert result.status_code == 200
    assert result.json() == {"reply": "Hello from the agent", "image": None}
    assert agent.request["messages"][0]["content"] == "Hello"


def test_chat_accepts_image_without_text(monkeypatch):
    agent = FakeAgent()
    captured = {}

    def fake_build_agent(image_path):
        captured["image_path"] = image_path
        return agent

    monkeypatch.setattr("api.index.build_agent", fake_build_agent)
    monkeypatch.setattr("api.index.ensure_image_overlay", lambda *args: None)

    result = client.post(
        "/api/chat",
        files={"image": ("appliance.jpg", b"image-content", "image/jpeg")},
    )

    assert result.status_code == 200
    assert result.json() == {"reply": "Hello from the agent", "image": None}
    assert "Uploaded image path:" in agent.request["messages"][0]["content"]
    assert not Path(captured["image_path"]).exists()


def test_chat_accepts_text_and_image(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr("api.index.build_agent", lambda image_path: agent)
    monkeypatch.setattr("api.index.ensure_image_overlay", lambda *args: None)

    result = client.post(
        "/api/chat",
        data={"message": "How do I clean it?"},
        files={"image": ("appliance.jpg", b"image-content", "image/jpeg")},
    )

    assert result.status_code == 200
    assert result.json() == {"reply": "Hello from the agent", "image": None}
    assert "How do I clean it?" in agent.request["messages"][0]["content"]


def test_chat_accepts_image_with_nonstandard_field_name(monkeypatch):
    agent = FakeAgent()
    captured = {}

    def fake_build_agent(image_path):
        captured["image_path"] = image_path
        return agent

    monkeypatch.setattr("api.index.build_agent", fake_build_agent)
    monkeypatch.setattr("api.index.ensure_image_overlay", lambda *args: None)

    result = client.post(
        "/api/chat",
        data={"text": "How do I prepare coffee?"},
        files={"photo": ("coffee-maker.jpg", b"image-content", "image/jpeg")},
    )

    assert result.status_code == 200
    assert result.json() == {"reply": "Hello from the agent", "image": None}
    assert "Uploaded image path:" in agent.request["messages"][0]["content"]
    assert not Path(captured["image_path"]).exists()


def test_chat_requires_text_or_image(monkeypatch):
    monkeypatch.setattr("api.index.build_agent", lambda image_path: FakeAgent())

    result = client.post("/api/chat", json={})

    assert result.status_code == 422
    assert result.json() == {
        "detail": "provide at least one of image or message",
    }


def test_chat_returns_generated_image_with_answer(monkeypatch):
    class ToolMessage:
        content = (
            '{"task_id": "task-123", "image_url": '
            '"https://output.example/guide.png", '
            '"message": "Generated the appliance guide."}'
        )
    
    class ImageAgent(FakeAgent):
        def invoke(self, request):
            self.request = request
            return {
                "messages": [
                    ToolMessage(),
                    type("Message", (), {"content": "Press Start after selecting Quick Wash."})(),
                ]
            }

    monkeypatch.setattr("api.index.build_agent", lambda image_path: ImageAgent())
    monkeypatch.setattr("api.index.ensure_image_overlay", lambda *args: None)

    result = client.post("/api/chat", json={"message": "How do I start it?"})

    assert result.status_code == 200
    assert result.json() == {
        "reply": "Press Start after selecting Quick Wash.",
        "image": "https://output.example/guide.png",
    }
