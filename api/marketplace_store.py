"""Firestore access for the marketplace checkout and orders (#43).

Collection and field names follow the Sprint 2 ER diagram (#31):

    carts/{id}               CART_ITEM: uid, product_type ("new" | "used" | "part"), product_id, quantity
    partner_products/{id}    PARTNER_PRODUCT: price_cents, stock, partner_id, title
    parts/{id}               PART: price_cents, stock, partner_id, title
    listings/{id}            LISTING: price_cents, status ("active" | "sold"), owner_uid, title
    orders/{id}              ORDER with ORDER_ITEM embedded as "items"; written only by this backend
    stripe_events/{id}       STRIPE_EVENT: Stripe events already processed (idempotency)

If #31 settles on different names, only the constants below need to change.
"""

from __future__ import annotations

from typing import Any

import firebase_store
from google.api_core import exceptions as google_exceptions

# product_type -> (collection, field holding the seller id)
PRODUCT_SOURCES: dict[str, tuple[str, str]] = {
    "new": ("partner_products", "partner_id"),
    "part": ("parts", "partner_id"),
    "used": ("listings", "owner_uid"),
}

# Parts without a partner are sold by HoDoWo itself.
PLATFORM_SELLER_ID = "hodowo"


def _db():
    return firebase_store.get_firestore_client()


def get_cart_items(uid: str) -> list[dict[str, Any]]:
    """Return the user's cart lines, each with its document id as ``cart_item_id``."""
    items = _db().collection("carts").where("uid", "==", uid).stream()
    return [{**(doc.to_dict() or {}), "cart_item_id": doc.id} for doc in items]


def get_cart_item(cart_item_id: str) -> dict[str, Any] | None:
    snapshot = _db().collection("carts").document(cart_item_id).get()
    return {**(snapshot.to_dict() or {}), "cart_item_id": cart_item_id} if snapshot.exists else None


def save_cart_item(cart_item_id: str, data: dict[str, Any]) -> None:
    _db().collection("carts").document(cart_item_id).set(data)


def update_cart_item(cart_item_id: str, fields: dict[str, Any]) -> None:
    _db().collection("carts").document(cart_item_id).update(fields)


def delete_cart_item(cart_item_id: str) -> None:
    _db().collection("carts").document(cart_item_id).delete()


def get_item(product_type: str, product_id: str) -> dict[str, Any] | None:
    """Return the current catalogue document behind a cart line, or None."""
    collection, seller_field = PRODUCT_SOURCES[product_type]
    snapshot = _db().collection(collection).document(product_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    data["seller_id"] = str(data.get(seller_field) or PLATFORM_SELLER_ID)
    return data


def save_order(order_id: str, order: dict[str, Any]) -> None:
    _db().collection("orders").document(order_id).set(order)


def update_order(order_id: str, fields: dict[str, Any]) -> None:
    _db().collection("orders").document(order_id).update(fields)


def get_order(order_id: str) -> dict[str, Any] | None:
    snapshot = _db().collection("orders").document(order_id).get()
    return {"order_id": order_id, **snapshot.to_dict()} if snapshot.exists else None


def get_orders_by_checkout(checkout_id: str) -> list[dict[str, Any]]:
    query = _db().collection("orders").where("checkout_id", "==", checkout_id)
    return [{"order_id": doc.id, **doc.to_dict()} for doc in query.stream()]


def list_user_orders(uid: str, limit: int) -> list[dict[str, Any]]:
    # Filter only on uid so no composite index is needed; sort in memory.
    query = _db().collection("orders").where("uid", "==", uid)
    orders = [{"order_id": doc.id, **doc.to_dict()} for doc in query.stream()]
    orders.sort(key=lambda order: order.get("created_at") or 0, reverse=True)
    return orders[:limit]


def claim_event(event_id: str, event_type: str, created_at: int) -> bool:
    """Record a Stripe event; False when it was already processed.

    ``create`` fails if the document exists, so two concurrent deliveries of
    the same event cannot both be processed.
    """
    try:
        _db().collection("stripe_events").document(event_id).create(
            {"type": event_type, "processed_at": created_at}
        )
    except google_exceptions.Conflict:
        return False
    return True


def release_event(event_id: str) -> None:
    """Forget an event whose processing failed, so Stripe's retry is handled."""
    _db().collection("stripe_events").document(event_id).delete()


def fulfil_order(order: dict[str, Any], paid_at: int) -> None:
    """Mark an order paid and apply it in one batch.

    Listing quantity and catalogue stock are decremented (a listing becomes
    sold at 0) and the cart lines are removed,
    so the order is never "paid" without its side effects (or the reverse).
    """
    from firebase_admin import firestore

    db = _db()
    batch = db.batch()
    batch.update(
        db.collection("orders").document(order["order_id"]),
        {"status": "paid", "paid_at": paid_at},
    )
    for item in order["items"]:
        collection, _ = PRODUCT_SOURCES[item["product_type"]]
        ref = db.collection(collection).document(item["product_id"])
        if item["product_type"] == "used":
            snapshot = ref.get()
            listing = (snapshot.to_dict() or {}) if snapshot.exists else {}
            offered = 1 if listing.get("quantity") is None else int(listing["quantity"])
            remaining = max(offered - int(item["quantity"]), 0)
            fields = {"quantity": remaining, "updated_at": paid_at}
            if remaining == 0:
                fields.update(status="sold", sold_at=paid_at)
            batch.update(ref, fields)
        else:
            batch.update(ref, {"stock": firestore.Increment(-int(item["quantity"]))})
    for cart_item_id in order.get("cart_item_ids", []):
        batch.delete(db.collection("carts").document(cart_item_id))
    batch.commit()
