"""Firestore side of product reviews."""

from __future__ import annotations

import firebase_store
import pytest
import reviews_store
from test_forum_store import Db


@pytest.fixture
def db(monkeypatch):
    fake = Db()
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: fake)
    return fake


def test_a_review_is_saved_read_and_deleted(db):
    reviews_store.save_review("part__P1__u1", {"product_key": "part__P1", "rating": 4})

    assert reviews_store.get_review("part__P1__u1") == {"product_key": "part__P1", "rating": 4, "id": "part__P1__u1"}

    reviews_store.delete_review("part__P1__u1")
    assert reviews_store.get_review("part__P1__u1") is None


def test_the_reviews_of_one_product(db):
    db.docs["reviews/a"] = {"product_key": "part__P1", "rating": 5}
    db.docs["reviews/b"] = {"product_key": "part__P2", "rating": 1}

    assert [r["id"] for r in reviews_store.list_product_reviews("part__P1")] == ["a"]


def test_the_product_gets_its_average_and_count(db):
    db.docs["parts/P1"] = {"title": "Pump"}
    db.docs["listings/L1"] = {"title": "Fridge"}

    reviews_store.set_product_rating("part", "P1", 4.5, 2)
    reviews_store.set_product_rating("used", "L1", 3.0, 1)

    assert db.docs["parts/P1"] == {"title": "Pump", "rating": 4.5, "review_count": 2}
    assert db.docs["listings/L1"]["review_count"] == 1
