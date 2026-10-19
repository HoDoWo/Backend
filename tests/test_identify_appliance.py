import base64
import json

import pytest

import identify_appliance as identify_module


def _result():
    return {
        "appliance_name": "washing machine",
        "brand": "HoDoWo",
        "model": "HW-100",
        "confidence": 0.92,
        "visible_evidence": "Brand and model label visible on the front panel",
        "needs_better_image": False,
    }


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


def test_identify_appliance_sends_image_and_context(tmp_path, monkeypatch):
    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse(json.dumps(_result())))
    monkeypatch.setattr(identify_module, "_get_openrouter_llm", lambda: model)

    result = identify_module.identify_appliance(str(image), "Read the model label")

    assert result == _result()
    human_content = model.messages[1][1]
    assert human_content[0] == {
        "type": "text",
        "text": "Read the model label",
    }
    assert human_content[1]["type"] == "image_url"
    assert human_content[1]["image_url"]["url"] == (
        "data:image/jpeg;base64,"
        + base64.b64encode(b"image-bytes").decode("ascii")
    )


def test_identify_appliance_accepts_json_code_fence(tmp_path, monkeypatch):
    image = tmp_path / "appliance.png"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse(f"```json\n{json.dumps(_result())}\n```"))
    monkeypatch.setattr(identify_module, "_get_openrouter_llm", lambda: model)

    assert identify_module.identify_appliance(str(image)) == _result()


def test_identify_appliance_rejects_invalid_response(tmp_path, monkeypatch):
    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse('{"brand": "HoDoWo"}'))
    monkeypatch.setattr(identify_module, "_get_openrouter_llm", lambda: model)

    with pytest.raises(RuntimeError, match="missing required fields"):
        identify_module.identify_appliance(str(image))


def test_identify_appliance_without_image_returns_guidance():
    result = identify_module.identify_appliance(None)

    assert result["confidence"] == 0
    assert result["needs_better_image"] is True
    assert "attaching an image" in result["visible_evidence"]
