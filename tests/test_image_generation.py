import json

import pytest

from api import image_generation as image_module


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_generate_overlay_uploads_original_and_polls_kie(tmp_path, monkeypatch):
    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image-bytes")
    requests_seen = []

    def fake_post(url, **kwargs):
        requests_seen.append((url, kwargs))
        if url == image_module.KIE_UPLOAD_URL:
            return FakeResponse({"code": 200, "data": {"fileUrl": "https://input.example/image.jpg"}})
        return FakeResponse({"code": 200, "data": {"taskId": "task-123"}})

    monkeypatch.setenv("KIE_API_KEY", "test-key")
    monkeypatch.setenv("KIE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(image_module.requests, "post", fake_post)
    monkeypatch.setattr(
        image_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {
                "code": 200,
                "data": {
                    "state": "success",
                    "resultJson": json.dumps(
                        {"resultUrls": ["https://output.example/guide.png"]}
                    ),
                },
            }
        ),
    )

    result = image_module.generate_overlay(
        str(image), "Explain how to start a cycle.", ["Turn the dial", "Press Start"]
    )

    assert result["image_url"] == "https://output.example/guide.png"
    assert requests_seen[0][0] == image_module.KIE_UPLOAD_URL
    task_payload = requests_seen[1][1]["json"]
    assert task_payload["model"] == "gpt-image-2-5"
    assert "1. Turn the dial" in task_payload["input"]["prompt"]
    assert requests_seen[0][1]["headers"]["Authorization"] == "Bearer test-key"


def test_generate_overlay_requires_image():
    with pytest.raises(ValueError, match="image_path is required"):
        image_module.generate_overlay(None, "Instructions", ["Step"])
