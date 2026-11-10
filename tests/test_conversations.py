"""Buyer-seller conversations and Vinted-style offers on second-hand listings."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import checkout_service
import conversations_service
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeListingsStore, FakeStore
from test_checkout import FakeGateway, intent_event, signed

API = "/api/marketplace/conversations"
SELLER, BUYER, STRANGER = "seller-1", "buyer-2", "someone-3"
CONV = f"fridge__{BUYER}"
HOUR = 3_600_000

seller = TestClient(app, headers=bearer(make_token(sub=SELLER, name="Marc")))
buyer = TestClient(app, headers=bearer(make_token(sub=BUYER, name="Anna")))
stranger = TestClient(app, headers=bearer(make_token(sub=STRANGER)))


class Clock:
    def __init__(self):
        self.now = 1_000_000

    def __call__(self):
        return self.now


def fridge(**fields):
    doc = {
        "title": "Old fridge", "price_cents": 10000, "status": "active", "owner_uid": SELLER,
        "seller_name": "Marc", "image_urls": ["https://example.test/f.jpg"], "quantity": 1,
        "handover_methods": ["pickup"], "pickup_zip": "08012",
    }
    doc.update(fields)
    return doc


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(checkout_service, "now_ms", fake)
    return fake


@pytest.fixture
def listings(monkeypatch, clock):
    fake = FakeListingsStore({"fridge": fridge(), "mine": fridge(owner_uid=BUYER), "sold": fridge(status="sold")})
    monkeypatch.setattr(conversations_service, "get_listings", lambda: fake)
    return fake


def start(text=None):
    body = {"listing_id": "fridge"} if text is None else {"listing_id": "fridge", "text": text}
    return buyer.post(API, json=body)


def offer(client, amount):
    return client.post(f"{API}/{CONV}/offers", json={"amount_cents": amount})


# ---------- conversations ----------

def test_a_buyer_messages_the_seller_and_the_seller_sees_it_in_the_inbox(listings, conversations, clock):
    result = start("Hi, is it still available?")

    assert result.status_code == 201
    body = result.json()
    assert body["id"] == CONV and body["role"] == "buying" and body["other_name"] == "Marc"
    assert body["listing"] == {"id": "fridge", "title": "Old fridge", "image_url": "https://example.test/f.jpg",
                               "price_cents": 10000, "status": "active"}
    assert [(m["text"], m["mine"]) for m in body["messages"]] == [("Hi, is it still available?", True)]
    assert body["can_offer"] is True

    inbox = seller.get(API).json()
    assert inbox["unread"] == 1
    row = inbox["results"][0]
    assert row["role"] == "selling" and row["other_name"] == "Anna" and row["unread"] == 1
    assert row["last_message"]["text"] == "Hi, is it still available?" and row["last_message"]["mine"] is False
    assert "messages" not in row

    clock.now += 1
    opened = seller.get(f"{API}/{CONV}").json()
    assert opened["unread"] == 0 and opened["messages"][0]["mine"] is False
    assert seller.get(API).json()["unread"] == 0


def test_no_uid_ever_reaches_the_app(listings):
    start("Hello")

    text = seller.get(API).text + seller.get(f"{API}/{CONV}").text + buyer.get(f"{API}/{CONV}").text
    assert BUYER not in text.replace(CONV, "") and SELLER not in text


def test_messaging_again_reuses_the_conversation(listings, conversations):
    start("First")
    start("Second")

    assert list(conversations.conversations) == [CONV]
    assert [m["text"] for m in buyer.get(f"{API}/{CONV}").json()["messages"]] == ["First", "Second"]


def test_replies_go_both_ways(listings):
    start("Is it still available?")

    reply = seller.post(f"{API}/{CONV}/messages", json={"text": "  Yes,​ come on Saturday  "})

    assert reply.status_code == 201
    assert [(m["text"], m["from"]) for m in reply.json()["messages"]] == [
        ("Is it still available?", "them"), ("Yes, come on Saturday", "me"),
    ]
    assert buyer.get(API).json()["unread"] == 1


@pytest.mark.parametrize(
    "listing_id, status, detail",
    [("nope", 404, "listing not found"), ("sold", 404, "listing not found"), ("mine", 422, "this is your listing")],
)
def test_only_active_listings_of_others_can_be_discussed(listings, listing_id, status, detail):
    result = buyer.post(API, json={"listing_id": listing_id})

    assert result.status_code == status and result.json()["detail"] == detail


def test_other_people_cannot_read_or_write_the_conversation(listings):
    start("Private")

    assert stranger.get(f"{API}/{CONV}").status_code == 404
    assert stranger.post(f"{API}/{CONV}/messages", json={"text": "hi"}).status_code == 404
    assert stranger.get(API).json() == {"results": [], "unread": 0}


def test_empty_or_invisible_messages_are_refused(listings):
    start()

    assert buyer.post(f"{API}/{CONV}/messages", json={"text": "​ ‮"}).status_code == 422
    assert buyer.post(f"{API}/{CONV}/messages", json={"text": "x" * 1001}).status_code == 422
    assert buyer.post(f"{API}/{CONV}/messages", json={"text": "hi", "uid": SELLER}).status_code == 422


# ---------- offers ----------

def test_the_seller_accepts_an_offer_and_the_buyer_can_pay_it_for_48_hours(listings, clock):
    start()

    made = offer(buyer, 8000)
    assert made.status_code == 201
    pending = made.json()["offer"]
    assert pending["amount_cents"] == 8000 and pending["from"] == "me" and pending["status"] == "pending"
    assert pending["can_answer"] is False and made.json()["can_offer"] is False
    assert made.json()["messages"][-1]["kind"] == "offer" and made.json()["messages"][-1]["text"] == "Offered €80.00"

    seen = seller.get(f"{API}/{CONV}").json()["offer"]
    assert seen["from"] == "them" and seen["can_answer"] is True

    accepted = seller.post(f"{API}/{CONV}/offer/accept").json()
    assert accepted["offer"]["status"] == "accepted" and accepted["offer"]["valid_until"] == clock.now + 48 * HOUR
    assert accepted["messages"][-1]["text"] == "Offer of €80.00 accepted"

    paying = buyer.get(f"{API}/{CONV}").json()["offer"]
    assert paying["can_pay"] is True
    assert conversations_service.accepted_price(BUYER, "fridge", 10000) == (8000, paying["offer_id"])

    clock.now += 48 * HOUR
    assert buyer.get(f"{API}/{CONV}").json()["offer"]["status"] == "expired"
    assert conversations_service.accepted_price(BUYER, "fridge", 10000) is None


def test_a_counter_offer_replaces_the_pending_one(listings):
    start()
    offer(buyer, 6000)

    countered = offer(seller, 9000).json()

    assert countered["offer"]["amount_cents"] == 9000 and countered["offer"]["from"] == "me"
    assert countered["messages"][-1]["kind"] == "counter_offer"
    assert buyer.post(f"{API}/{CONV}/offer/accept").json()["offer"]["status"] == "accepted"
    assert conversations_service.accepted_price(BUYER, "fridge", 10000)[0] == 9000


def test_a_declined_offer_lets_the_buyer_try_again(listings):
    start()
    offer(buyer, 6000)

    declined = seller.post(f"{API}/{CONV}/offer/decline").json()

    assert declined["offer"]["status"] == "declined"
    assert buyer.get(f"{API}/{CONV}").json()["can_offer"] is True
    assert offer(buyer, 7000).status_code == 201


@pytest.mark.parametrize(
    "amount, detail",
    [(10000, "an offer must be below the price (€100.00)"), (4999, "an offer must be at least €50.00")],
)
def test_an_offer_must_be_between_half_and_the_price(listings, amount, detail):
    start()

    result = offer(buyer, amount)

    assert result.status_code == 422 and result.json()["detail"] == detail


def test_offers_follow_the_turns(listings):
    start()

    assert offer(seller, 9000).json()["detail"] == "you can only answer an offer of the buyer"
    offer(buyer, 6000)
    assert offer(buyer, 6500).json()["detail"] == "wait for the answer to your offer"
    assert buyer.post(f"{API}/{CONV}/offer/accept").json()["detail"] == "you cannot answer your own offer"
    seller.post(f"{API}/{CONV}/offer/accept")
    assert offer(buyer, 5000).json()["detail"] == "an offer is already accepted: pay it from the cart"
    assert seller.post(f"{API}/{CONV}/offer/decline").json()["detail"] == "there is no offer to answer"


def test_a_pending_offer_expires_after_24_hours(listings, clock):
    start()
    offer(buyer, 6000)

    clock.now += 24 * HOUR

    assert seller.get(f"{API}/{CONV}").json()["offer"]["status"] == "expired"
    assert seller.post(f"{API}/{CONV}/offer/accept").status_code == 409
    assert offer(buyer, 6000).status_code == 201


def test_a_sold_listing_cannot_be_negotiated(listings):
    start()
    offer(buyer, 6000)
    listings.listings["fridge"]["status"] = "sold"

    assert seller.post(f"{API}/{CONV}/offer/accept").json()["detail"] == "the listing is no longer for sale"
    assert offer(buyer, 6000).status_code == 409
    body = buyer.get(f"{API}/{CONV}").json()
    assert body["listing"]["status"] == "sold" and body["can_offer"] is False
    # Talking about it is still possible.
    assert buyer.post(f"{API}/{CONV}/messages", json={"text": "Too bad"}).status_code == 201


def test_too_many_offers_are_refused(listings, monkeypatch):
    monkeypatch.setattr(conversations_service, "MAX_OFFERS", 2)
    start()
    offer(buyer, 6000)
    offer(seller, 9000)

    assert offer(buyer, 7000).json()["detail"] == "too many offers in this conversation"


def test_offers_must_be_whole_cents(listings):
    start()

    assert offer(buyer, 80.5).status_code == 422
    assert buyer.post(f"{API}/{CONV}/offers", json={"amount_cents": "8000"}).status_code == 422


# ---------- paying an accepted offer ----------

@pytest.fixture
def market(monkeypatch, listings):
    store = FakeStore()
    store.items[("used", "fridge")] = listings.listings["fridge"]  # the same document
    store.set_cart(BUYER, [{"cart_item_id": "c1", "product_type": "used", "product_id": "fridge",
                            "quantity": 1, "added_at": 1, "price_cents": 10000}])
    gateway = FakeGateway()
    monkeypatch.setattr(checkout_service, "get_store", lambda: store)
    monkeypatch.setattr(checkout_service, "get_gateway", lambda: gateway)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    monkeypatch.delenv("STRIPE_CURRENCY", raising=False)
    return store, gateway


def test_the_cart_and_the_checkout_charge_the_accepted_offer(listings, market):
    store, gateway = market
    start()
    offer(buyer, 8000)
    offer_id = seller.post(f"{API}/{CONV}/offer/accept").json()["offer"]["offer_id"]

    line = buyer.get("/api/marketplace/cart").json()["items"][0]
    assert (line["unit_price_cents"], line["list_price_cents"], line["offer_id"]) == (8000, 10000, offer_id)
    assert line["price_changed"] is False

    checkout = buyer.post("/api/marketplace/checkout", json={}).json()
    assert checkout["subtotal_cents"] == 8000 and gateway.calls[0]["amount_cents"] == 8000

    payload, headers = signed(intent_event("payment_intent.succeeded", checkout, amount=8000, amount_received=8000))
    assert buyer.post("/api/marketplace/stripe/webhook", content=payload, headers=headers).status_code == 200
    assert store.used_offers == [offer_id]


def test_a_used_offer_is_not_charged_again(listings, market, conversations):
    start()
    offer(buyer, 8000)
    offer_id = seller.post(f"{API}/{CONV}/offer/accept").json()["offer"]["offer_id"]
    conversations.conversations[CONV]["offer_used"] = offer_id  # what the payment writes

    assert buyer.get(f"{API}/{CONV}").json()["offer"]["status"] == "used"
    assert buyer.get("/api/marketplace/cart").json()["items"][0]["unit_price_cents"] == 10000


def test_a_lower_listing_price_beats_the_offer(listings, market):
    start()
    offer(buyer, 8000)
    seller.post(f"{API}/{CONV}/offer/accept")
    listings.listings["fridge"]["price_cents"] = 7500

    line = buyer.get("/api/marketplace/cart").json()["items"][0]
    assert line["unit_price_cents"] == 7500 and line["offer_id"] is None


def test_someone_elses_offer_does_not_change_my_price(listings, market):
    start()
    offer(buyer, 8000)
    seller.post(f"{API}/{CONV}/offer/accept")

    assert conversations_service.accepted_price(STRANGER, "fridge", 10000) is None
