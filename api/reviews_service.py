"""Product reviews: only who bought a product can review it.

* One review per buyer and product: writing again changes it.
* A review is a rating from 1 to 5 stars and an optional text.
* "Bought" means a paid order with the product, so every review is a
  verified purchase; the seller of a listing cannot review it.
* The list shows the newest first, with the average, the number of reviews and
  how many there are for each number of stars. The author's name is shown, the
  uid never.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import checkout_service
import forum_service
import reviews_store

PRODUCT_TYPES = ("new", "part", "used")
MAX_TEXT = 1000
PAGE = 50
ORDERS_CHECKED = 200


class ReviewError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def get_review_store():
    """Return the persistence backend (replaced by a fake in the tests)."""
    return reviews_store


def product_key(product_type: str, product_id: str) -> str:
    return f"{product_type}__{product_id}"


def _review_id(product_type: str, product_id: str, uid: str) -> str:
    return f"{product_key(product_type, product_id)}__{uid}"


def _check_product(product_type: str, product_id: str) -> dict[str, Any]:
    if product_type not in PRODUCT_TYPES:
        raise ReviewError(422, "unknown product type")
    product = checkout_service.get_store().get_item(product_type, product_id)
    if product is None:
        raise ReviewError(404, "product not found")
    return product


def bought(uid: str, product_type: str, product_id: str) -> bool:
    """True when one of the user's paid orders has this product."""
    for order in checkout_service.get_store().list_user_orders(uid, ORDERS_CHECKED):
        if order.get("status") != "paid":
            continue
        for item in order.get("items") or []:
            if item.get("product_type") == product_type and item.get("product_id") == product_id:
                return True
    return False


def _public(review: dict[str, Any], uid: str) -> dict[str, Any]:
    return {
        "id": review["id"],
        "author_name": review.get("author_name") or forum_service.DEFAULT_AUTHOR,
        "rating": int(review.get("rating") or 0),
        "text": review.get("text") or "",
        "created_at": review.get("created_at"),
        "updated_at": review.get("updated_at"),
        "verified_purchase": True,
        "mine": review.get("uid") == uid,
    }


def _summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    stars = Counter(int(review.get("rating") or 0) for review in reviews)
    count = len(reviews)
    average = round(sum(int(r.get("rating") or 0) for r in reviews) / count, 1) if count else 0.0
    return {"average": average, "count": count, "stars": {str(n): stars.get(n, 0) for n in range(5, 0, -1)}}


def list_reviews(uid: str, product_type: str, product_id: str) -> dict[str, Any]:
    _check_product(product_type, product_id)
    rows = get_review_store().list_product_reviews(product_key(product_type, product_id))
    rows.sort(key=lambda review: review.get("updated_at") or review.get("created_at") or 0, reverse=True)
    mine = next((review for review in rows if review.get("uid") == uid), None)
    return {
        "summary": _summary(rows),
        "items": [_public(review, uid) for review in rows[:PAGE]],
        "my_review": _public(mine, uid) if mine else None,
        "can_review": mine is not None or bought(uid, product_type, product_id),
    }


def _refresh_rating(product_type: str, product_id: str) -> None:
    store = get_review_store()
    summary = _summary(store.list_product_reviews(product_key(product_type, product_id)))
    store.set_product_rating(product_type, product_id, summary["average"], summary["count"])


def save_review(user: dict[str, Any], uid: str, product_type: str, product_id: str,
                rating: int, text: str | None) -> dict[str, Any]:
    product = _check_product(product_type, product_id)
    if not 1 <= int(rating) <= 5:
        raise ReviewError(422, "rating must be between 1 and 5")
    clean = str(text or "").strip()
    if len(clean) > MAX_TEXT:
        raise ReviewError(422, f"text must be at most {MAX_TEXT} characters")
    if product.get("seller_id") == uid:
        raise ReviewError(403, "you cannot review your own listing")
    store = get_review_store()
    review_id = _review_id(product_type, product_id, uid)
    existing = store.get_review(review_id)
    if existing is None and not bought(uid, product_type, product_id):
        raise ReviewError(403, "only buyers of this product can review it")
    now = checkout_service.now_ms()
    data = {
        "product_key": product_key(product_type, product_id),
        "product_type": product_type,
        "product_id": product_id,
        "uid": uid,
        "author_name": forum_service.author_name(user),
        "rating": int(rating),
        "text": clean,
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }
    store.save_review(review_id, data)
    _refresh_rating(product_type, product_id)
    return _public({**data, "id": review_id}, uid)


def delete_review(uid: str, product_type: str, product_id: str) -> None:
    _check_product(product_type, product_id)
    store = get_review_store()
    review_id = _review_id(product_type, product_id, uid)
    if store.get_review(review_id) is None:
        raise ReviewError(404, "you have not reviewed this product")
    store.delete_review(review_id)
    _refresh_rating(product_type, product_id)
