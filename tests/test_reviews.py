"""Product reviews: only buyers write them, everyone signed in reads them."""

from __future__ import annotations

import copy

import checkout_service
import firebase_store
import pytest
import reviews_service
from fastapi.testclient import TestClient
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeStore

PUMP = "/api/marketplace/products/part/HDW-P-001/reviews"


class FakeReviewStore:
    def __init__(self):
        self.reviews: dict[str, dict] = {}
        self.ratings: dict[tuple[str, str], tuple[float, int]] = {}

    def get_review(self, review_id):
        review = self.reviews.get(review_id)
        return {**copy.deepcopy(review), "id": review_id} if review else None

    def save_review(self, review_id, data):
        self.reviews[review_id] = copy.deepcopy(data)

    def delete_review(self, review_id):
        self.reviews.pop(review_id, None)

    def list_product_reviews(self, product_key):
        return [{**copy.deepcopy(r), "id": i} for i, r in self.reviews.items() if r["product_key"] == product_key]

    def set_product_rating(self, product_type, product_id, rating, count):
        self.ratings[(product_type, product_id)] = (rating, count)


class Clock:
    def __init__(self):
        self.now = 1_000

    def __call__(self):
        self.now += 1
        return self.now


@pytest.fixture
def shop(monkeypatch):
    store = FakeStore()
    store.items[("part", "HDW-P-001")] = {"title": "Drain pump", "price": 24.9}
    store.items[("used", "L1")] = {"title": "Old fridge", "owner_uid": "seller-1", "price": 100}
    reviews = FakeReviewStore()
    monkeypatch.setattr(checkout_service, "get_store", lambda: store)
    monkeypatch.setattr(checkout_service, "now_ms", Clock())
    monkeypatch.setattr(reviews_service, "get_review_store", lambda: reviews)
    return store, reviews


def client(uid, name=None):
    return TestClient(app, headers=bearer(make_token(sub=uid, name=name)))


def buy(store, uid, product_type="part", product_id="HDW-P-001", status="paid"):
    order_id = f"o-{uid}-{product_id}-{status}"
    store.orders[order_id] = {
        "uid": uid, "checkout_id": "c", "status": status, "created_at": 1,
        "items": [{"product_type": product_type, "product_id": product_id}],
    }


def test_a_product_without_reviews(shop):
    body = client("anna").get(PUMP).json()

    assert body == {
        "summary": {"average": 0.0, "count": 0, "stars": {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}},
        "items": [], "my_review": None, "can_review": False,
    }


def test_a_buyer_can_review_and_everyone_sees_it(shop):
    store, reviews = shop
    buy(store, "anna")
    anna = client("anna", "Anna M")

    assert anna.get(PUMP).json()["can_review"] is True
    saved = anna.put(f"{PUMP}/mine", json={"rating": 4, "text": "  Fitted in ten minutes.  "})

    assert saved.status_code == 200
    assert saved.json()["text"] == "Fitted in ten minutes."
    body = client("marc").get(PUMP).json()
    assert body["summary"]["average"] == 4.0
    assert body["summary"]["stars"]["4"] == 1
    item = body["items"][0]
    assert item["author_name"] == "Anna M"
    assert item["verified_purchase"] is True
    assert item["mine"] is False
    assert "uid" not in item
    assert body["can_review"] is False
    assert reviews.ratings[("part", "HDW-P-001")] == (4.0, 1)


def test_only_buyers_can_review(shop):
    store, _ = shop
    buy(store, "anna", status="pending")

    response = client("anna").put(f"{PUMP}/mine", json={"rating": 5})

    assert response.status_code == 403
    assert response.json()["detail"] == "only buyers of this product can review it"


def test_writing_again_changes_the_review(shop):
    store, reviews = shop
    buy(store, "anna")
    buy(store, "marc")
    client("anna").put(f"{PUMP}/mine", json={"rating": 2, "text": "Noisy"})
    client("marc").put(f"{PUMP}/mine", json={"rating": 5})
    client("anna").put(f"{PUMP}/mine", json={"rating": 4, "text": "Fine after all"})

    body = client("anna").get(PUMP).json()

    assert body["summary"]["count"] == 2
    assert body["summary"]["average"] == 4.5
    # Newest first: Anna changed hers last.
    assert [i["rating"] for i in body["items"]] == [4, 5]
    assert body["my_review"]["text"] == "Fine after all"
    assert body["my_review"]["mine"] is True
    assert len(reviews.reviews) == 2


def test_a_review_can_be_deleted(shop):
    store, reviews = shop
    buy(store, "anna")
    anna = client("anna")
    anna.put(f"{PUMP}/mine", json={"rating": 3})

    assert anna.delete(f"{PUMP}/mine").status_code == 204
    assert anna.delete(f"{PUMP}/mine").status_code == 404
    assert reviews.ratings[("part", "HDW-P-001")] == (0.0, 0)


def test_the_seller_cannot_review_their_own_listing(shop):
    store, _ = shop
    buy(store, "seller-1", "used", "L1")

    response = client("seller-1").put("/api/marketplace/products/used/L1/reviews/mine", json={"rating": 5})

    assert response.status_code == 403


def test_a_bought_second_hand_item_can_be_reviewed(shop):
    store, _ = shop
    buy(store, "anna", "used", "L1")

    response = client("anna").put("/api/marketplace/products/used/L1/reviews/mine", json={"rating": 5})

    assert response.status_code == 200


@pytest.mark.parametrize("body", [{"rating": 0}, {"rating": 6}, {"rating": 3, "text": "x" * 1001}, {}])
def test_invalid_reviews_are_refused(shop, body):
    store, _ = shop
    buy(store, "anna")

    assert client("anna").put(f"{PUMP}/mine", json=body).status_code == 422


def test_an_unknown_product_has_no_reviews(shop):
    assert client("anna").get("/api/marketplace/products/part/NOPE/reviews").status_code == 404
    assert client("anna").get("/api/marketplace/products/other/X/reviews").status_code == 422


def test_reviews_need_a_signed_in_user(shop):
    assert TestClient(app).get(PUMP).status_code == 401


def test_reviews_unavailable_without_firestore(shop, monkeypatch):
    def broken(*args):
        raise firebase_store.FirebaseConfigurationError("no credentials")

    monkeypatch.setattr(reviews_service, "list_reviews", broken)

    assert client("anna").get(PUMP).status_code == 503


def test_service_errors_are_checked_directly(shop):
    with pytest.raises(reviews_service.ReviewError):
        reviews_service.save_review({}, "anna", "part", "HDW-P-001", 9, None)
    with pytest.raises(reviews_service.ReviewError):
        reviews_service.list_reviews("anna", "gadget", "X")
