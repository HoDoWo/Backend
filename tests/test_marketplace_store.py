"""Firestore side of the checkout (#43): idempotency marker and paid-order batch."""

from __future__ import annotations

import pytest
from google.api_core import exceptions as google_exceptions

import firebase_store
import marketplace_store


class FakeDoc:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def get(self):
        data = self.db.docs.get(self.path)
        return type("Snap", (), {"exists": data is not None, "to_dict": lambda _self: data})()

    def create(self, data):
        if self.path in self.db.docs:
            raise google_exceptions.AlreadyExists("exists")
        self.db.docs[self.path] = dict(data)

    def delete(self):
        self.db.docs.pop(self.path, None)

    def collection(self, name):
        return FakeCollection(self.db, f"{self.path}/{name}")


class FakeCollection:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def document(self, doc_id):
        return FakeDoc(self.db, f"{self.path}/{doc_id}")


class FakeBatch:
    def __init__(self, db):
        self.db, self.ops = db, []

    def update(self, ref, fields):
        self.ops.append(("update", ref.path, fields))

    def delete(self, ref):
        self.ops.append(("delete", ref.path, None))

    def commit(self):
        self.db.commits.append(self.ops)


class FakeDb:
    def __init__(self):
        self.docs, self.commits = {}, []

    def collection(self, name):
        return FakeCollection(self, name)

    def batch(self):
        return FakeBatch(self)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: fake)
    return fake


def test_an_event_can_be_claimed_only_once(db):
    assert marketplace_store.claim_event("evt_1", "payment_intent.succeeded", 1) is True
    assert marketplace_store.claim_event("evt_1", "payment_intent.succeeded", 2) is False

    marketplace_store.release_event("evt_1")
    assert marketplace_store.claim_event("evt_1", "payment_intent.succeeded", 3) is True


def test_fulfil_writes_status_listing_stock_and_cart_in_one_batch(db):
    order = {
        "order_id": "o1",
        "uid": "buyer-1",
        "items": [
            {"product_type": "used", "product_id": "fridge-2", "quantity": 1},
            {"product_type": "part", "product_id": "pump-1", "quantity": 2},
        ],
        "cart_item_ids": ["c1", "c2"],
    }

    db.docs["listings/fridge-2"] = {"status": "active"}  # no quantity: one item

    marketplace_store.fulfil_order(order, paid_at=123)

    assert len(db.commits) == 1
    ops = {(kind, path) for kind, path, _ in db.commits[0]}
    assert ops == {
        ("update", "orders/o1"),
        ("update", "listings/fridge-2"),
        ("update", "parts/pump-1"),
        ("delete", "carts/c1"),
        ("delete", "carts/c2"),
    }
    fields = {path: f for _, path, f in db.commits[0]}
    assert fields["orders/o1"] == {"status": "paid", "paid_at": 123}
    assert fields["listings/fridge-2"] == {"quantity": 0, "updated_at": 123, "status": "sold", "sold_at": 123}


def test_a_listing_with_items_left_stays_active(db):
    db.docs["listings/pans"] = {"status": "active", "quantity": 3}
    order = {"order_id": "o2", "uid": "b", "cart_item_ids": [],
             "items": [{"product_type": "used", "product_id": "pans", "quantity": 1}]}

    marketplace_store.fulfil_order(order, paid_at=5)

    fields = {path: f for _, path, f in db.commits[0]}
    assert fields["listings/pans"] == {"quantity": 2, "updated_at": 5}


def test_a_paid_offer_is_marked_used_in_the_same_batch(db):
    db.docs["listings/fridge-2"] = {"status": "active"}
    order = {"order_id": "o3", "uid": "buyer-1", "cart_item_ids": [],
             "items": [{"product_type": "used", "product_id": "fridge-2", "quantity": 1, "offer_id": "of1"}]}

    marketplace_store.fulfil_order(order, paid_at=7)

    fields = {path: f for _, path, f in db.commits[0]}
    assert fields["conversations/fridge-2__buyer-1"] == {"offer_used": "of1", "updated_at": 7}
