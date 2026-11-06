"""Firestore access for second-hand listings (#35).

    listings/{listingId}    a user's listing: owner_uid, status "active" | "sold", price_cents...

Only this backend writes listings. A user's listings are read with one
equality filter on ``owner_uid`` and the public ones with one on ``status``,
so no composite index is needed; they are sorted and paged in memory, like
the marketplace search.
"""

from __future__ import annotations

from typing import Any

import firebase_store

LISTINGS = "listings"
MAX_READ = 500


def _listings():
    return firebase_store.get_firestore_client().collection(LISTINGS)


def _rows(query) -> list[dict[str, Any]]:
    return [{**(doc.to_dict() or {}), "id": doc.id} for doc in query.limit(MAX_READ).stream()]


def list_by_owner(uid: str) -> list[dict[str, Any]]:
    return _rows(_listings().where("owner_uid", "==", uid))


def list_by_status(status: str) -> list[dict[str, Any]]:
    return _rows(_listings().where("status", "==", status))


def get_listing(listing_id: str) -> dict[str, Any] | None:
    snapshot = _listings().document(listing_id).get()
    return {**(snapshot.to_dict() or {}), "id": listing_id} if snapshot.exists else None


def update_listing(listing_id: str, fields: dict[str, Any]) -> None:
    _listings().document(listing_id).update(fields)
