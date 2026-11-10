"""Firestore side of buyer-seller conversations: ids, inbox, messages and unread counters."""

from __future__ import annotations

import pytest

import conversations_store
import firebase_store
from test_forum_store import Db, Inc


@pytest.fixture
def db(monkeypatch):
    fake = Db()
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: fake)
    monkeypatch.setattr(conversations_store, "_increment", Inc)
    monkeypatch.setattr(conversations_store, "_descending", lambda: "DESCENDING")
    return fake


def test_one_conversation_per_listing_and_buyer(db):
    conv_id = conversations_store.conversation_id("fridge", "buyer-1")

    assert conv_id == "fridge__buyer-1"
    assert conversations_store.create_conversation(conv_id, {"buyer_uid": "buyer-1", "unread_seller": 0}) is True
    assert conversations_store.create_conversation(conv_id, {"buyer_uid": "other"}) is False
    assert conversations_store.get_conversation(conv_id) == {"buyer_uid": "buyer-1", "unread_seller": 0, "id": conv_id}
    assert conversations_store.get_conversation("missing") is None


def test_the_inbox_has_the_conversations_where_the_user_buys_or_sells(db):
    db.docs["conversations/a"] = {"buyer_uid": "u1", "seller_uid": "s1"}
    db.docs["conversations/b"] = {"buyer_uid": "b2", "seller_uid": "u1"}
    db.docs["conversations/c"] = {"buyer_uid": "b3", "seller_uid": "s3"}

    assert sorted(row["id"] for row in conversations_store.list_for_user("u1")) == ["a", "b"]


def test_a_message_and_the_conversation_change_are_written_together(db):
    db.docs["conversations/c1"] = {"unread_seller": 1, "updated_at": 1}

    message_id = conversations_store.add_message(
        "c1", {"text": "hi", "created_at": 5}, {"updated_at": 5}, unread_role="seller"
    )

    assert db.commits == 1
    assert db.docs[f"conversations/c1/messages/{message_id}"] == {"text": "hi", "created_at": 5}
    assert db.docs["conversations/c1"] == {"unread_seller": 2, "updated_at": 5}


def test_messages_are_the_newest_ones_oldest_first(db):
    db.docs["conversations/c1"] = {}
    for at in (3, 1, 2):
        db.docs[f"conversations/c1/messages/m{at}"] = {"created_at": at}

    assert [m["id"] for m in conversations_store.list_messages("c1", limit=2)] == ["m2", "m3"]

    conversations_store.update_conversation("c1", {"unread_buyer": 0})
    assert db.docs["conversations/c1"] == {"unread_buyer": 0}
