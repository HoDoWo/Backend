from fastapi.testclient import TestClient

import firebase_store
from index import app
from jwt_helpers import bearer, make_token


client = TestClient(app)


def test_search_returns_mixed_catalogue(monkeypatch):
    monkeypatch.setattr(
        firebase_store,
        "search_marketplace",
        lambda **kwargs: {
            "items": [
                {
                    "id": "p1",
                    "title": "Drain pump",
                    "description": "Compatible pump",
                    "category": "parts",
                    "condition": "new",
                    "type": "part",
                    "price": 24.0,
                    "image_urls": [],
                    "seller_name": "Partner",
                    "partner_name": "Partner",
                    "sponsored": True,
                    "status": "active",
                }
            ],
            "next_cursor": None,
        },
    )

    response = client.get(
        "/api/marketplace/search?q=drain&type=part",
        headers=bearer(make_token()),
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["sponsored"] is True


def test_create_listing_uses_authenticated_owner(monkeypatch):
    captured = {}

    def create(uid, payload, seller_name):
        captured.update(uid=uid, payload=payload, seller_name=seller_name)
        return {
            "id": "listing-1",
            **payload,
            "type": "used",
            "seller_name": seller_name,
            "status": "active",
            "sponsored": False,
        }

    monkeypatch.setattr(firebase_store, "create_marketplace_listing", create)
    response = client.post(
        "/api/marketplace/listings",
        headers=bearer(make_token(sub="owner-1")),
        json={
            "title": "Used washer motor",
            "description": "Tested and working motor for a washer.",
            "category": "replacement part",
            "condition": "used",
            "price": 45,
            "image_urls": ["https://example.test/motor.jpg"],
        },
    )

    assert response.status_code == 201
    assert captured["uid"] == "owner-1"
    assert response.json()["id"] == "listing-1"


def test_create_listing_rejects_empty_images():
    response = client.post(
        "/api/marketplace/listings",
        headers=bearer(make_token()),
        json={
            "title": "Used washer motor",
            "description": "Tested and working motor for a washer.",
            "category": "replacement part",
            "condition": "used",
            "price": 45,
            "image_urls": [],
        },
    )

    assert response.status_code == 422
