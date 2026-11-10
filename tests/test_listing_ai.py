"""AI helpers of the sell form: label scan, polish and price estimate."""

from __future__ import annotations

import base64
import json

import pytest

import listing_ai


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


def test_scan_model_label_reads_the_photo(tmp_path, monkeypatch):
    image = tmp_path / "label.jpg"
    image.write_bytes(b"image-bytes")
    payload = {
        "brand": " Bosch ",
        "model": "WAN28201GB",
        "part_number": "00145787",
        "confidence": 0.91,
        "visible_evidence": "Sticker on the door frame",
    }
    model = FakeModel(FakeResponse(json.dumps(payload)))
    monkeypatch.setattr(listing_ai.config, "get_openrouter_llm", lambda: model)

    result = listing_ai.scan_model_label(str(image), "Read the label")

    assert result["brand"] == "Bosch"
    assert result["model"] == "WAN28201GB"
    assert result["part_number"] == "00145787"
    assert result["confidence"] == 0.91
    human = model.messages[1][1]
    assert human[0] == {"type": "text", "text": "Read the label"}
    assert human[1]["image_url"]["url"] == (
        "data:image/jpeg;base64," + base64.b64encode(b"image-bytes").decode("ascii")
    )


def test_scan_model_label_uses_null_for_unknown_fields(tmp_path, monkeypatch):
    image = tmp_path / "label.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(
        FakeResponse(
            '```json\n{"brand": "LG", "model": null, "part_number": null, '
            '"confidence": 2, "visible_evidence": "No model plate"}\n```'
        )
    )
    monkeypatch.setattr(listing_ai.config, "get_openrouter_llm", lambda: model)

    result = listing_ai.scan_model_label(str(image))

    assert result["brand"] == "LG"
    assert result["model"] is None and result["part_number"] is None
    assert result["confidence"] == 1.0  # clamped


def test_scan_model_label_rejects_invalid_json(tmp_path, monkeypatch):
    image = tmp_path / "label.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse("I cannot read that."))
    monkeypatch.setattr(listing_ai.config, "get_openrouter_llm", lambda: model)

    with pytest.raises(RuntimeError, match="invalid response"):
        listing_ai.scan_model_label(str(image))


def test_polish_listing_returns_the_model_text(monkeypatch):
    model = FakeModel(FakeResponse("  Genuine OEM motor. Brushes replaced recently.  "))
    monkeypatch.setattr(listing_ai.config, "get_openrouter_llm", lambda: model)

    assert listing_ai.polish_listing("genuine oem motor brushes replaced recently") == (
        "Genuine OEM motor. Brushes replaced recently."
    )


def test_polish_listing_keeps_short_or_empty_text_without_a_call(monkeypatch):
    def fail():
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(listing_ai.config, "get_openrouter_llm", fail)

    assert listing_ai.polish_listing("   ") == ""
    assert listing_ai.polish_listing("Barely.") == "Barely."


def test_estimate_listing_returns_a_clean_range(monkeypatch):
    model = FakeModel(
        FakeResponse(
            json.dumps(
                {
                    "low": "$18",
                    "high": 26,
                    "currency": "usd",
                    "rationale": "14 similar motors sold recently.",
                }
            )
        )
    )
    monkeypatch.setattr(listing_ai.config, "get_openrouter_llm", lambda: model)

    result = listing_ai.estimate_listing(
        {
            "title": "Bosch drain pump motor",
            "description": "Tested and working.",
            "category": "washing machine",
            "brand": "Bosch",
            "model": "WAN28201GB",
            "condition": "used-good",
            "listing_kind": "part",
            "photo_count": 2,
            "pickup": True,
            "shipping": True,
            "location": "98101",
        }
    )

    assert result == {
        "low": 18.0,
        "high": 26.0,
        "currency": "usd",
        "rationale": "14 similar motors sold recently.",
    }
    context = model.messages[1][1]
    assert "Bosch" in context and "WAN28201GB" in context and "used-good" in context


def test_estimate_listing_swaps_inverted_prices(monkeypatch):
    model = FakeModel(FakeResponse('{"low": 40, "high": 20, "rationale": "x"}'))
    monkeypatch.setattr(listing_ai.config, "get_openrouter_llm", lambda: model)

    result = listing_ai.estimate_listing({})

    assert result["low"] == 20.0 and result["high"] == 40.0


def test_estimate_listing_rejects_missing_prices(monkeypatch):
    model = FakeModel(FakeResponse('{"low": 0, "high": -2}'))
    monkeypatch.setattr(listing_ai.config, "get_openrouter_llm", lambda: model)

    with pytest.raises(RuntimeError, match="valid prices"):
        listing_ai.estimate_listing({})
