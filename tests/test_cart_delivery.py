"""Cart split by seller, delivery options, item selection and payment method."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

import checkout_service
import fulfilment
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeStore
from test_checkout import FakeGateway, intent_event, signed

BUYER = "buyer-1"
CART = "/api/marketplace/cart"
QUOTE = "/api/marketplace/checkout/quote"
CHECKOUT = "/api/marketplace/checkout"
# Friday 6 November 2026, 10:00 UTC.
NOW = int(dt.datetime(2026, 11, 6, 10, tzinfo=dt.timezone.utc).timestamp() * 1000)

client = TestClient(app, headers=bearer(make_token(sub=BUYER)))


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(checkout_service, "get_store", lambda: fake)
    monkeypatch.setattr(checkout_service, "now_ms", lambda: NOW)
    fake.items = {
        ("part", "pump-1"): {"title": "Drain pump", "price_cents": 2500, "stock": 5, "partner_id": "voltacasa",
                             "partner_name": "VoltaCasa", "brand": "Bosch", "part_number": "00145787"},
        ("new", "oven-9"): {"title": "Oven", "price_cents": 39900, "stock": 1, "partner_id": "nordhome",
                            "partner_name": "NordHome", "brand": "Aurora", "model": "OV-9"},
        ("used", "fridge-2"): {"title": "Used fridge", "price_cents": 12000, "status": "active",
                               "owner_uid": "seller-9", "seller_name": "Marc", "condition": "like-new",
                               "handover_methods": ["pickup", "shipping"], "shipping_price_cents": 1500,
                               "pickup_zip": "08012"},
    }
    fake.set_cart(BUYER, [
        {"cart_item_id": "c1", "product_type": "part", "product_id": "pump-1", "quantity": 1, "added_at": 1},
        {"cart_item_id": "c2", "product_type": "new", "product_id": "oven-9", "quantity": 1, "added_at": 2},
        {"cart_item_id": "c3", "product_type": "used", "product_id": "fridge-2", "quantity": 1, "added_at": 3},
    ])
    return fake


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(checkout_service, "get_gateway", lambda: fake)
    return fake


@pytest.fixture
def stripe_on(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    monkeypatch.delenv("STRIPE_CURRENCY", raising=False)


def group(body, seller_id):
    return next(g for g in body["groups"] if g["seller_id"] == seller_id)


# ---------- cart and quote ----------

def test_the_cart_is_split_by_seller_partners_first(store):
    body = client.get(CART).json()

    assert [g["seller_id"] for g in body["groups"]] == ["nordhome", "voltacasa", "seller-9"]
    assert [g["seller_kind"] for g in body["groups"]] == ["partner", "partner", "community"]
    pump = body["items"][0]
    assert pump["seller_name"] == "VoltaCasa" and pump["subtitle"] == "Bosch · #00145787"
    assert body["items"][2]["subtitle"] == "Like new" and body["items"][2]["selected"] is True
    assert body["payment_methods"] == ["card", "google_pay", "klarna"]


def test_each_group_offers_its_delivery_options_and_defaults_to_the_cheapest(store):
    body = client.get(CART).json()

    pump = group(body, "voltacasa")
    assert [(o["method"], o["price_cents"]) for o in pump["options"]] == [("standard", 490), ("express", 990)]
    assert pump["method"] == "standard" and pump["shipping_cents"] == 490
    # Friday + 3 to 5 business days: Wednesday to Friday of the next week.
    assert pump["delivery"] == {"from": "2026-11-11", "to": "2026-11-13"}

    oven = group(body, "nordhome")
    assert oven["shipping_cents"] == 0  # free standard shipping from 50 €

    fridge = group(body, "seller-9")
    assert [(o["method"], o["price_cents"]) for o in fridge["options"]] == [("pickup", 0), ("standard", 1500)]
    assert fridge["method"] == "pickup" and fridge["delivery"] is None
    assert fridge["pickup_area"] == "080xx"  # the exact postcode only after paying

    summary = body["summary"]
    assert summary["subtotal_cents"] == 2500 + 39900 + 12000
    assert summary["shipping_cents"] == 490
    assert summary["total_cents"] == 2500 + 39900 + 12000 + 490
    assert summary["vat_included_cents"] == round(summary["total_cents"] * 21 / 121)
    assert summary["can_checkout"] is True


def test_the_quote_follows_the_selection_and_the_delivery_choices(store):
    body = client.post(QUOTE, json={"cart_item_ids": ["c1", "c3"],
                                    "delivery": {"voltacasa": "express", "seller-9": "standard"}}).json()

    assert [line["selected"] for line in body["items"]] == [True, False, True]
    assert group(body, "nordhome")["selected_count"] == 0 and group(body, "nordhome")["shipping_cents"] == 0
    assert group(body, "voltacasa")["shipping_cents"] == 990
    assert group(body, "voltacasa")["delivery"] == {"from": "2026-11-09", "to": "2026-11-10"}
    assert group(body, "seller-9")["shipping_cents"] == 1500 and group(body, "seller-9")["pickup_area"] is None
    assert body["summary"]["selected_item_count"] == 2
    assert body["summary"]["total_cents"] == 2500 + 990 + 12000 + 1500


def test_nothing_selected_cannot_be_checked_out(store):
    summary = client.post(QUOTE, json={"cart_item_ids": []}).json()["summary"]

    assert summary == {**summary, "selected_item_count": 0, "total_cents": 0, "can_checkout": False}


@pytest.mark.parametrize(
    "request_body, detail",
    [
        ({"cart_item_ids": ["nope"]}, "some selected items are not in the cart"),
        ({"delivery": {"seller-9": "express"}}, "Express shipping is not available for Marc"),
        ({"delivery": {"someone-else": "standard"}}, "a delivery choice refers to a seller that is not in the cart"),
    ],
)
def test_invalid_choices_are_refused(store, request_body, detail):
    result = client.post(QUOTE, json=request_body)

    assert result.status_code == 422 and result.json()["detail"] == detail


def test_unknown_methods_and_fields_are_refused_by_the_schema(store):
    assert client.post(QUOTE, json={"delivery": {"voltacasa": "drone"}}).status_code == 422
    assert client.post(QUOTE, json={"total_cents": 1}).status_code == 422


def test_listing_handover_decides_the_options(store):
    fridge = store.items[("used", "fridge-2")]

    fridge["handover_methods"] = ["shipping"]
    fridge.pop("shipping_price_cents")
    options = group(client.get(CART).json(), "seller-9")["options"]
    assert [(o["method"], o["price_cents"]) for o in options] == [("standard", 490)]

    fridge.pop("handover_methods")  # older listings: pickup only
    assert [o["method"] for o in group(client.get(CART).json(), "seller-9")["options"]] == ["pickup"]


# ---------- checkout ----------

def test_checkout_buys_only_the_selection_with_its_delivery(store, gateway, stripe_on):
    result = client.post(CHECKOUT, json={
        "cart_item_ids": ["c1", "c3"],
        "delivery": {"voltacasa": "express", "seller-9": "pickup"},
        "payment_method": "klarna",
    })

    assert result.status_code == 200
    body = result.json()
    assert body["subtotal_cents"] == 2500 + 12000 and body["shipping_cents"] == 990
    assert body["total_cents"] == 2500 + 12000 + 990 and body["payment_method"] == "klarna"
    assert gateway.calls[0]["amount_cents"] == body["total_cents"]
    assert gateway.calls[0]["allowed_payment_method_types"] == ["klarna"]

    orders = {store.orders[oid]["seller_id"]: store.orders[oid] for oid in body["order_ids"]}
    assert set(orders) == {"voltacasa", "seller-9"}  # the oven was not selected
    pump = orders["voltacasa"]
    assert (pump["shipping_method"], pump["shipping_cents"], pump["total_cents"]) == ("express", 990, 3490)
    assert pump["delivery"] == {"from": "2026-11-09", "to": "2026-11-10"} and pump["pickup_zip"] is None
    fridge = orders["seller-9"]
    assert fridge["shipping_method"] == "pickup" and fridge["pickup_zip"] == "08012"
    assert fridge["seller_name"] == "Marc" and fridge["payment_method"] == "klarna"


def test_after_paying_only_the_bought_lines_leave_the_cart(store, gateway, stripe_on):
    checkout = client.post(CHECKOUT, json={"cart_item_ids": ["c1"]}).json()

    payload, headers = signed(intent_event("payment_intent.succeeded", checkout, amount=checkout["total_cents"],
                                           amount_received=checkout["total_cents"]))
    assert client.post("/api/marketplace/stripe/webhook", content=payload, headers=headers).status_code == 200

    assert sorted(line["cart_item_id"] for line in store.cart(BUYER)) == ["c2", "c3"]
    order = client.get(f"/api/marketplace/orders/{checkout['order_ids'][0]}").json()
    assert order["status"] == "paid" and order["shipping_cents"] == 490 and order["total_cents"] == 2990


@pytest.mark.parametrize(
    "method, types",
    [("card", ["card"]), ("google_pay", ["card"]), ("klarna", ["klarna"]), (None, None)],
)
def test_the_payment_method_restricts_the_payment_sheet(store, gateway, stripe_on, method, types):
    client.post(CHECKOUT, json={"payment_method": method} if method else {})

    assert gateway.calls[0]["allowed_payment_method_types"] == types


def test_an_empty_selection_or_an_unknown_method_is_refused(store, gateway, stripe_on):
    assert client.post(CHECKOUT, json={"cart_item_ids": []}).json()["detail"] == "select at least one item to buy"
    assert client.post(CHECKOUT, json={"payment_method": "paypal"}).status_code == 422
    assert gateway.calls == []


def test_an_unavailable_line_blocks_only_when_selected(store, gateway, stripe_on):
    store.items[("new", "oven-9")]["stock"] = 0

    assert client.post(CHECKOUT, json={}).status_code == 409
    assert client.post(CHECKOUT, json={"cart_item_ids": ["c1", "c3"]}).status_code == 200


def test_business_days_skip_the_weekend():
    friday = dt.date(2026, 11, 6)

    assert fulfilment.delivery_window("express", friday) == {"from": "2026-11-09", "to": "2026-11-10"}
    assert fulfilment.delivery_window("pickup", friday) is None
    assert fulfilment.pickup_area({"pickup_zip": "08"}) is None
    assert fulfilment.seller_name("part", {"seller_id": "hodowo"}) == "HoDoWo"
    assert fulfilment.item_subtitle("used", {}) == "Second-hand"


def test_the_gateway_sends_the_chosen_payment_method_to_stripe(monkeypatch):
    import stripe

    import stripe_gateway

    sent = []

    class Intents:
        def create(self, params, options):
            sent.append((params, options))
            return type("Intent", (), {"id": "pi_9", "client_secret": "pi_9_secret"})()

    class Client:
        def __init__(self, key):
            self.v1 = type("V1", (), {"payment_intents": Intents()})()

    monkeypatch.setattr(stripe, "StripeClient", Client)
    common = {"secret_key": "sk_test_1", "amount_cents": 1000, "currency": "eur", "metadata": {}, "idempotency_key": "k"}

    stripe_gateway.create_payment_intent(**common, allowed_payment_method_types=["klarna"])
    stripe_gateway.create_payment_intent(**common)

    assert sent[0][0]["allowed_payment_method_types"] == ["klarna"] and "automatic_payment_methods" not in sent[0][0]
    # Refused by Stripe since API version 2026-09-30.endive: never sent.
    assert all("payment_method_types" not in params for params, _ in sent)
    assert sent[1][0]["automatic_payment_methods"] == {"enabled": True}
    assert sent[0][1] == {"idempotency_key": "k"}
