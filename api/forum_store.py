"""Firestore access for the appliance forum (#44).

    forum_threads/{threadId}                  a discussion in one appliance category,
                                              optionally about one marketplace product
    forum_threads/{threadId}/replies/{id}     the replies to it
    forum_votes/{voteId}                      one upvote; id "{uid}_{threadId}[_{replyId}]"
    forum_reports/{reportId}                  one report; id "{uid}_{threadId}[_{replyId}]"

Only this backend writes these collections. Vote and report ids are derived
from the user, so ``create`` (which fails when the document exists) makes a
second vote or report from the same user a no-op.

Paginated lists (threads of a category, replies of a thread) are Firestore
queries ordered by the server and continued with ``start_after`` the last
document of the previous page; they need the indexes in
``firestore.indexes.json``. The questions of one product are few, so they are
read with a single equality filter and sorted in memory, with no index.
"""

from __future__ import annotations

from typing import Any

import firebase_store
from google.api_core import exceptions as google_exceptions

THREADS = "forum_threads"
REPLIES = "replies"
VOTES = "forum_votes"
REPORTS = "forum_reports"
VISIBLE = "visible"

# Upper bound on what one product page reads.
MAX_THREADS_READ = 200
MAX_REPLIES_READ = 200


class InvalidCursor(Exception):
    """The cursor does not point to a document of this list."""


def _db():
    return firebase_store.get_firestore_client()


def _increment(amount: int):
    from firebase_admin import firestore

    return firestore.Increment(amount)


def _descending() -> str:
    from firebase_admin import firestore

    return firestore.Query.DESCENDING


def _threads():
    return _db().collection(THREADS)


def _post_ref(thread_id: str, reply_id: str | None = None):
    thread = _threads().document(thread_id)
    return thread.collection(REPLIES).document(reply_id) if reply_id else thread


def _page(query, collection, cursor: str | None, limit: int, key: str) -> tuple[list[dict[str, Any]], bool]:
    """Run ``query`` from after ``cursor``; returns one page and whether more follow."""
    if cursor:
        snapshot = collection.document(cursor).get()
        if not snapshot.exists:
            raise InvalidCursor(cursor)
        query = query.start_after(snapshot)
    docs = list(query.limit(limit + 1).stream())
    return [{**(doc.to_dict() or {}), key: doc.id} for doc in docs[:limit]], len(docs) > limit


# threads

def list_category_threads(category: str, order: str, limit: int,
                          cursor: str | None) -> tuple[list[dict[str, Any]], bool]:
    """Visible threads of a category, by ``order`` ("recent" or "popular")."""
    query = _threads().where("category", "==", category).where("status", "==", VISIBLE)
    if order == "popular":
        query = query.order_by("upvotes", direction=_descending())
    query = query.order_by("last_activity_at", direction=_descending())
    return _page(query, _threads(), cursor, limit, "thread_id")


def list_product_threads(product_key: str) -> list[dict[str, Any]]:
    query = _threads().where("product_key", "==", product_key).limit(MAX_THREADS_READ)
    return [{**(doc.to_dict() or {}), "thread_id": doc.id} for doc in query.stream()]


def get_thread(thread_id: str) -> dict[str, Any] | None:
    snapshot = _post_ref(thread_id).get()
    return {**(snapshot.to_dict() or {}), "thread_id": thread_id} if snapshot.exists else None


def create_thread(data: dict[str, Any]) -> str:
    ref = _threads().document()
    ref.set(data)
    return ref.id


# replies

def list_replies(thread_id: str, limit: int, cursor: str | None) -> tuple[list[dict[str, Any]], bool]:
    """Visible replies, oldest first."""
    replies = _post_ref(thread_id).collection(REPLIES)
    query = replies.where("status", "==", VISIBLE).order_by("created_at")
    return _page(query, replies, cursor, limit, "reply_id")


def all_replies(thread_id: str) -> list[dict[str, Any]]:
    """Every reply of a thread, unsorted (to pick the best one on a product page)."""
    query = _post_ref(thread_id).collection(REPLIES).limit(MAX_REPLIES_READ)
    return [{**(doc.to_dict() or {}), "reply_id": doc.id} for doc in query.stream()]


def get_reply(thread_id: str, reply_id: str) -> dict[str, Any] | None:
    snapshot = _post_ref(thread_id, reply_id).get()
    return {**(snapshot.to_dict() or {}), "reply_id": reply_id} if snapshot.exists else None


def create_reply(thread_id: str, data: dict[str, Any]) -> str:
    """Save a reply and update the thread counters in one batch."""
    db = _db()
    thread = _post_ref(thread_id)
    ref = thread.collection(REPLIES).document()
    batch = db.batch()
    batch.set(ref, data)
    batch.update(thread, {"reply_count": _increment(1), "last_activity_at": data["created_at"]})
    batch.commit()
    return ref.id


# changes to a post

def update_post(thread_id: str, reply_id: str | None, fields: dict[str, Any]) -> None:
    _post_ref(thread_id, reply_id).update(fields)


def set_status(thread_id: str, reply_id: str | None, status: str, reply_count_change: int = 0) -> None:
    """Hide, restore or delete a post; a reply leaving or entering the thread moves its counter."""
    db = _db()
    batch = db.batch()
    batch.update(_post_ref(thread_id, reply_id), {"status": status})
    if reply_id and reply_count_change:
        batch.update(_post_ref(thread_id), {"reply_count": _increment(reply_count_change)})
    batch.commit()


# votes and reports

def voted(vote_ids: list[str]) -> set[str]:
    """Return which of these vote ids exist (one round trip)."""
    if not vote_ids:
        return set()
    db = _db()
    refs = [db.collection(VOTES).document(vote_id) for vote_id in vote_ids]
    return {snapshot.id for snapshot in db.get_all(refs) if snapshot.exists}


def add_vote(vote_id: str, data: dict[str, Any], thread_id: str, reply_id: str | None) -> bool:
    """Record an upvote; False when this user had already voted."""
    try:
        _db().collection(VOTES).document(vote_id).create(data)
    except google_exceptions.Conflict:
        return False
    _post_ref(thread_id, reply_id).update({"upvotes": _increment(1)})
    return True


def remove_vote(vote_id: str, thread_id: str, reply_id: str | None) -> bool:
    """Withdraw an upvote; False when there was none."""
    db = _db()
    ref = db.collection(VOTES).document(vote_id)
    if not ref.get().exists:
        return False
    batch = db.batch()
    batch.delete(ref)
    batch.update(_post_ref(thread_id, reply_id), {"upvotes": _increment(-1)})
    batch.commit()
    return True


def add_report(report_id: str, data: dict[str, Any], thread_id: str, reply_id: str | None) -> bool:
    """Store a report; False when this user had already reported the post."""
    try:
        _db().collection(REPORTS).document(report_id).create(data)
    except google_exceptions.Conflict:
        return False
    _post_ref(thread_id, reply_id).update({"report_count": _increment(1)})
    return True
