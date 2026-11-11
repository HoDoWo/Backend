"""Firestore access for buyer-seller conversations and offers.

    conversations/{listingId}__{buyerUid}            one conversation per listing and buyer:
                                                    participants, listing snapshot, unread
                                                    counters, last message and current offer
    conversations/{id}/messages/{messageId}         its messages, offers and offer answers

Only this backend reads and writes these collections (the Firestore rules deny
the app), so the other participant's uid never reaches the app. A user's
conversations are read with one equality filter on ``buyer_uid`` and one on
``seller_uid`` and sorted in memory, so no composite index is needed; the
messages of a conversation are ordered by ``created_at`` (single-field index).
"""

from __future__ import annotations

from typing import Any

import firebase_store
from google.api_core import exceptions as google_exceptions

CONVERSATIONS = "conversations"
MESSAGES = "messages"
# Upper bounds on what one request reads.
MAX_CONVERSATIONS_READ = 200
MAX_MESSAGES_READ = 200


def _db():
    return firebase_store.get_firestore_client()


def _increment(amount: int):
    from firebase_admin import firestore

    return firestore.Increment(amount)


def _descending():
    from firebase_admin import firestore

    return firestore.Query.DESCENDING


def conversation_id(listing_id: str, buyer_uid: str) -> str:
    """Listing ids and uids never contain "__", so the id names one buyer of one listing."""
    return f"{listing_id}__{buyer_uid}"


def _conversations():
    return _db().collection(CONVERSATIONS)


def get_conversation(conv_id: str) -> dict[str, Any] | None:
    snapshot = _conversations().document(conv_id).get()
    return {**(snapshot.to_dict() or {}), "id": conv_id} if snapshot.exists else None


def create_conversation(conv_id: str, data: dict[str, Any]) -> bool:
    """Create the conversation; False when it already exists (two taps on "Message")."""
    try:
        _conversations().document(conv_id).create(data)
    except google_exceptions.AlreadyExists:
        return False
    return True


def list_for_user(uid: str) -> list[dict[str, Any]]:
    """The conversations where the user buys or sells, in no particular order."""
    rows: dict[str, dict[str, Any]] = {}
    for field in ("buyer_uid", "seller_uid"):
        query = _conversations().where(field, "==", uid).limit(MAX_CONVERSATIONS_READ)
        for doc in query.stream():
            rows[doc.id] = {**(doc.to_dict() or {}), "id": doc.id}
    return list(rows.values())


def update_conversation(conv_id: str, fields: dict[str, Any]) -> None:
    _conversations().document(conv_id).update(fields)


def add_message(conv_id: str, message: dict[str, Any], fields: dict[str, Any],
                unread_role: str | None) -> str:
    """Write a message and the conversation fields it changes in one batch.

    ``unread_role`` ("buyer" or "seller") is the participant whose unread
    counter goes up: the one who did not write the message.
    """
    db = _db()
    conversation = db.collection(CONVERSATIONS).document(conv_id)
    ref = conversation.collection(MESSAGES).document()
    changes = dict(fields)
    if unread_role:
        changes[f"unread_{unread_role}"] = _increment(1)
    batch = db.batch()
    batch.set(ref, message)
    batch.update(conversation, changes)
    batch.commit()
    return ref.id


def list_messages(conv_id: str, limit: int = MAX_MESSAGES_READ) -> list[dict[str, Any]]:
    """The newest ``limit`` messages, oldest first."""
    query = (
        _conversations().document(conv_id).collection(MESSAGES)
        .order_by("created_at", direction=_descending())
        .limit(min(limit, MAX_MESSAGES_READ))
    )
    rows = [{**(doc.to_dict() or {}), "id": doc.id} for doc in query.stream()]
    rows.reverse()
    return rows
