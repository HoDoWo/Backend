"""Marketplace cart (#42): the bridge between the catalogue and the checkout.

Every cart line is a document in ``carts`` (CART_ITEM in the ER diagram) with
``uid``, ``product_type``, ``product_id``, ``quantity``, the price when it was
added (``price_cents``) and ``added_at``. The document id is derived from the
user and the product, so adding the same product twice increases the quantity
instead of creating a second line.

Totals are always computed here from the current catalogue prices, the same
ones the checkout charges; the price saved when the item was added is only
used to tell the user that it changed.
"""

from __future__ import annotations

from typing import Any

import checkout_service
import config
import marketplace_store

MAX_LINES = 50
MAX_QUANTITY = 99


class CartError(checkout_service.CheckoutError):
    """Cart change refused; carries the HTTP status and a client-safe detail."""


def get_store():
    """Return the persistence backend (replaced by a fake in the tests)."""
    return checkout_service.get_store()


def cart_item_id(uid: str, product_type: str, product_id: str) -> str:
    # Firestore ids cannot contain "/"; uids and product ids never do.
    return f"{uid}_{product_type}_{product_id}"


def _currency() -> str:
    return (config.get_optional_env("STRIPE_CURRENCY") or config.DEFAULT_STRIPE_CURRENCY).lower()


def _image(item: dict[str, Any]) -> str | None:
    if item.get("image_url"):
        return item["image_url"]
    images = item.get("image_urls")
    return images[0] if isinstance(images, list) and images else None


def get_cart(uid: str) -> dict[str, Any]:
    """Return the cart with current prices, availability and server-side totals."""
    store = get_store()
    lines = []
    total_cents = 0
    item_count = 0

    for cart_item in sorted(store.get_cart_items(uid), key=lambda line: line.get("added_at") or 0):
        product_type = cart_item.get("product_type")
        product_id = str(cart_item.get("product_id") or "")
        quantity = cart_item.get("quantity") or 1
        item = (
            store.get_item(product_type, product_id)
            if product_type in marketplace_store.PRODUCT_SOURCES and product_id
            else None
        )
        problem = checkout_service.availability_problem(uid, product_type, item, quantity) if item else "not available"
        unit_price = item.get("price_cents") if item and not problem else None
        added_price = cart_item.get("price_cents")

        line = {
            "cart_item_id": cart_item["cart_item_id"],
            "product_type": product_type,
            "product_id": product_id,
            "title": str((item or {}).get("title") or ""),
            "image_url": _image(item or {}),
            "quantity": quantity,
            "unit_price_cents": unit_price,
            "price_cents": unit_price * quantity if unit_price else None,
            "added_price_cents": added_price,
            "price_changed": bool(unit_price and added_price and unit_price != added_price),
            "available": problem is None,
            "unavailable_reason": problem,
        }
        if problem is None:
            total_cents += line["price_cents"]
            item_count += quantity
        lines.append(line)

    return {
        "items": lines,
        "item_count": item_count,  # for the cart badge: available items only
        "total_cents": total_cents,
        "currency": _currency(),
        "has_unavailable_items": any(not line["available"] for line in lines),
    }


def _buyable_item(uid: str, product_type: str, product_id: str, quantity: int) -> dict[str, Any]:
    item = get_store().get_item(product_type, product_id)
    if item is None:
        raise CartError(404, "product not found")
    problem = checkout_service.availability_problem(uid, product_type, item, quantity)
    if problem:
        raise CartError(409, problem)
    return item


def add_item(uid: str, product_type: str, product_id: str, quantity: int) -> dict[str, Any]:
    """Add a product, or increase its quantity when it is already in the cart."""
    store = get_store()
    line_id = cart_item_id(uid, product_type, product_id)
    existing = store.get_cart_item(line_id)

    if existing is None and len(store.get_cart_items(uid)) >= MAX_LINES:
        raise CartError(409, f"the cart can hold at most {MAX_LINES} different products")

    new_quantity = quantity + (existing.get("quantity", 0) if existing else 0)
    if new_quantity > MAX_QUANTITY:
        raise CartError(422, f"the quantity cannot be more than {MAX_QUANTITY}")

    item = _buyable_item(uid, product_type, product_id, new_quantity)
    if existing is None:
        store.save_cart_item(
            line_id,
            {
                "uid": uid,
                "product_type": product_type,
                "product_id": product_id,
                "quantity": new_quantity,
                "price_cents": item["price_cents"],
                "added_at": checkout_service.now_ms(),
            },
        )
    else:
        # Keep the price of the first add, so a later change is still reported.
        store.update_cart_item(line_id, {"quantity": new_quantity})
    return get_cart(uid)


def _own_line(uid: str, line_id: str) -> dict[str, Any]:
    line = get_store().get_cart_item(line_id)
    # Another user's line is reported as missing: its id must not leak.
    if line is None or line.get("uid") != uid:
        raise CartError(404, "cart item not found")
    return line


def set_quantity(uid: str, line_id: str, quantity: int) -> dict[str, Any]:
    line = _own_line(uid, line_id)
    _buyable_item(uid, line["product_type"], line["product_id"], quantity)
    get_store().update_cart_item(line_id, {"quantity": quantity})
    return get_cart(uid)


def remove_item(uid: str, line_id: str) -> dict[str, Any]:
    _own_line(uid, line_id)
    get_store().delete_cart_item(line_id)
    return get_cart(uid)
