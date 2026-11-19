"""The bell of the app: messages, offers, price changes in the cart and a sponsored product."""

from __future__ import annotations

import cart_service
import checkout_service
import conversations_service
import notifications_service
import partner_products
import pytest
from fastapi.testclient import TestClient
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeListingsStore
from test_conversations import API, CONV, Clock, buyer, fridge, seller, start

NOTIFICATIONS = "/api/notifications"


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(checkout_service, "now_ms", fake)
    return fake


@pytest.fixture
def market(monkeypatch, clock):
    """A listing of Marc that Anna can write about."""
    monkeypatch.setattr(conversations_service, "get_listings", lambda: FakeListingsStore({"fridge": fridge()}))


def _no_cart(monkeypatch):
    monkeypatch.setattr(cart_service, "get_cart", lambda uid: {"items": []})


def _no_sponsored(monkeypatch):
    monkeypatch.setattr(partner_products, "list_products", lambda user, **kw: {"items": []})


def test_a_signed_in_user_is_needed():
    assert TestClient(app).get(NOTIFICATIONS).status_code == 401


def test_the_seller_is_told_about_a_new_message(market, monkeypatch):
    _no_cart(monkeypatch)
    _no_sponsored(monkeypatch)
    start("Hi, is it still available?")

    body = seller.get(NOTIFICATIONS).json()

    assert body["unread"] == 1
    item = body["items"][0]
    assert item["kind"] == "message"
    assert item["title"] == "Anna · Old fridge"
    assert item["body"] == "Hi, is it still available?"
    assert item["target"] == {"type": "conversation", "id": CONV}
    assert item["image_url"] == "https://example.test/f.jpg"
    # The one who wrote it gets nothing.
    assert buyer.get(NOTIFICATIONS).json() == {"items": [], "unread": 0}


def test_the_seller_is_told_about_an_offer_on_a_product_for_sale(market, monkeypatch):
    _no_cart(monkeypatch)
    _no_sponsored(monkeypatch)
    start("Hello")
    buyer.post(f"{API}/{CONV}/offers", json={"amount_cents": 8000})

    item = seller.get(NOTIFICATIONS).json()["items"][0]

    assert item["kind"] == "offer"
    assert item["title"] == "New offer: €80.00"
    assert item["body"] == "Anna wants to buy Old fridge"


def test_the_buyer_is_told_when_the_offer_is_answered(market, monkeypatch, clock):
    _no_cart(monkeypatch)
    _no_sponsored(monkeypatch)
    start("Hello")
    buyer.post(f"{API}/{CONV}/offers", json={"amount_cents": 8000})
    clock.now += 1
    seller.post(f"{API}/{CONV}/offer/accept")

    item = buyer.get(NOTIFICATIONS).json()["items"][0]

    assert item["kind"] == "offer_accepted"
    assert item["body"] == "Marc accepted your offer for Old fridge"


def test_a_read_conversation_is_no_longer_notified(market, monkeypatch, clock):
    _no_cart(monkeypatch)
    _no_sponsored(monkeypatch)
    start("Hello")
    clock.now += 1
    seller.get(f"{API}/{CONV}")

    assert seller.get(NOTIFICATIONS).json()["unread"] == 0


def test_a_price_change_in_the_cart_is_notified(market, monkeypatch):
    _no_sponsored(monkeypatch)
    lines = [
        {"cart_item_id": "c1", "title": "Drain pump", "available": True, "price_changed": True,
         "unit_price_cents": 2500, "added_price_cents": 3000, "image_url": "https://x.test/p.jpg"},
        {"cart_item_id": "c2", "title": "Filter", "available": True, "price_changed": True,
         "unit_price_cents": 1200, "added_price_cents": 1000},
        {"cart_item_id": "c3", "title": "Same price", "available": True, "price_changed": False,
         "unit_price_cents": 500, "added_price_cents": 500},
        {"cart_item_id": "c4", "title": "Gone", "available": False, "price_changed": True},
    ]
    monkeypatch.setattr(cart_service, "get_cart", lambda uid: {"items": lines})

    items = buyer.get(NOTIFICATIONS).json()["items"]

    assert [(i["kind"], i["id"]) for i in items] == [("price_drop", "price:c1:2500"), ("price_change", "price:c2:1200")]
    assert items[0]["body"] == "Drain pump now costs €25.00 (was €30.00)"
    assert items[0]["target"] == {"type": "cart"}


