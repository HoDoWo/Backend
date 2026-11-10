"""HTTP layer of the sell-form AI helpers and the listing photo upload."""

from __future__ import annotations

from fastapi.testclient import TestClient

import listing_ai
from index import app
from jwt_helpers import bearer, make_token


client = TestClient(app, headers=bearer(make_token(sub="seller-1")))
anonymous = TestClient(app)


def test_scan_label_requires_a_token():
    result = anonymous.post(
        "/api/marketplace/listings/scan-label",
        files={"image": ("label.jpg", b"image-bytes", "image/jpeg")},
    )

    assert result.status_code == 401


def test_scan_label_reads_brand_and_model(monkeypatch, tmp_path):
    captured = {}

    def scan(image_path, context):
        captured["exists"] = __import__("pathlib").Path(image_path).is_file()
        captured["context"] = context
        return {
            "brand": "Bosch",
            "model": "WAN28201GB",
            "part_number": "00145787",
            "confidence": 0.9,
            "visible_evidence": "Door sticker",
        }

    monkeypatch.setattr(listing_ai, "scan_model_label", scan)

    result = client.post(
        "/api/marketplace/listings/scan-label",
        data={"context": "Read the model plate"},
        files={"image": ("label.jpg", b"image-bytes", "image/jpeg")},
    )

    assert result.status_code == 200
    assert result.json()["model"] == "WAN28201GB"
    assert result.json()["part_number"] == "00145787"
    assert captured == {"exists": True, "context": "Read the model plate"}


def test_scan_label_needs_an_image():
    result = client.post("/api/marketplace/listings/scan-label", data={"context": "x"})

    assert result.status_code == 415


def test_polish_returns_the_rewritten_description(monkeypatch):
    monkeypatch.setattr(
        listing_ai, "polish_listing", lambda text: "Genuine OEM motor, tested and working."
    )

    result = client.post(
        "/api/marketplace/listings/polish",
        json={"description": "genuine oem motor tested works"},
    )

    assert result.status_code == 200
    assert result.json() == {"description": "Genuine OEM motor, tested and working."}


def test_estimate_passes_every_form_field(monkeypatch):
    captured = {}

    def estimate(details):
        captured.update(details)
        return {"low": 18.0, "high": 26.0, "currency": "USD", "rationale": "Similar sales."}

    monkeypatch.setattr(listing_ai, "estimate_listing", estimate)

    result = client.post(
        "/api/marketplace/listings/estimate",
        json={
            "title": "Bosch drain pump motor",
            "description": "Tested and working.",
            "category": "washing-machine",
            "brand": "Bosch",
            "model": "WAN28201GB",
            "condition": "used-good",
            "listing_kind": "part",
            "photo_count": 2,
            "pickup": True,
            "shipping": True,
            "location": "98101",
        },
    )

    assert result.status_code == 200
    assert result.json()["low"] == 18.0
    assert captured["brand"] == "Bosch"
    assert captured["photo_count"] == 2
    assert captured["location"] == "98101"


def test_estimate_maps_ai_failures_to_502(monkeypatch):
    def fail(details):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(listing_ai, "estimate_listing", fail)

    result = client.post("/api/marketplace/listings/estimate", json={"title": "x"})

    assert result.status_code == 502



