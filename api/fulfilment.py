"""Delivery options of the cart: one group per seller, each with its shipping method.

The cart is split by seller, as the orders are (one order per seller). For each
group the buyer chooses how to receive it:

    official partners and HoDoWo parts:  standard or express shipping
    community sellers (listings):        local pickup and/or shipping, as the
                                         seller allowed in the listing

Shipping is simulated (no carrier is booked), but its price is part of the
total the backend charges, so it is computed here and never sent by the app.
Prices include VAT; the summary shows how much of the total is VAT.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import marketplace_store

STANDARD = "standard"
EXPRESS = "express"
PICKUP = "pickup"
METHODS = (STANDARD, EXPRESS, PICKUP)

STANDARD_CENTS = 490
EXPRESS_CENTS = 990
# Standard shipping is free from this subtotal per seller.
FREE_STANDARD_FROM_CENTS = 5000
VAT_RATE_PERCENT = 21

PARTNER = "partner"
COMMUNITY = "community"

# Business days from today, (fastest, slowest).
DELIVERY_DAYS = {STANDARD: (3, 5), EXPRESS: (1, 2)}
LABELS = {
    STANDARD: "Standard shipping",
    EXPRESS: "Express shipping",
    PICKUP: "Local pickup",
}


def seller_kind(product_type: str) -> str:
    return COMMUNITY if product_type == "used" else PARTNER


def seller_name(product_type: str, item: dict[str, Any]) -> str:
    if product_type == "used":
        return str(item.get("seller_name") or "Community seller")
    if item.get("seller_id") == marketplace_store.PLATFORM_SELLER_ID:
        return "HoDoWo"
    return str(item.get("partner_name") or item.get("seller_id") or "Official partner")


def item_subtitle(product_type: str, item: dict[str, Any]) -> str:
    """Short second line of the cart row: model and part number, or the condition."""
    if product_type == "used":
        condition = str(item.get("condition") or "").replace("-", " ")
        return condition[:1].upper() + condition[1:] if condition else "Second-hand"
    parts = [str(item.get(field)) for field in ("brand", "model") if item.get(field)]
    number = item.get("part_number") or item.get("sku")
    if number:
        parts.append(f"#{number}")
    return " · ".join(parts)


def pickup_area(item: dict[str, Any]) -> str | None:
    """Approximate pickup area before paying: the first digits of the postcode only."""
    zip_code = str(item.get("pickup_zip") or "").strip()
    return f"{zip_code[:3]}xx" if len(zip_code) >= 3 else None


def _add_business_days(start: dt.date, days: int) -> dt.date:
    current = start
    while days > 0:
        current += dt.timedelta(days=1)
        if current.weekday() < 5:
            days -= 1
    return current


def delivery_window(method: str, today: dt.date) -> dict[str, str] | None:
    if method not in DELIVERY_DAYS:
        return None
    fastest, slowest = DELIVERY_DAYS[method]
    return {
        "from": _add_business_days(today, fastest).isoformat(),
        "to": _add_business_days(today, slowest).isoformat(),
    }


def _community_shipping_cents(items: list[dict[str, Any]]) -> int:
    """A seller who ships sets the price in the listing; the dearest one applies."""
    prices = [item.get("shipping_price_cents") for item in items]
    prices = [price for price in prices if isinstance(price, int) and not isinstance(price, bool) and price >= 0]
    return max(prices) if prices else STANDARD_CENTS


def options(kind: str, items: list[dict[str, Any]], subtotal_cents: int, today: dt.date) -> list[dict[str, Any]]:
    """The ways this group can be delivered, cheapest first."""
    result: list[dict[str, Any]] = []
    if kind == PARTNER:
        standard = 0 if subtotal_cents >= FREE_STANDARD_FROM_CENTS else STANDARD_CENTS
        result.append({"method": STANDARD, "price_cents": standard})
        result.append({"method": EXPRESS, "price_cents": EXPRESS_CENTS})
    else:
        # Every listing of the group must allow the method (missing = pickup only).
        allowed = [set(item.get("handover_methods") or [PICKUP]) for item in items]
        if all(PICKUP in methods for methods in allowed):
            result.append({"method": PICKUP, "price_cents": 0})
        if all("shipping" in methods for methods in allowed):
            result.append({"method": STANDARD, "price_cents": _community_shipping_cents(items)})
    for option in result:
        option["label"] = LABELS[option["method"]]
        option["delivery"] = delivery_window(option["method"], today)
    return sorted(result, key=lambda option: option["price_cents"])


def vat_included_cents(total_cents: int) -> int:
    """VAT contained in a VAT-inclusive total (21 %), for information only."""
    return round(total_cents * VAT_RATE_PERCENT / (100 + VAT_RATE_PERCENT))
