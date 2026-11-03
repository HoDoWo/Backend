"""Marketplace checkout, Stripe confirmation and order history (#43).

Flow:
    cart -> POST /checkout -> PaymentIntent (one per checkout)
         -> app PaymentSheet -> Stripe webhook -> orders "paid"

Prices and availability are always re-read from Firestore: the amount sent by
the app, or the price stored in the cart, is never trusted. A checkout is split
into one order per seller, all paid with the same PaymentIntent.

Seller payouts are out of scope in Sprint 2: every payment lands on the
platform's Stripe test account (Stripe Connect is planned for Sprint 3).
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Any

import config
import logging_config
import marketplace_store
import stripe_gateway

logger = logging_config.get_logger(__name__)

# Stripe refuses charges below this amount for EUR (0.50 €).
MIN_CHARGE_CENTS = 50
ORDER_HISTORY_LIMIT = 50
PUBLIC_ORDER_FIELDS = (
    "order_id",
    "checkout_id",
    "seller_id",
    "items",
    "total_cents",
    "currency",
    "status",
    "simulated",
    "created_at",
    "paid_at",
)


class CheckoutError(Exception):
    """Checkout refused; carries the HTTP status and a client-safe detail."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def get_store():
    """Return the persistence backend (replaced by a fake in the tests)."""
    return marketplace_store


def get_gateway():
    """Return the Stripe gateway (replaced by a fake in the tests)."""
    return stripe_gateway


def now_ms() -> int:
    return int(time.time() * 1000)


def public_order(order: dict[str, Any]) -> dict[str, Any]:
    """Drop internal fields (cart ids, PaymentIntent id) before returning an order."""
    return {field: order.get(field) for field in PUBLIC_ORDER_FIELDS}


def availability_problem(uid: str, product_type: str, item: dict[str, Any] | None, quantity: int) -> str | None:
    """Why ``uid`` cannot buy ``quantity`` of this catalogue item, or None if they can.

    Shared by the cart (#42) and the checkout, so both refuse the same things.
    """
    price = item.get("price_cents") if item else None
    if item is None or not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        return "not available"
    if product_type == "used":
        if item.get("status") != "active":
            return "already sold"
        if item["seller_id"] == uid:
            return "you cannot buy your own listing"
        # A listing can offer several identical items (the sell form has a quantity).
        # 0 means nothing left; a missing quantity means one item.
        available = 1 if item.get("quantity") is None else int(item["quantity"])
    else:
        available = int(item.get("stock") or 0)
    if available < quantity:
        return "not enough stock"
    return None


