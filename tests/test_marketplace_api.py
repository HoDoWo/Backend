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


def test_search_accepts_condition_and_classification_filters(monkeypatch):
    captured_filters = {}

    def mock_search(**kwargs):
        captured_filters.update(kwargs)
        return {"items": [], "next_cursor": None}

    monkeypatch.setattr(firebase_store, "search_marketplace", mock_search)
    response = client.get(
        "/api/marketplace/search?q=pump&type=appliance&condition=brand-new&brand=Bosch",
        headers=bearer(make_token()),
    )

    assert response.status_code == 200
    assert captured_filters["item_type"] == "appliance"
    assert captured_filters["condition"] == "brand-new"
    assert captured_filters["brand"] == "Bosch"


def test_create_listing_uses_authenticated_owner(monkeypatch):
    captured = {}

    def create(uid, payload, seller_name):
        captured.update(uid=uid, payload=payload, seller_name=seller_name)
        return {
            "id": "listing-1",
            **payload,
            "listing_kind": "part",
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
            "listing_kind": "part",
            "condition": "used-good",
            "price": 45,
            "image_urls": ["https://example.test/motor.jpg"],
            "handover_methods": ["pickup"],
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
            "listing_kind": "part",
            "condition": "used-good",
            "price": 45,
            "image_urls": [],
            "handover_methods": ["pickup"],
        },
    )

    assert response.status_code == 422


def test_create_listing_accepts_appliance_form_fields(monkeypatch):
    monkeypatch.setattr(
        firebase_store,
        "create_marketplace_listing",
        lambda uid, payload, seller_name: {
            "id": "appliance-1",
            **payload,
            "type": "used",
            "seller_name": seller_name,
            "status": "active",
            "sponsored": False,
        },
    )
    response = client.post(
        "/api/marketplace/listings",
        headers=bearer(make_token()),
        json={
            "title": "Bosch Serie 4 washing machine",
            "description": "Fully working appliance with tested cycles and normal wear.",
            "category": "washing machine",
            "brand": "Bosch",
            "model": "WAN28201GB",
            "listing_kind": "appliance",
            "condition": "used-good",
            "price": 220,
            "image_urls": ["https://example.test/washer.jpg"],
            "handover_methods": ["pickup", "shipping"],
            "pickup_zip": "08001",
            "location": "Barcelona",
            "shipping_price_cents": 680,
        },
    )

    assert response.status_code == 201
    assert response.json()["listing_kind"] == "appliance"
    assert response.json()["location"] == "Barcelona"
