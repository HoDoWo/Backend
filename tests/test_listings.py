"""Second-hand listing CRUD and ownership (#35): browse, read, edit, mark as sold."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import checkout_service
import listings_service
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeListingsStore

API = "/api/marketplace/listings"
OWNER, OTHER = "seller-1", "buyer-2"
NOW = 10 * 24 * 60 * 60_000

owner = TestClient(app, headers=bearer(make_token(sub=OWNER)))
other = TestClient(app, headers=bearer(make_token(sub=OTHER)))


def listing(title, owner_uid=OWNER, **fields):
    doc = {
        "title": title, "description": "Working well, tested and cleaned before sale.",
        "category": "washing machine", "brand": "Bosch", "model": "WAN28201GB",
        "listing_kind": "appliance", "condition": "used-good", "price_cents": 22000,
        "image_urls": ["https://example.test/a.jpg"], "handover_methods": ["pickup"],
        "pickup_zip": "08001",
        "owner_uid": owner_uid, "seller_name": "Davide", "status": "active", "quantity": 1,
        "created_at": NOW - 2 * 24 * 60 * 60_000,
    }
    doc.update(fields)
    return doc


@pytest.fixture
def store(monkeypatch):
    fake = FakeListingsStore({
        "washer": listing("Bosch washer", created_at=NOW - 60 * 60_000),
        "fridge": listing("Old fridge", category="refrigerator", price_cents=9000),
        "sold-tv": listing("Sold dryer", status="sold"),
        "other-oven": listing("Other user's oven", owner_uid=OTHER, category="oven", created_at=NOW - 5 * 60_000),
    })
    monkeypatch.setattr(listings_service, "get_store", lambda: fake)
    monkeypatch.setattr(checkout_service, "now_ms", lambda: NOW)
    return fake


# ---------- browse ----------

def test_browsing_shows_active_listings_newest_first(store):
    body = other.get(API).json()

    assert [r["id"] for r in body["results"]] == ["other-oven", "washer", "fridge"]
    washer = body["results"][1]
    assert washer["price"] == 220.0 and washer["condition_label"] == "Used, good"
    assert washer["posted_ago"] == "1h ago" and washer["inquiry_count"] == 0
    assert washer["location"] == "08001"  # falls back to pickup_zip
    assert washer["is_mine"] is False and "owner_uid" not in washer
    assert body["next_cursor"] is None


def test_mine_shows_all_my_listings_including_sold(store):
    results = owner.get(API, params={"mine": "true"}).json()["results"]

    assert [r["id"] for r in results] == ["washer", "fridge", "sold-tv"]
    assert all(r["is_mine"] for r in results)
    assert [r["id"] for r in owner.get(API, params={"mine": "true", "status": "sold"}).json()["results"]] == ["sold-tv"]


def test_filters(store):
    def ids(**params):
        return [r["id"] for r in other.get(API, params=params).json()["results"]]

    assert ids(q="bosch") == ["other-oven", "washer", "fridge"]  # brand matches everywhere
    assert ids(q="fridge") == ["fridge"]
    assert ids(category="Refrigerator") == ["fridge"]
    assert ids(max_price=100) == ["fridge"]
    assert other.get(API, params={"status": "sold"}).status_code == 422  # others' sold listings are hidden
    assert other.get(API, params={"status": "draft"}).status_code == 422


def test_browsing_is_paginated(store):
    first = other.get(API, params={"limit": 2}).json()
    second = other.get(API, params={"limit": 2, "cursor": first["next_cursor"]}).json()

    assert [r["id"] for r in first["results"] + second["results"]] == ["other-oven", "washer", "fridge"]
    assert second["next_cursor"] is None
    assert other.get(API, params={"cursor": "nope"}).status_code == 422


# ---------- read ----------

def test_a_listing_can_be_read_and_a_sold_one_only_by_its_owner(store):
    assert other.get(f"{API}/washer").json()["title"] == "Bosch washer"
    assert owner.get(f"{API}/sold-tv").json()["status"] == "sold"
    assert other.get(f"{API}/sold-tv").status_code == 404
    assert other.get(f"{API}/missing").status_code == 404
    assert other.get(f"{API}/bad.id").status_code == 422


# ---------- edit ----------

def test_the_owner_edits_a_listing(store):
    edited = owner.patch(f"{API}/washer", json={
        "title": "  Bosch washer, 8 kg  ", "price": 199.5, "condition": "Like new",
        "handover_methods": ["shipping"], "location": "Barcelona",
    })

    assert edited.status_code == 200
    body = edited.json()
    assert body["title"] == "Bosch washer, 8 kg" and body["price_cents"] == 19950
    assert body["condition"] == "like-new" and body["condition_label"] == "Like new"
    assert body["location"] == "Barcelona"
    saved = store.listings["washer"]
    assert saved["title_normalised"] == "bosch washer 8 kg" and saved["updated_at"] == NOW
    assert saved["owner_uid"] == OWNER


def test_editing_another_users_listing_is_403(store):
    result = other.patch(f"{API}/washer", json={"price": 1})

    assert result.status_code == 403
    assert store.listings["washer"]["price_cents"] == 22000
    assert other.patch(f"{API}/missing", json={"price": 1}).status_code == 404


def test_a_sold_listing_cannot_be_edited(store):
    assert owner.patch(f"{API}/sold-tv", json={"price": 1}).status_code == 409


@pytest.mark.parametrize(
    "change, field",
    [
        ({"price": 0}, "price"),
        ({"price": 100001}, "price"),
        ({"title": "ab"}, "title"),
        ({"description": "too short"}, "description"),
        ({"image_urls": []}, "image_urls"),
        ({"image_urls": [f"https://e.test/{n}.jpg" for n in range(9)]}, "image_urls"),
        ({"image_urls": ["http://e.test/a.jpg"]}, "image_urls"),
        ({"condition": "broken"}, "condition"),
        ({"handover_methods": ["drone"]}, "handover_methods"),
        ({"quantity": 0}, "quantity"),
        ({"owner_uid": OTHER}, "owner_uid"),
        ({"status": "active"}, "status"),
    ],
)
def test_validation_errors_name_the_field(store, change, field):
    result = owner.patch(f"{API}/washer", json=change)

    assert result.status_code == 422
    assert field in result.json()["detail"][0]["loc"]
    assert store.listings["washer"]["price_cents"] == 22000


def test_a_title_of_spaces_is_refused_after_trimming(store):
    result = owner.patch(f"{API}/washer", json={"title": "   ab   "})

    assert result.status_code == 422 and result.json()["detail"][0]["loc"] == ["body", "title"]


def test_an_empty_edit_is_refused(store):
    assert owner.patch(f"{API}/washer", json={}).json()["detail"] == "nothing to change"


@pytest.mark.parametrize(
    "label, key",
    [("like-new", "like-new"), ("Like new", "like-new"), ("USED, GOOD", "used-good"), ("used good", "used-good"),
     ("For parts", "parts-only"), ("parts only", "parts-only"), ("Unopened", "unopened"), ("refurbished", "refurbished")],
)
def test_condition_accepts_keys_and_labels(label, key):
    assert listings_service.condition_key(label) == key


# ---------- sold ----------

def test_the_owner_marks_a_listing_as_sold(store):
    sold = owner.post(f"{API}/washer/sold")

    assert sold.status_code == 200 and sold.json()["status"] == "sold" and sold.json()["sold_at"] == NOW
    assert owner.post(f"{API}/washer/sold").json()["status"] == "sold"  # repeating is harmless
    assert "washer" not in [r["id"] for r in other.get(API).json()["results"]]


def test_marking_another_users_listing_as_sold_is_403(store):
    assert other.post(f"{API}/washer/sold").status_code == 403
    assert store.listings["washer"]["status"] == "active"
    assert other.post(f"{API}/missing/sold").status_code == 404


def test_posted_ago_labels():
    minute = 60_000
    assert listings_service.posted_ago(None, NOW) == ""
    assert listings_service.posted_ago(NOW, NOW) == "just now"
    assert listings_service.posted_ago(NOW - 5 * minute, NOW) == "5m ago"
    assert listings_service.posted_ago(NOW - 3 * 60 * minute, NOW) == "3h ago"
    assert listings_service.posted_ago(NOW - 2 * 24 * 60 * minute, NOW) == "2d ago"
    assert listings_service.posted_ago(NOW - 9 * 24 * 60 * minute, NOW) == "1w ago"


def test_listings_need_a_token(store):
    assert TestClient(app).get(API).status_code == 401


def test_a_misconfigured_database_answers_503(monkeypatch):
    import firebase_store

    def broken():
        raise firebase_store.FirebaseConfigurationError("no credentials")

    monkeypatch.setattr(listings_service, "get_store", broken)

    assert owner.get(API).status_code == 503
