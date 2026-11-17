"""Firestore access for product reviews.

    reviews/{productType}__{productId}__{uid}   one review per buyer and product

Only this backend reads and writes the collection (closed in the Firestore
rules), so the author's uid never reaches the app. The reviews of a product are
read with one equality filter on ``product_key`` and sorted in memory, so no
composite index is needed. After every change the product document gets its
``rating`` (average) and ``review_count``, which the marketplace cards show.
"""

from __future__ import annotations

from typing import Any

import firebase_store
import marketplace_store

REVIEWS = "reviews"
MAX_REVIEWS_READ = 500


def _db():
    return firebase_store.get_firestore_client()


def get_review(review_id: str) -> dict[str, Any] | None:
    snapshot = _db().collection(REVIEWS).document(review_id).get()
    return {**(snapshot.to_dict() or {}), "id": review_id} if snapshot.exists else None


def save_review(review_id: str, data: dict[str, Any]) -> None:
    _db().collection(REVIEWS).document(review_id).set(data)


def delete_review(review_id: str) -> None:
    _db().collection(REVIEWS).document(review_id).delete()


def list_product_reviews(product_key: str) -> list[dict[str, Any]]:
    query = _db().collection(REVIEWS).where("product_key", "==", product_key).limit(MAX_REVIEWS_READ)
    return [{**(doc.to_dict() or {}), "id": doc.id} for doc in query.stream()]


def set_product_rating(product_type: str, product_id: str, rating: float, count: int) -> None:
    """The average and the number of reviews on the product, for the marketplace cards."""
    collection, _ = marketplace_store.PRODUCT_SOURCES[product_type]
    _db().collection(collection).document(product_id).update({"rating": rating, "review_count": count})
