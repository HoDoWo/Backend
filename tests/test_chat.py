from pathlib import Path

from fastapi.testclient import TestClient

from index import app
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
    monkeypatch.setattr("langchain_setup.build_agent", lambda image_path: agent)
    monkeypatch.setattr("langchain_setup.ensure_image_overlay", lambda *args: None)

    result = client.post("/api/chat", json={"message": "Hello"})

    assert result.status_code == 200
    assert result.json() == {"reply": "Hello from the agent", "image": None}
    assert agent.request["messages"][0]["content"] == "Hello"


def test_chat_passes_conversation_history_to_langchain_agent(monkeypatch):
    agent = FakeAgent(response="You like coffee!")
    monkeypatch.setattr("langchain_setup.build_agent", lambda image_path: agent)
    monkeypatch.setattr("langchain_setup.ensure_image_overlay", lambda *args: None)

    history = [
        {"role": "user", "content": "I like coffee"},
        {"role": "assistant", "content": "Coffee is great!"}
    ]
    result = client.post("/api/chat", json={"message": "What do I like?", "history": history})

    assert result.status_code == 200
    assert result.json() == {"reply": "You like coffee!", "image": None}
    assert len(agent.request["messages"]) == 3
    assert agent.request["messages"][0] == {"role": "user", "content": "I like coffee"}
    assert agent.request["messages"][1] == {"role": "assistant", "content": "Coffee is great!"}
    assert agent.request["messages"][2] == {"role": "user", "content": "What do I like?"}


def test_chat_parses_json_string_history_from_form_data(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr("langchain_setup.build_agent", lambda image_path: agent)
    monkeypatch.setattr("langchain_setup.ensure_image_overlay", lambda *args: None)

    result = client.post(
        "/api/chat",
        data={
            "message": "Continue",
            "history": '[{"role": "user", "content": "Turn 1"}, {"role": "assistant", "content": "Reply 1"}]'
        }
    )

    assert result.status_code == 200
    assert len(agent.request["messages"]) == 3
    assert agent.request["messages"][0] == {"role": "user", "content": "Turn 1"}
    assert agent.request["messages"][1] == {"role": "assistant", "content": "Reply 1"}
    assert agent.request["messages"][2] == {"role": "user", "content": "Continue"}



def test_chat_accepts_image_without_text(monkeypatch):
    agent = FakeAgent()
    captured = {}

    def fake_build_agent(image_path):
        captured["image_path"] = image_path
        return agent

    monkeypatch.setattr("langchain_setup.build_agent", fake_build_agent)
    monkeypatch.setattr("langchain_setup.ensure_image_overlay", lambda *args: None)

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
    monkeypatch.setattr("langchain_setup.build_agent", lambda image_path: agent)
    monkeypatch.setattr("langchain_setup.ensure_image_overlay", lambda *args: None)

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

    monkeypatch.setattr("langchain_setup.build_agent", fake_build_agent)
    monkeypatch.setattr("langchain_setup.ensure_image_overlay", lambda *args: None)

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
    monkeypatch.setattr("langchain_setup.build_agent", lambda image_path: FakeAgent())

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

    monkeypatch.setattr("langchain_setup.build_agent", lambda image_path: ImageAgent())
    monkeypatch.setattr("langchain_setup.ensure_image_overlay", lambda *args: None)

    result = client.post("/api/chat", json={"message": "How do I start it?"})

    assert result.status_code == 200
    assert result.json() == {
        "reply": "Press Start after selecting Quick Wash.",
        "image": "https://output.example/guide.png",
    }


def test_format_chat_response_creates_manual_guide_card_from_tool_result():
    from response_formatting import format_chat_response as _format_chat_response

    class ManualMessage:
        content = (
            "Here are the steps:\n"
            "1. **Power On** — Flip the switch.\n"
            "2. **Start** — Press the orange button."
        )

    class ManualToolMessage:
        content = (
            '{"appliance_name": "washing machine", "brand": "Bosch", '
            '"model": "WAT-1", "summary": "Start a wash cycle.", '
            '"steps": ["Power On: Flip the switch.", '
            '"Start: Press the orange button."], '
            '"warnings": ["Keep the door closed."]}'
        )

    formatted = _format_chat_response(
        [ManualToolMessage(), ManualMessage()],
        None,
        "How do I start it?",
        "https://output.example/guide.png",
    )

    assert formatted["is_manual"] is True
    assert formatted["appliance_name"] == "Bosch washing machine (WAT-1)"
    assert formatted["issue_title"] == "How do I start it?"
    assert formatted["overview"] == "Start a wash cycle."
    assert formatted["steps"] == [
        {"number": 1, "title": "Power On", "description": "Flip the switch."},
        {"number": 2, "title": "Start", "description": "Press the orange button."},
    ]
    assert formatted["image"] == "https://output.example/guide.png"


def test_format_chat_response_keeps_conversational_messages_compact():
    from response_formatting import format_chat_response as _format_chat_response

    class ConversationMessage:
        content = "You can attach a photo of the appliance and ask a question."

    assert _format_chat_response([ConversationMessage()], None, "Hello") == {
        "reply": "You can attach a photo of the appliance and ask a question.",
        "image": None,
    }
