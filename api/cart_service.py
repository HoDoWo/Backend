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

import datetime as dt
from typing import Any

import checkout_service
import config
import fulfilment
import marketplace_store

MAX_LINES = 50
MAX_QUANTITY = 99
# How the buyer can pay in the PaymentSheet; Google Pay is a card wallet.
PAYMENT_METHODS = ("card", "google_pay", "klarna")


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


def _line(uid: str, cart_item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One cart row with current price and availability, and the catalogue item behind it."""
    product_type = cart_item.get("product_type")
    product_id = str(cart_item.get("product_id") or "")
    quantity = cart_item.get("quantity") or 1
    item = (
        get_store().get_item(product_type, product_id)
        if product_type in marketplace_store.PRODUCT_SOURCES and product_id
        else None
    )
    problem = checkout_service.availability_problem(uid, product_type, item, quantity) if item else "not available"
    unit_price, offer_id = (
        checkout_service.unit_price(uid, product_type, product_id, item) if item and not problem else (None, None)
    )
    added_price = cart_item.get("price_cents")
    known = item or {}
    line = {
        "cart_item_id": cart_item["cart_item_id"],
        "product_type": product_type,
        "product_id": product_id,
        "title": str(known.get("title") or ""),
        "subtitle": fulfilment.item_subtitle(product_type, known) if item else "",
        "image_url": _image(known),
        "seller_id": known.get("seller_id"),
        "seller_name": fulfilment.seller_name(product_type, known) if item else None,
        "seller_kind": fulfilment.seller_kind(product_type),
        "quantity": quantity,
        "unit_price_cents": unit_price,
        "price_cents": unit_price * quantity if unit_price else None,
        "added_price_cents": added_price,
        # An accepted offer is a price the buyer agreed, not a change to warn about.
        "price_changed": bool(unit_price and added_price and unit_price != added_price and not offer_id),
        "offer_id": offer_id,
        "list_price_cents": item.get("price_cents") if offer_id else None,
        "available": problem is None,
        "unavailable_reason": problem,
    }
    return line, item


def quote(uid: str, selected_ids: list[str] | None = None,
          choices: dict[str, str] | None = None) -> dict[str, Any]:
    """The cart split by seller, with delivery options and the totals of the selection.

    ``selected_ids`` are the cart lines the user wants to buy now (all of them
    when None); the others stay in the cart. ``choices`` maps a seller id to
    the delivery method chosen for that group; a group without a choice gets
    its cheapest option. The totals are the ones the checkout will charge.
    """
    cart_items = sorted(get_store().get_cart_items(uid), key=lambda line: line.get("added_at") or 0)
    known_ids = {cart_item["cart_item_id"] for cart_item in cart_items}
    if selected_ids is not None:
        unknown = [line_id for line_id in selected_ids if line_id not in known_ids]
        if unknown:
            raise CartError(422, "some selected items are not in the cart")
    selected = set(known_ids if selected_ids is None else selected_ids)
    choices = choices or {}
    today = dt.datetime.fromtimestamp(checkout_service.now_ms() / 1000, tz=dt.timezone.utc).date()

    lines: list[dict[str, Any]] = []
    by_seller: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    item_count = 0
    for cart_item in cart_items:
        line, item = _line(uid, cart_item)
        line["selected"] = line["cart_item_id"] in selected and line["available"]
        lines.append(line)
        if line["available"]:
            item_count += line["quantity"]
            by_seller.setdefault(line["seller_id"], []).append((line, item))

    unknown_sellers = set(choices) - set(by_seller)
    if unknown_sellers:
        raise CartError(422, "a delivery choice refers to a seller that is not in the cart")

    groups = []
    for seller_id, entries in by_seller.items():
        first_line, first_item = entries[0]
        chosen = [(line, item) for line, item in entries if line["selected"]]
        subtotal = sum(line["price_cents"] for line, _ in chosen)
        offer = fulfilment.options(first_line["seller_kind"], [item for _, item in (chosen or entries)], subtotal, today)
        methods = {option["method"]: option for option in offer}
        method = choices.get(seller_id) or (offer[0]["method"] if offer else None)
        if method not in methods:
            raise CartError(422, f"{fulfilment.LABELS.get(method, method)} is not available for {first_line['seller_name']}")
        shipping = methods[method]["price_cents"] if chosen else 0
        groups.append({
            "seller_id": seller_id,
            "seller_name": first_line["seller_name"],
            "seller_kind": first_line["seller_kind"],
            "cart_item_ids": [line["cart_item_id"] for line, _ in entries],
            "selected_count": sum(line["quantity"] for line, _ in chosen),
            "options": offer,
            "method": method,
            "delivery": methods[method]["delivery"],
            "pickup_area": fulfilment.pickup_area(first_item) if method == fulfilment.PICKUP else None,
            "subtotal_cents": subtotal,
            "shipping_cents": shipping,
            "total_cents": subtotal + shipping,
        })
    # Official partners first, then community sellers, each by name.
    groups.sort(key=lambda group: (group["seller_kind"] != fulfilment.PARTNER, group["seller_name"].lower()))

    subtotal = sum(group["subtotal_cents"] for group in groups)
    shipping = sum(group["shipping_cents"] for group in groups)
    selected_count = sum(group["selected_count"] for group in groups)
    return {
        "items": lines,
        "item_count": item_count,  # for the cart badge: available items only
        "total_cents": sum(line["price_cents"] for line in lines if line["available"]),
        "currency": _currency(),
        "has_unavailable_items": any(not line["available"] for line in lines),
        "groups": groups,
        "summary": {
            "selected_item_count": selected_count,
            "subtotal_cents": subtotal,
            "shipping_cents": shipping,
            "total_cents": subtotal + shipping,
            "vat_included_cents": fulfilment.vat_included_cents(subtotal + shipping),
            "can_checkout": selected_count > 0 and subtotal + shipping >= checkout_service.MIN_CHARGE_CENTS,
        },
        "payment_methods": list(PAYMENT_METHODS),
    }


def get_cart(uid: str) -> dict[str, Any]:
    """Return the cart with current prices, availability and server-side totals."""
    return quote(uid)


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