def _priced_lines(uid: str, cart_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve every cart line against the catalogue, or refuse the checkout."""
    store = get_store()
    lines: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []

    for cart_item in cart_items:
        product_type = cart_item.get("product_type")
        product_id = str(cart_item.get("product_id") or "")
        quantity = cart_item.get("quantity", 1)

        if product_type not in marketplace_store.PRODUCT_SOURCES or not product_id:
            problems.append({"product_id": product_id, "reason": "invalid cart item"})
            continue
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            problems.append({"product_id": product_id, "reason": "invalid quantity"})
            continue

        item = store.get_item(product_type, product_id)
        problem = availability_problem(uid, product_type, item, quantity)
        if problem:
            problems.append({"product_id": product_id, "reason": problem})
            continue
        price = item["price_cents"]

        lines.append(
            {
                "cart_item_id": cart_item["cart_item_id"],
                "seller_id": item["seller_id"],
                "product_type": product_type,
                "product_id": product_id,
                "title": str(item.get("title") or ""),
                "quantity": quantity,
                "unit_price_cents": price,
                "price_cents": price * quantity,
            }
        )

    if problems:
        raise CheckoutError(409, {"message": "some cart items cannot be bought", "items": problems})
    return lines


def _build_orders(uid: str, checkout_id: str, lines: list[dict[str, Any]], currency: str, simulated: bool):
    by_seller: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        by_seller[line["seller_id"]].append(line)

    created_at = now_ms()
    orders = []
    for seller_id, seller_lines in sorted(by_seller.items()):
        orders.append(
            {
                "order_id": uuid.uuid4().hex,
                "checkout_id": checkout_id,
                "uid": uid,
                "seller_id": seller_id,
                "items": [
                    {key: line[key] for key in ("product_type", "product_id", "title", "quantity", "unit_price_cents", "price_cents")}
                    for line in seller_lines
                ],
                "cart_item_ids": [line["cart_item_id"] for line in seller_lines],
                "total_cents": sum(line["price_cents"] for line in seller_lines),
                "currency": currency,
                "status": "pending",
                "simulated": simulated,
                "payment_intent_id": None,
                "created_at": created_at,
                "paid_at": None,
            }
        )
    return orders


def _mark_paid(orders: list[dict[str, Any]], paid_at: int) -> None:
    store = get_store()
    for order in orders:
        if order.get("status") == "paid":
            continue
        # One atomic write: the order is never paid without its side effects.
        store.fulfil_order(order, paid_at)
        order.update(status="paid", paid_at=paid_at)


def checkout(uid: str) -> dict[str, Any]:
    """Turn the user's cart into pending orders and a PaymentIntent."""
    settings = config.get_stripe_settings()
    store = get_store()

    cart_items = store.get_cart_items(uid)
    if not cart_items:
        raise CheckoutError(400, "the cart is empty")

    lines = _priced_lines(uid, cart_items)
    total_cents = sum(line["price_cents"] for line in lines)
    if total_cents < MIN_CHARGE_CENTS:
        raise CheckoutError(422, f"the total must be at least {MIN_CHARGE_CENTS} cents")

    checkout_id = uuid.uuid4().hex
    orders = _build_orders(uid, checkout_id, lines, settings.currency, simulated=not settings.enabled)
    for order in orders:
        store.save_order(order["order_id"], order)

    response = {
        "checkout_id": checkout_id,
        "order_ids": [order["order_id"] for order in orders],
        "total_cents": total_cents,
        "currency": settings.currency,
        "simulated": not settings.enabled,
        "client_secret": None,
    }

    if not settings.enabled:
        # No Stripe keys (local dev, CI): record the purchase as simulated.
        _mark_paid(orders, now_ms())
        logger.info("Simulated checkout %s for %d order(s)", checkout_id, len(orders))
        return response

    try:
        intent = get_gateway().create_payment_intent(
            secret_key=settings.secret_key,
            amount_cents=total_cents,
            currency=settings.currency,
            metadata={"checkout_id": checkout_id, "uid": uid},
            idempotency_key=f"checkout-{checkout_id}",
        )
    except stripe_gateway.PaymentGatewayError as exc:
        logger.error("PaymentIntent creation failed for checkout %s: %s", checkout_id, exc)
        for order in orders:
            store.update_order(order["order_id"], {"status": "cancelled"})
        raise CheckoutError(502, "the payment provider is unavailable, try again") from exc

    for order in orders:
        store.update_order(order["order_id"], {"payment_intent_id": intent["id"]})
    response["client_secret"] = intent["client_secret"]
    return response


def handle_webhook(payload: bytes, signature_header: str | None) -> dict[str, Any]:
    """Verify a Stripe webhook and update the orders of its checkout."""
    settings = config.get_stripe_settings()
    if not settings.webhook_secret:
        raise CheckoutError(503, "payments are not configured")

    try:
        event = get_gateway().parse_webhook(payload, signature_header, settings.webhook_secret)
    except stripe_gateway.InvalidWebhookError as exc:
        logger.warning("Rejected Stripe webhook: %s", exc)
        raise CheckoutError(400, "invalid webhook") from exc

    event_type = event.get("type")
    handlers = {
        "payment_intent.succeeded": "paid",
        "payment_intent.payment_failed": "failed",
        "payment_intent.canceled": "cancelled",
    }
    if event_type not in handlers:
        return {"received": True, "handled": False}

    store = get_store()
    event_id = str(event["id"])
    if not store.claim_event(event_id, event_type, now_ms()):
        return {"received": True, "handled": False, "duplicate": True}

    try:
        _apply_payment_event(event, handlers[event_type])
    except Exception:
        # Let Stripe retry the event instead of losing it.
        store.release_event(event_id)
        raise
    return {"received": True, "handled": True}


def _apply_payment_event(event: dict[str, Any], new_status: str) -> None:
    intent = event["data"]["object"]
    checkout_id = (intent.get("metadata") or {}).get("checkout_id")
    if not checkout_id:
        logger.warning("Stripe event %s has no checkout_id, ignored", event["id"])
        return

    orders = [
        order
        for order in get_store().get_orders_by_checkout(checkout_id)
        if order.get("payment_intent_id") == intent["id"]
    ]
    if not orders:
        logger.warning("No orders for checkout %s (event %s)", checkout_id, event["id"])
        return

    if new_status != "paid":
        for order in orders:
            # A failed attempt can still be retried in the PaymentSheet, and a
            # paid order never goes back.
            if order.get("status") in ("pending", "failed"):
                get_store().update_order(order["order_id"], {"status": new_status})
        return

    expected = sum(order["total_cents"] for order in orders)
    received = intent.get("amount_received", intent.get("amount"))
    if received != expected or str(intent.get("currency", "")).lower() != orders[0]["currency"]:
        logger.error(
            "Amount mismatch for checkout %s: expected %s %s, got %s %s",
            checkout_id, expected, orders[0]["currency"], received, intent.get("currency"),
        )
        return

    _mark_paid(orders, now_ms())


def list_orders(uid: str) -> list[dict[str, Any]]:
    return [public_order(order) for order in get_store().list_user_orders(uid, ORDER_HISTORY_LIMIT)]


def get_order(uid: str, order_id: str) -> dict[str, Any]:
    order = get_store().get_order(order_id)
    if order is None:
        raise CheckoutError(404, "order not found")
    if order.get("uid") != uid:
        raise CheckoutError(403, "this order belongs to another user")
    return public_order(order)
