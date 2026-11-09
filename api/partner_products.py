"""Official partner product API (#39): catalogue reads and partner-only writes.

Official vendors sell new products. In Sprint 2 they have no dashboard, so
they write through this API; the Sprint 3 dashboard will call the same
endpoints, so it needs no redesign.

Who can write:

* a partner account (``role: "partner"``, ``partner_id``) creates and edits
  the products of its own partner only;
* an admin can do it for any partner, and is the only one who can set
  ``sponsored`` (paid placement);
* everyone else gets 403.

``partner_tier`` and ``partner_name`` always come from the ``partners``
document, never from the request, and the request models refuse unknown
fields, so a client cannot raise its own ranking.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import catalogue_import
import firebase_store
import partner_store
import roles

# Same categories as the curated catalogue (#33), lower case.
CATEGORIES = (
    "washing machine",
    "dryer",
    "dishwasher",
    "refrigerator",
    "oven",
    "hob",
    "microwave",
    "coffee machine",
)

DEFAULT_PAGE = 20
MAX_PAGE = 50

EDITABLE_FIELDS = ("title", "brand", "model", "category", "description", "price", "stock",
                   "image_url", "listing_kind", "sponsored")


class ProductError(Exception):
    """Request refused; carries the HTTP status and a client-safe detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def get_store():
    """Return the persistence backend (replaced by a fake in the tests)."""
    return partner_store


def product_id(partner_id: str, sku: str) -> str:
    return f"{partner_id}_{sku}"


def _cents(price: Decimal) -> int:
    return int(price * 100)


def _public(product: dict[str, Any]) -> dict[str, Any]:
    """What the app sees, with the fields it uses to label results."""
    cents = int(product.get("price_cents") or 0)
    stock = int(product.get("stock") or 0)
    return {
        "id": product["id"],
        "type": "new",
        "sku": product.get("sku"),
        "partner_id": product.get("partner_id"),
        "partner_name": product.get("partner_name"),
        "partner_tier": int(product.get("partner_tier") or 0),
        "sponsored": bool(product.get("sponsored", False)),
        "title": product.get("title") or "",
        "brand": product.get("brand"),
        "model": product.get("model"),
        "category": product.get("category") or "",
        "listing_kind": product.get("listing_kind") or "appliance",
        "description": product.get("description") or "",
        "price": cents / 100,
        "price_cents": cents,
        "stock": stock,
        "in_stock": stock > 0,
        "image_url": product.get("image_url"),
        "created_at": product.get("created_at"),
        "updated_at": product.get("updated_at"),
    }


# ---------- who may do what ----------

def _manages(user: dict[str, Any], partner_id: str | None) -> bool:
    return roles.is_admin(user) or (partner_id is not None and roles.partner_id_of(user) == partner_id)


def _writer_partner(user: dict[str, Any], requested: str | None) -> str:
    """The partner a create acts for: the admin's choice, or the caller's own."""
    if roles.is_admin(user):
        if not requested:
            raise ProductError(422, "an admin must say which partner_id the product belongs to")
        return requested
    own = roles.partner_id_of(user)
    if own is None:
        raise ProductError(403, "only partners can manage products")
    if requested and requested != own:
        raise ProductError(403, "a partner can only manage its own products")
    return own


def _active_partner(partner_id: str) -> dict[str, Any]:
    partner = get_store().get_partner(partner_id)
    if partner is None:
        raise ProductError(404, "partner not found")
    if not partner.get("active", True):
        raise ProductError(403, "this partner is not active")
    return partner


def _check_sponsored(user: dict[str, Any], requested: bool | None, current: bool = False) -> None:
    """Paid placement: only an admin turns it on or off; repeating the current value is harmless."""
    if requested is not None and requested != current and not roles.is_admin(user):
        raise ProductError(403, "only an admin can change sponsored")


def _check_category(category: str | None) -> None:
    if category is not None and category not in CATEGORIES:
        raise ProductError(422, f"category must be one of: {', '.join(CATEGORIES)}")


def _check_image(url: str | None) -> None:
    if url and not url.startswith("https://"):
        raise ProductError(422, "image_url must start with https://")


# ---------- reading ----------

