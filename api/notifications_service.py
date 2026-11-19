"""The user's notifications (bell of the app), worked out when they are asked for.

Nothing is stored: every call reads what changed for the user.

* ``message``: an unread message in one of the user's conversations;
* ``offer``: an offer (or counter-offer) waiting for the user's answer, for
  example on a product the user is selling;
* ``offer_accepted`` / ``offer_declined``: the other person answered the user's offer;
* ``price_drop`` / ``price_change``: a product in the user's cart now costs
  something different from when it was added;
* ``sale``: a second-hand listing of the user was bought and paid (for a week);
* ``sponsored``: one product of a sponsored partner, a different one each day.

Each source is read on its own: if one fails (no cart yet, Stripe or the
catalogue not configured), the others are still returned. ``id`` changes when
the notification changes, so the app can tell a new one from one already seen.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import cart_service
import checkout_service
import conversations_service
import fulfilment
import logging_config
import partner_products

logger = logging_config.get_logger(__name__)

MAX_BODY = 120
OFFER_KINDS = ("offer", "counter_offer")


def _euros(cents: int | None) -> str:
    return f"€{(cents or 0) / 100:.2f}"


def _short(text: str | None) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= MAX_BODY else clean[: MAX_BODY - 1].rstrip() + "…"


def _conversation_item(conversation: dict[str, Any]) -> dict[str, Any] | None:
    last = conversation.get("last_message")
    if not conversation.get("unread") or not last or last.get("mine"):
        return None
    listing = conversation.get("listing") or {}
    offer = conversation.get("offer") or {}
    other = conversation.get("other_name") or "HoDoWo user"
    title_of_listing = listing.get("title") or "your listing"
    kind = last.get("kind") or "text"
    if kind in OFFER_KINDS and offer.get("can_answer"):
        selling = conversation.get("role") == "selling"
        item = ("offer", f"New offer: {_euros(offer.get('amount_cents'))}",
                f"{other} {'wants to buy' if selling else 'answered with'} {title_of_listing}")
    elif kind == "offer_accepted":
        item = ("offer_accepted", "Offer accepted", f"{other} accepted your offer for {title_of_listing}")
    elif kind == "offer_declined":
        item = ("offer_declined", "Offer declined", f"{other} declined your offer for {title_of_listing}")
    else:
        item = ("message", f"{other} · {title_of_listing}", _short(last.get("text")))
    notification_kind, title, body = item
    return {
        "id": f"conversation:{conversation['id']}:{last.get('created_at') or 0}",
        "kind": notification_kind,
        "title": title,
        "body": body,
        "image_url": listing.get("image_url"),
        "created_at": last.get("created_at"),
        "unread": True,
        "target": {"type": "conversation", "id": conversation["id"]},
    }


def _conversations(uid: str) -> list[dict[str, Any]]:
    rows = conversations_service.list_conversations(uid).get("results") or []
    return [item for item in map(_conversation_item, rows) if item]


def _price_changes(uid: str, now: int) -> list[dict[str, Any]]:
    items = []
    for line in cart_service.get_cart(uid).get("items") or []:
        if not line.get("available") or not line.get("price_changed"):
            continue
        now_cents, was_cents = line.get("unit_price_cents"), line.get("added_price_cents")
        if not now_cents or not was_cents:
            continue
        drop = now_cents < was_cents
        items.append({
            "id": f"price:{line['cart_item_id']}:{now_cents}",
            "kind": "price_drop" if drop else "price_change",
            "title": "Price drop in your cart" if drop else "Price change in your cart",
            "body": f"{line.get('title') or 'A product'} now costs {_euros(now_cents)} (was {_euros(was_cents)})",
            "image_url": line.get("image_url"),
            "created_at": now,
            "unread": True,
            "target": {"type": "cart"},
        })
    return items


def _sales(uid: str, now: int) -> list[dict[str, Any]]:
    items = []
    for order in checkout_service.list_recent_sales(uid, now):
        titles = [item.get("title") or "your item" for item in order.get("items") or []] or ["your item"]
        what = titles[0] if len(titles) == 1 else f"{titles[0]} and {len(titles) - 1} more"
        how = (", the buyer picks it up" if order.get("shipping_method") == fulfilment.PICKUP
               else ", to ship" if order.get("shipping_method") else "")
        items.append({
            "id": f"sale:{order['order_id']}",
            "kind": "sale",
            "title": f"Sold: {_short(what)}",
            "body": f"Paid {_euros(order.get('total_cents'))}{how}",
            "image_url": None,
            "created_at": order.get("paid_at"),
            "unread": True,
            "target": {"type": "my_listings"},
        })
    return items


def _sponsored(user: dict[str, Any], now: int) -> list[dict[str, Any]]:
    products = [
        product for product in partner_products.list_products(user, limit=partner_products.MAX_PAGE)["items"]
        if product.get("sponsored")
    ]
    if not products:
        return []
    day = dt.datetime.fromtimestamp(now / 1000, tz=dt.timezone.utc).date().toordinal()
    product = products[day % len(products)]
    partner = product.get("partner_name") or "a HoDoWo partner"
    return [{
        "id": f"sponsored:{product['id']}",
        "kind": "sponsored",
        "title": "Sponsored",
        "body": f"{product.get('title') or 'New product'} from {partner}, {_euros(product.get('price_cents'))}",
        "image_url": product.get("image_url"),
        "created_at": now,
        "unread": False,
        "target": {"type": "product", "id": product["id"], "product": product},
    }]


def _safely(source: str, read, *args) -> list[dict[str, Any]]:
    try:
        return read(*args)
    except Exception as exc:  # noqa: BLE001 - one source must not hide the others
        logger.warning("Notifications from %s are not available: %s", source, exc)
        return []


def list_notifications(user: dict[str, Any]) -> dict[str, Any]:
    """The user's notifications, newest first, with the sponsored product last."""
    uid = user["uid"]
    now = checkout_service.now_ms()
    items = (_safely("conversations", _conversations, uid) + _safely("cart", _price_changes, uid, now)
             + _safely("sales", _sales, uid, now))
    items.sort(key=lambda item: -(item.get("created_at") or 0))
    items += _safely("sponsored products", _sponsored, user, now)
    return {"items": items, "unread": sum(1 for item in items if item["unread"])}
