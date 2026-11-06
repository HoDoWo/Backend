"""Search and user listings work with the cart and the checkout (alignment fix).

These tests run the real firebase_store functions on an in-memory Firestore.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import checkout_service
import firebase_store
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeStore


class Snap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data

    def to_dict(self):
        return dict(self._data)


class Doc:
    def __init__(self, coll, doc_id):
        self.coll, self.id = coll, doc_id

    def set(self, data):
        self.coll.docs[self.id] = dict(data)


class Coll:
    def __init__(self, docs):
        self.docs = docs
        self.counter = 0

    def stream(self):
        return [Snap(doc_id, data) for doc_id, data in self.docs.items()]

    def document(self, doc_id=None):
        if doc_id is None:
            self.counter += 1
            doc_id = f"new-{self.counter}"
        return Doc(self, doc_id)


class Db:
    def __init__(self, data):
        self.colls = {name: Coll(docs) for name, docs in data.items()}

    def collection(self, name):
        return self.colls.setdefault(name, Coll({}))


@pytest.fixture
def db(monkeypatch):
    fake = Db({
        "listings": {
            # an older document saved with item_type "part" before this fix
            "old-pump": {"title": "Used drain pump", "listing_kind": "part", "item_type": "part",
                         "status": "active", "price_cents": 1500, "owner_uid": "seller-1",
                         "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)},
            "sold-tv": {"title": "Old TV", "listing_kind": "appliance", "status": "sold", "price_cents": 900},
            "empty": {"title": "Pans", "listing_kind": "appliance", "status": "active", "quantity": 0,
                      "price_cents": 900},
        },
        "parts": {
            "pump-1": {"title": "Drain pump", "price_cents": 2500, "stock": 0, "created_at": 2},
            "belt-1": {"title": "Drum belt", "price_cents": 900, "stock": 4, "created_at": 1},
            "legacy": {"title": "Legacy filter", "price_cents": 500, "available": True, "created_at": 3},
        },
        "partner_products": {
            "oven-1": {"title": "Oven", "price_cents": 39900, "stock": 2, "partner_name": "Partner A",
                       "sponsored": True, "created_at": 5},
        },
    })
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: fake)
    return fake


def search(**overrides):
    params = dict(query="", item_type="all", category=None, brand=None, min_price=None,
                  max_price=None, cursor=None, limit=20)
    params.update(overrides)
    return firebase_store.search_marketplace(**params)


def ids(result):
    return [item["id"] for item in result["items"]]


def test_user_listings_are_returned_as_used_even_if_saved_as_part(db):
    result = search(item_type="part")

    pump = next(item for item in result["items"] if item["id"] == "old-pump")
    assert pump["type"] == "used"  # the cart looks for "used" items in listings
    assert pump["listing_kind"] == "part"  # still found by the "parts" filter


def test_out_of_stock_and_sold_items_are_hidden(db):
    found = ids(search())

    assert "pump-1" not in found  # stock 0
    assert "sold-tv" not in found and "empty" not in found
    assert {"belt-1", "legacy", "oven-1", "old-pump"} <= set(found)


def test_no_invented_ratings(db):
    for item in search()["items"]:
        assert item["rating"] is None and item["review_count"] is None


def test_newest_first_after_sponsored_and_dates_are_epoch_ms(db):
    result = search()

    assert ids(result)[0] == "oven-1"  # sponsored
    assert ids(result)[1:] == ["old-pump", "legacy", "belt-1"]  # 1 Nov 2026 > 3 > 1
    assert all(isinstance(item["created_at"], int) for item in result["items"])


def test_cursor_pages_through_without_repeats(db):
    first = search(limit=2)
    second = search(limit=2, cursor=first["next_cursor"])

    assert first["next_cursor"] == "used:old-pump"
    assert set(ids(first)).isdisjoint(ids(second))
    assert second["next_cursor"] is None


def test_new_listing_is_used_with_epoch_ms_date(db):
    created = firebase_store.create_marketplace_listing(
        "owner-1",
        {"title": "Washer motor", "listing_kind": "part", "price": 45.5, "quantity": 2},
        seller_name="Davide",
    )

    stored = db.collection("listings").docs[created["id"]]
    assert stored["item_type"] == "used" and stored["type"] == "used"
    assert stored["price_cents"] == 4550
    assert isinstance(stored["created_at"], int)


def test_seller_name_is_never_the_email(monkeypatch):
    captured = {}

    def create(uid, payload, seller_name):
        captured["seller_name"] = seller_name
        return {"id": "l1", **payload, "type": "used", "seller_name": seller_name, "status": "active"}

    monkeypatch.setattr(firebase_store, "create_marketplace_listing", create)
    token = make_token(sub="owner-1", email="private@example.com")
    TestClient(app).post(
        "/api/marketplace/listings",
        headers=bearer(token),
        json={"title": "Used washer motor", "description": "Tested and working motor for a washer.",
              "category": "replacement part", "listing_kind": "part", "condition": "used-good",
              "price": 45, "image_urls": ["https://example.test/m.jpg"], "handover_methods": ["pickup"]},
    )

    assert captured["seller_name"] == "Community seller"


def test_a_searched_user_part_can_be_added_to_the_cart(db, monkeypatch):
    """End to end: the type returned by the search is what the cart accepts."""
    item = next(i for i in search(item_type="part")["items"] if i["id"] == "old-pump")
    store = FakeStore()
    store.items[(item["type"], item["id"])] = {"title": item["title"], "price_cents": 1500,
                                              "status": "active", "owner_uid": "seller-1"}
    monkeypatch.setattr(checkout_service, "get_store", lambda: store)

    result = TestClient(app, headers=bearer(make_token(sub="buyer-1"))).post(
        "/api/marketplace/cart/items", json={"product_type": item["type"], "product_id": item["id"]}
    )

    assert result.status_code == 200
    assert result.json()["total_cents"] == 1500


def test_search_results_never_contain_the_owner_uid(db):
    items = firebase_store.search_marketplace("", "used", None, None, None, None, None, 20)["items"]

    assert items and all("owner_uid" not in item for item in items)
