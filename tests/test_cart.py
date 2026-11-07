"""Marketplace cart API (#42)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import checkout_service
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeStore

BUYER = "buyer-1"
CART = "/api/marketplace/cart"
ITEMS = "/api/marketplace/cart/items"

client = TestClient(app, headers=bearer(make_token(sub=BUYER)))


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(checkout_service, "get_store", lambda: fake)
    monkeypatch.delenv("STRIPE_CURRENCY", raising=False)
    fake.items = {
        ("part", "pump-1"): {"title": "Drain pump", "price_cents": 2500, "stock": 3, "partner_id": "partner-a",
                             "image_url": "https://example.test/pump.jpg"},
        ("new", "oven-9"): {"title": "Oven", "price_cents": 39900, "stock": 0, "partner_id": "partner-b"},
        ("used", "fridge-2"): {"title": "Used fridge", "price_cents": 12000, "status": "active",
                               "owner_uid": "seller-9", "image_urls": ["https://example.test/f1.jpg"]},
        ("used", "my-sofa"): {"title": "My own listing", "price_cents": 5000, "status": "active", "owner_uid": BUYER},
        ("used", "sold-tv"): {"title": "Sold TV", "price_cents": 8000, "status": "sold", "owner_uid": "seller-9"},
    }
    return fake


def add(product_type, product_id, quantity=1):
    return client.post(ITEMS, json={"product_type": product_type, "product_id": product_id, "quantity": quantity})


def test_new_cart_is_empty(store):
    body = client.get(CART).json()

    assert {key: body[key] for key in ("items", "item_count", "total_cents", "currency", "has_unavailable_items")} == {
        "items": [], "item_count": 0, "total_cents": 0, "currency": "eur", "has_unavailable_items": False,
    }
    assert body["groups"] == [] and body["summary"]["can_checkout"] is False


def test_add_returns_the_cart_with_server_side_totals(store):
    add("part", "pump-1", 2)
    body = add("used", "fridge-2").json()

    assert body["total_cents"] == 2 * 2500 + 12000
    assert body["item_count"] == 3
    pump, fridge = body["items"]
    assert pump["unit_price_cents"] == 2500 and pump["price_cents"] == 5000
    assert pump["image_url"] == "https://example.test/pump.jpg"
    assert fridge["image_url"] == "https://example.test/f1.jpg"  # first listing photo


def test_adding_the_same_product_again_increases_the_quantity(store):
    add("part", "pump-1", 1)
    body = add("part", "pump-1", 2).json()

    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 3


@pytest.mark.parametrize(
    "product, quantity, status, detail",
    [
        (("used", "sold-tv"), 1, 409, "already sold"),
        (("new", "oven-9"), 1, 409, "not enough stock"),
        (("part", "pump-1"), 4, 409, "not enough stock"),
        (("used", "my-sofa"), 1, 409, "you cannot buy your own listing"),
        (("used", "fridge-2"), 2, 409, "not enough stock"),  # listing quantity defaults to 1
        (("part", "missing"), 1, 404, "product not found"),
    ],
)
def test_unbuyable_products_are_refused(store, product, quantity, status, detail):
    result = add(*product, quantity)

    assert result.status_code == status
    assert result.json()["detail"] == detail
    assert store.cart(BUYER) == []


def test_a_single_listing_cannot_be_added_twice(store):
    add("used", "fridge-2")

    assert add("used", "fridge-2").status_code == 409


def test_a_listing_with_several_items_can_be_bought_up_to_its_quantity(store):
    store.items[("used", "fridge-2")]["quantity"] = 2

    assert add("used", "fridge-2", 2).json()["items"][0]["quantity"] == 2
    assert add("used", "fridge-2").status_code == 409


def test_invalid_input_is_rejected_by_validation(store):
    assert client.post(ITEMS, json={"product_type": "car", "product_id": "x"}).status_code == 422
    assert client.post(ITEMS, json={"product_type": "part", "product_id": "a/b"}).status_code == 422
    assert add("part", "pump-1", 0).status_code == 422
    assert add("part", "pump-1", 100).status_code == 422


def test_change_quantity(store):
    line_id = add("part", "pump-1").json()["items"][0]["cart_item_id"]

    body = client.patch(f"{ITEMS}/{line_id}", json={"quantity": 3}).json()
    assert body["items"][0]["quantity"] == 3 and body["total_cents"] == 7500

    assert client.patch(f"{ITEMS}/{line_id}", json={"quantity": 4}).status_code == 409


def test_removing_the_last_item_empties_the_cart(store):
    line_id = add("part", "pump-1").json()["items"][0]["cart_item_id"]

    body = client.delete(f"{ITEMS}/{line_id}").json()

    assert body["items"] == [] and body["item_count"] == 0 and body["total_cents"] == 0


def test_another_users_line_cannot_be_changed_or_removed(store):
    store.set_cart("someone-else", [{"cart_item_id": "theirs", "product_type": "part", "product_id": "pump-1", "quantity": 1}])

    assert client.patch(f"{ITEMS}/theirs", json={"quantity": 2}).status_code == 404
    assert client.delete(f"{ITEMS}/theirs").status_code == 404
    assert store.cart_items["theirs"]["quantity"] == 1


def test_items_that_became_unavailable_are_flagged_and_left_out_of_the_total(store):
    add("part", "pump-1")
    add("used", "fridge-2")
    store.items[("used", "fridge-2")]["status"] = "sold"

    body = client.get(CART).json()

    fridge = next(line for line in body["items"] if line["product_id"] == "fridge-2")
    assert fridge["available"] is False and fridge["unavailable_reason"] == "already sold"
    assert body["has_unavailable_items"] is True
    assert body["total_cents"] == 2500 and body["item_count"] == 1


def test_price_change_since_adding_is_reported(store):
    add("part", "pump-1")
    store.items[("part", "pump-1")]["price_cents"] = 2700

    line = client.get(CART).json()["items"][0]

    assert line["added_price_cents"] == 2500
    assert line["unit_price_cents"] == 2700
    assert line["price_changed"] is True


def test_the_cart_feeds_the_checkout(store, monkeypatch):
    for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    add("part", "pump-1", 2)
    cart_total = add("used", "fridge-2").json()["total_cents"]

    checkout = client.post("/api/marketplace/checkout").json()

    assert checkout["total_cents"] == cart_total
    assert client.get(CART).json()["items"] == []  # simulated purchase empties the cart


def test_cart_requires_a_token(store):
    assert TestClient(app).get(CART).status_code == 401


def test_a_listing_with_nothing_left_cannot_be_added(store):
    store.items[("used", "fridge-2")]["quantity"] = 0

    assert add("used", "fridge-2").json()["detail"] == "not enough stock"
