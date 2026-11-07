"""Marketplace checkout, Stripe webhook and order history (#43).

Stripe is never called: PaymentIntent creation is faked, while webhooks are
signed here with the test secret and verified by the real Stripe SDK.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import checkout_service
import stripe_gateway
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeStore

WEBHOOK = "/api/marketplace/stripe/webhook"
WEBHOOK_SECRET = "whsec_test_secret"
BUYER = "buyer-1"

client = TestClient(app, headers=bearer(make_token(sub=BUYER)))


class FakeGateway:
    """Fakes PaymentIntent creation; webhook parsing uses the real Stripe SDK."""

    InvalidWebhookError = stripe_gateway.InvalidWebhookError
    parse_webhook = staticmethod(stripe_gateway.parse_webhook)

    def __init__(self):
        self.calls: list[dict] = []
        self.fail = False

    def create_payment_intent(self, **kwargs):
        if self.fail:
            raise stripe_gateway.PaymentGatewayError("Stripe is down")
        self.calls.append(kwargs)
        return {"id": f"pi_{len(self.calls)}", "client_secret": f"pi_{len(self.calls)}_secret_abc"}


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(checkout_service, "get_store", lambda: fake)
    fake.items = {
        ("part", "pump-1"): {"title": "Drain pump", "price_cents": 2500, "stock": 3, "partner_id": "partner-a"},
        ("new", "oven-9"): {"title": "Oven", "price_cents": 39900, "stock": 1, "partner_id": "partner-b"},
        ("used", "fridge-2"): {"title": "Used fridge", "price_cents": 12000, "status": "active", "owner_uid": "seller-9"},
    }
    fake.set_cart(BUYER, [
        {"cart_item_id": "c1", "product_type": "part", "product_id": "pump-1", "quantity": 2},
        {"cart_item_id": "c2", "product_type": "used", "product_id": "fridge-2", "quantity": 1},
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
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.delenv("STRIPE_CURRENCY", raising=False)


@pytest.fixture
def stripe_off(monkeypatch):
    for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_CURRENCY"):
        monkeypatch.delenv(name, raising=False)


def signed(event: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, dict]:
    """Sign a webhook body the way Stripe does (v1 = HMAC-SHA256 of 't.payload')."""
    payload = json.dumps(event).encode()
    timestamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, {"Stripe-Signature": f"t={timestamp},v1={signature}", "Content-Type": "application/json"}


def intent_event(event_type: str, checkout: dict, event_id: str = "evt_1", **overrides) -> dict:
    intent = {
        "id": "pi_1",
        "object": "payment_intent",
        "amount": checkout["total_cents"],
        "amount_received": checkout["total_cents"],
        "currency": checkout["currency"],
        "metadata": {"checkout_id": checkout["checkout_id"], "uid": BUYER},
    }
    intent.update(overrides)
    return {"id": event_id, "object": "event", "type": event_type, "data": {"object": intent}}


def post_webhook(event: dict, secret: str = WEBHOOK_SECRET):
    payload, headers = signed(event, secret)
    return TestClient(app).post(WEBHOOK, content=payload, headers=headers)


# --- checkout ---------------------------------------------------------------


def test_checkout_creates_one_order_per_seller_with_server_side_totals(store, gateway, stripe_on):
    # The app cannot send an amount: unknown fields are refused.
    assert client.post("/api/marketplace/checkout", json={"total_cents": 1}).status_code == 422

    result = client.post("/api/marketplace/checkout", json={})

    assert result.status_code == 200
    body = result.json()
    # Pumps 50 € (free standard shipping from 50 €) + fridge with local pickup.
    assert body["total_cents"] == 2 * 2500 + 12000
    assert body["currency"] == "eur"
    assert body["simulated"] is False
    assert body["client_secret"] == "pi_1_secret_abc"

    orders = [store.orders[order_id] for order_id in body["order_ids"]]
    assert sorted(order["seller_id"] for order in orders) == ["partner-a", "seller-9"]
    assert all(order["status"] == "pending" and order["payment_intent_id"] == "pi_1" for order in orders)
    assert gateway.calls[0]["amount_cents"] == body["total_cents"]
    assert gateway.calls[0]["metadata"]["checkout_id"] == body["checkout_id"]


def test_checkout_uses_the_current_catalogue_price(store, gateway, stripe_on):
    store.set_cart(BUYER, [{"cart_item_id": "c1", "product_type": "part", "product_id": "pump-1", "quantity": 1, "price_cents": 1}])
    store.items[("part", "pump-1")]["price_cents"] = 2700

    body = client.post("/api/marketplace/checkout").json()
    assert body["subtotal_cents"] == 2700
    assert body["shipping_cents"] == 490  # standard shipping under 50 €
    assert body["total_cents"] == 3190


def test_empty_cart_is_refused(store, gateway, stripe_on):
    store.set_cart(BUYER, [])

    assert client.post("/api/marketplace/checkout").status_code == 400
    assert gateway.calls == []


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda s: s.items[("used", "fridge-2")].update(status="sold"), "already sold"),
        (lambda s: s.items[("part", "pump-1")].update(stock=1), "not enough stock"),
        (lambda s: s.items.pop(("part", "pump-1")), "not available"),
        (lambda s: s.items[("used", "fridge-2")].update(owner_uid=BUYER), "you cannot buy your own listing"),
    ],
)
def test_unbuyable_items_return_409_and_nothing_is_created(store, gateway, stripe_on, change, reason):
    change(store)

    result = client.post("/api/marketplace/checkout")

    assert result.status_code == 409
    assert reason in [item["reason"] for item in result.json()["detail"]["items"]]
    assert store.orders == {}
    assert gateway.calls == []


def test_stripe_failure_cancels_the_orders(store, gateway, stripe_on):
    gateway.fail = True

    result = client.post("/api/marketplace/checkout")

    assert result.status_code == 502
    assert {order["status"] for order in store.orders.values()} == {"cancelled"}


def test_without_stripe_keys_the_purchase_is_simulated(store, gateway, stripe_off):
    body = client.post("/api/marketplace/checkout").json()

    assert body["simulated"] is True
    assert body["client_secret"] is None
    assert gateway.calls == []
    assert all(store.orders[i]["status"] == "paid" and store.orders[i]["simulated"] for i in body["order_ids"])
    assert store.items[("used", "fridge-2")]["status"] == "sold"
    assert store.cart(BUYER) == []


def test_live_keys_are_refused(store, gateway, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_123")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)

    assert client.post("/api/marketplace/checkout").status_code == 503
    assert gateway.calls == []


def test_checkout_requires_a_token(store, gateway, stripe_on):
    assert TestClient(app).post("/api/marketplace/checkout").status_code == 401


# --- webhook ----------------------------------------------------------------


def test_succeeded_event_marks_orders_paid_and_applies_them(store, gateway, stripe_on):
    checkout = client.post("/api/marketplace/checkout").json()

    result = post_webhook(intent_event("payment_intent.succeeded", checkout))

    assert result.status_code == 200
    assert result.json()["handled"] is True
    assert {store.orders[i]["status"] for i in checkout["order_ids"]} == {"paid"}
    assert store.items[("used", "fridge-2")]["status"] == "sold"
    assert store.items[("part", "pump-1")]["stock"] == 1
    assert store.cart(BUYER) == []


def test_the_same_event_twice_changes_nothing(store, gateway, stripe_on):
    checkout = client.post("/api/marketplace/checkout").json()
    event = intent_event("payment_intent.succeeded", checkout)

    post_webhook(event)
    second = post_webhook(event)

    assert second.json()["duplicate"] is True
    assert store.items[("part", "pump-1")]["stock"] == 1  # decremented only once


def test_wrong_or_missing_signature_is_rejected(store, gateway, stripe_on):
    checkout = client.post("/api/marketplace/checkout").json()
    event = intent_event("payment_intent.succeeded", checkout)

    assert post_webhook(event, secret="whsec_attacker").status_code == 400
    assert TestClient(app).post(WEBHOOK, json=event).status_code == 400
    assert {store.orders[i]["status"] for i in checkout["order_ids"]} == {"pending"}


def test_amount_mismatch_does_not_mark_orders_paid(store, gateway, stripe_on):
    checkout = client.post("/api/marketplace/checkout").json()

    post_webhook(intent_event("payment_intent.succeeded", checkout, amount_received=100))

    assert {store.orders[i]["status"] for i in checkout["order_ids"]} == {"pending"}
    assert store.items[("used", "fridge-2")]["status"] == "active"


def test_failed_payment_can_still_succeed_on_retry(store, gateway, stripe_on):
    checkout = client.post("/api/marketplace/checkout").json()

    post_webhook(intent_event("payment_intent.payment_failed", checkout, event_id="evt_fail"))
    assert {store.orders[i]["status"] for i in checkout["order_ids"]} == {"failed"}

    post_webhook(intent_event("payment_intent.succeeded", checkout, event_id="evt_ok"))
    assert {store.orders[i]["status"] for i in checkout["order_ids"]} == {"paid"}


def test_unrelated_events_are_acknowledged(store, gateway, stripe_on):
    event = {"id": "evt_x", "object": "event", "type": "customer.created", "data": {"object": {}}}

    result = post_webhook(event)

    assert result.status_code == 200
    assert result.json()["handled"] is False


def test_webhook_without_configuration_returns_503(store, gateway, stripe_off):
    assert TestClient(app).post(WEBHOOK, content=b"{}").status_code == 503


# --- order history ----------------------------------------------------------


def test_history_lists_only_the_users_orders_without_internal_fields(store, gateway, stripe_off):
    client.post("/api/marketplace/checkout")
    store.save_order("other", {"checkout_id": "x", "uid": "someone-else", "created_at": 1, "items": []})

    orders = client.get("/api/marketplace/orders").json()["orders"]

    assert len(orders) == 2
    assert all("cart_item_ids" not in order and "payment_intent_id" not in order for order in orders)


def test_another_users_order_is_forbidden(store, gateway, stripe_off):
    store.save_order("other", {"checkout_id": "x", "uid": "someone-else", "created_at": 1, "items": []})

    assert client.get("/api/marketplace/orders/other").status_code == 403
    assert client.get("/api/marketplace/orders/missing").status_code == 404
