import json

import pytest

from api import manual_retrieve as manual_module


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return self.response


def _manual_result():
    return {
        "summary": "Use the quick wash program.",
        "steps": ["Turn the program dial to Quick Wash.", "Press Start."],
        "warnings": ["Close the door before starting."],
        "needs_clarification": False,
        "confidence": 0.9,
    }


def test_retrieve_manual_uses_user_request_and_optional_hints(monkeypatch):
    model = FakeModel(FakeResponse(json.dumps(_manual_result())))
    monkeypatch.setattr(manual_module, "_get_openrouter_llm", lambda: model)

    result = manual_module.retrieve_manual(
        "How do I run a quick wash?",
        brand="HoDoWo",
        model="HW-100",
    )

    assert result == _manual_result()
    human_text = model.messages[1][1][0]["text"]
    assert "How do I run a quick wash?" in human_text
    assert '"brand": "HoDoWo"' in human_text
    assert '"model": "HW-100"' in human_text


def test_retrieve_manual_sends_original_image(monkeypatch, tmp_path):
    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse(json.dumps(_manual_result())))
    monkeypatch.setattr(manual_module, "_get_openrouter_llm", lambda: model)

    manual_module.retrieve_manual(
        "How do I run a quick wash?", str(image)
    )

    human_content = model.messages[1][1]
    assert human_content[1]["type"] == "image_url"
    assert human_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_retrieve_manual_accepts_json_code_fence(monkeypatch):
    model = FakeModel(FakeResponse(f"```json\n{json.dumps(_manual_result())}\n```"))
    monkeypatch.setattr(manual_module, "_get_openrouter_llm", lambda: model)

    assert manual_module.retrieve_manual("How do I start it?") == _manual_result()


def test_retrieve_manual_rejects_invalid_response(monkeypatch):
    model = FakeModel(FakeResponse('{"summary": "not enough"}'))
    monkeypatch.setattr(manual_module, "_get_openrouter_llm", lambda: model)

    with pytest.raises(RuntimeError, match="missing required fields"):
        manual_module.retrieve_manual("How do I start it?")
