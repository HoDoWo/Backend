"""Firestore side of the listings (#35)."""

from __future__ import annotations

import pytest

import firebase_store
import listings_store
from test_forum_store import Db


@pytest.fixture
def db(monkeypatch):
    fake = Db()
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: fake)
    fake.docs.update({
        "listings/a": {"owner_uid": "u1", "status": "active"},
        "listings/b": {"owner_uid": "u1", "status": "sold"},
        "listings/c": {"owner_uid": "u2", "status": "active"},
    })
    return fake


def test_listings_are_read_by_owner_and_by_status(db):
    assert sorted(item["id"] for item in listings_store.list_by_owner("u1")) == ["a", "b"]
    assert sorted(item["id"] for item in listings_store.list_by_status("active")) == ["a", "c"]


def test_one_listing_is_read_and_updated(db):
    listings_store.update_listing("a", {"status": "sold"})

    assert listings_store.get_listing("a") == {"owner_uid": "u1", "status": "sold", "id": "a"}
    assert listings_store.get_listing("missing") is None