def list_products(user: dict[str, Any], q: str = "", brand: str | None = None, category: str | None = None,
                  partner: str | None = None, cursor: str | None = None,
                  limit: int = DEFAULT_PAGE) -> dict[str, Any]:
    """The catalogue: in-stock products of active partners, sponsored and higher tiers first.

    A partner filtering on itself (and an admin) also sees its products that
    are out of stock, to restock them.
    """
    _check_category(category)
    store = get_store()
    partners = store.list_partners()
    sees_all = _manages(user, partner)
    needle = firebase_store._title_key(q or "")

    items = []
    for product in store.list_products():
        owner = product.get("partner_id")
        if partner and owner != partner:
            continue
        if not sees_all and (int(product.get("stock") or 0) <= 0 or not partners.get(owner, {}).get("active", True)):
            continue
        if brand and str(product.get("brand") or "").lower() != brand.lower():
            continue
        if category and product.get("category") != category:
            continue
        if needle:
            haystack = firebase_store._title_key(" ".join(
                str(product.get(field) or "")
                for field in ("title", "brand", "model", "sku", "category", "description")
            ))
            if needle not in haystack:
                continue
        items.append(product)

    items.sort(key=lambda p: (
        -int(bool(p.get("sponsored"))),
        -int(p.get("partner_tier") or 0),
        -int(p.get("created_at") or 0),
        p["id"],
    ))
    ids = [p["id"] for p in items]
    if cursor is not None and cursor not in ids:
        raise ProductError(422, "invalid cursor")
    start = ids.index(cursor) + 1 if cursor else 0
    limit = min(max(limit, 1), MAX_PAGE)
    page = items[start:start + limit]
    more = start + limit < len(items)
    return {"items": [_public(p) for p in page], "next_cursor": page[-1]["id"] if more and page else None}


def get_product(user: dict[str, Any], product_id_: str) -> dict[str, Any]:
    product = get_store().get_product(product_id_)
    if product is None:
        raise ProductError(404, "product not found")
    return _public(product)


# ---------- writing ----------

def create_product(user: dict[str, Any], uid: str, payload: dict[str, Any]) -> dict[str, Any]:
    partner_id = _writer_partner(user, payload.get("partner_id"))
    _check_sponsored(user, payload.get("sponsored"))
    _check_category(payload["category"])
    _check_image(payload.get("image_url"))
    partner = _active_partner(partner_id)

    sku = payload["sku"].strip()
    store = get_store()
    # SKUs are unique per partner, whatever their case: "ab-1" and "AB-1" are one product.
    taken = {str(p.get("sku") or "").lower() for p in store.list_products() if p.get("partner_id") == partner_id}
    if sku.lower() in taken:
        raise ProductError(409, f"SKU {sku} already exists for this partner")

    now = catalogue_import.now_ms()
    title = payload["title"].strip()
    data = {
        "partner_id": partner_id,
        "partner_name": partner.get("name") or partner_id,
        "partner_tier": int(partner.get("partner_tier") or 0),
        "sku": sku,
        "title": title,
        "title_normalised": firebase_store._title_key(title),
        "brand": payload["brand"].strip(),
        "model": payload["model"].strip(),
        "category": payload["category"],
        "description": (payload.get("description") or "").strip(),
        "price_cents": _cents(payload["price"]),
        "stock": payload["stock"],
        "image_url": payload.get("image_url"),
        "condition": "new",
        "item_type": "new",
        "listing_kind": payload.get("listing_kind") or "appliance",
        "sponsored": bool(payload.get("sponsored", False)),
        "created_by": uid,
        "created_at": now,
        "updated_at": now,
    }
    new_id = product_id(partner_id, sku)
    if not store.create_product(new_id, data):
        raise ProductError(409, f"SKU {sku} already exists for this partner")
    return _public({**data, "id": new_id})


def update_product(user: dict[str, Any], uid: str, product_id_: str, changes: dict[str, Any]) -> dict[str, Any]:
    store = get_store()
    product = store.get_product(product_id_)
    if product is None:
        raise ProductError(404, "product not found")
    if not _manages(user, product.get("partner_id")):
        # Same answer for a regular user and for another partner.
        raise ProductError(403, "a partner can only manage its own products")

    changes = {key: value for key, value in changes.items() if key in EDITABLE_FIELDS}
    if not changes:
        raise ProductError(422, "nothing to change")
    _check_sponsored(user, changes.get("sponsored"), bool(product.get("sponsored", False)))
    _check_category(changes.get("category"))
    _check_image(changes.get("image_url"))
    partner = _active_partner(product["partner_id"])

    fields: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "price":
            fields["price_cents"] = _cents(value)
        elif isinstance(value, str) and key != "category":
            fields[key] = value.strip()
        else:
            fields[key] = value
    if "title" in fields:
        fields["title_normalised"] = firebase_store._title_key(fields["title"])
    # Name and tier always follow the partners document.
    fields["partner_name"] = partner.get("name") or product["partner_id"]
    fields["partner_tier"] = int(partner.get("partner_tier") or 0)
    fields["updated_at"] = catalogue_import.now_ms()
    fields["updated_by"] = uid

    store.update_product(product_id_, fields)
    return _public({**product, **fields})