def test_one_sponsored_product_comes_last_and_is_not_unread(market, monkeypatch, clock):
    _no_cart(monkeypatch)
    products = [
        {"id": "p1", "title": "Bosch filter", "partner_name": "Bosch Store", "price_cents": 1999, "sponsored": True},
        {"id": "p2", "title": "Plain", "sponsored": False},
    ]
    monkeypatch.setattr(partner_products, "list_products", lambda user, **kw: {"items": products})
    start("Hello")

    body = seller.get(NOTIFICATIONS).json()

    assert [i["kind"] for i in body["items"]] == ["message", "sponsored"]
    sponsored = body["items"][-1]
    assert sponsored["body"] == "Bosch filter from Bosch Store, €19.99"
    assert sponsored["unread"] is False
    assert sponsored["target"]["type"] == "product" and sponsored["target"]["product"]["id"] == "p1"
    assert body["unread"] == 1


def test_a_failing_source_does_not_hide_the_others(market, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("catalogue down")

    monkeypatch.setattr(cart_service, "get_cart", broken)
    monkeypatch.setattr(partner_products, "list_products", broken)
    start("Hello")

    assert [i["kind"] for i in seller.get(NOTIFICATIONS).json()["items"]] == ["message"]


def test_long_messages_are_shortened():
    assert len(notifications_service._short("word " * 100)) == notifications_service.MAX_BODY
    assert notifications_service._short("  two   spaces ") == "two spaces"


def test_a_stranger_sees_nothing(market, monkeypatch):
    _no_cart(monkeypatch)
    _no_sponsored(monkeypatch)
    start("Hello")
    stranger = TestClient(app, headers=bearer(make_token(sub="someone-3")))

    assert stranger.get(NOTIFICATIONS).json()["items"] == []


def _sales_store(monkeypatch, *orders):
    from marketplace_fakes import FakeStore

    fake = FakeStore()
    for order in orders:
        fake.save_order(order["order_id"], order)
    monkeypatch.setattr(checkout_service, "get_store", lambda: fake)


def _order(order_id, *, status="paid", paid_at, seller_id="seller-uid", titles=("Old fridge",), method="pickup"):
    return {
        "order_id": order_id, "checkout_id": "co-1", "uid": "buyer-uid", "seller_id": seller_id,
        "items": [{"title": title} for title in titles], "total_cents": 12000, "shipping_method": method,
        "status": status, "created_at": paid_at, "paid_at": paid_at,
    }


def test_the_seller_is_told_when_a_listing_is_sold(monkeypatch, clock):
    _no_cart(monkeypatch)
    _no_sponsored(monkeypatch)
    now = checkout_service.now_ms()
    _sales_store(monkeypatch, _order("o1", paid_at=now - 1000))
    monkeypatch.setattr(conversations_service, "list_conversations", lambda uid: {"results": []})

    items = notifications_service.list_notifications({"uid": "seller-uid"})["items"]

    assert items == [{
        "id": "sale:o1", "kind": "sale", "title": "Sold: Old fridge",
        "body": "Paid €120.00, the buyer picks it up", "image_url": None,
        "created_at": now - 1000, "unread": True, "target": {"type": "my_listings"},
    }]


def test_only_paid_sales_of_the_last_week_of_this_seller_are_notified(monkeypatch, clock):
    now = checkout_service.now_ms()
    week = checkout_service.SALE_NOTIFICATION_MS
    _sales_store(
        monkeypatch,
        _order("recent", paid_at=now - week + 1, titles=("Pump", "Belt", "Seal"), method="standard"),
        _order("old", paid_at=now - week - 1),
        _order("pending", status="pending", paid_at=now),
        _order("other", seller_id="someone-else", paid_at=now),
    )

    items = notifications_service._sales("seller-uid", now)

    assert [item["id"] for item in items] == ["sale:recent"]
    assert items[0]["title"] == "Sold: Pump and 2 more"
    assert items[0]["body"] == "Paid €120.00, to ship"
