"""Second-hand listings (#35): browse, read, edit and mark as sold, owner only.

Creating and deleting a listing stay in ``firebase_store`` (POST and DELETE
``/api/marketplace/listings``); this module adds the rest of the CRUD that the
app's "My listings" screen (app #146) calls.

* Browsing shows active listings to everyone; ``mine=true`` shows all the
  user's listings, sold ones included.
* Only the owner reads a sold listing, edits a listing or marks it as sold:
  anyone else gets 403 (404 when the listing does not exist).
* A sold listing cannot be edited any more: the buyer saw that version.
* Responses never contain ``owner_uid``: only ``is_mine``.
"""

from __future__ import annotations

from typing import Any

import checkout_service
import firebase_store
import listings_store

ACTIVE = "active"
SOLD = "sold"
DEFAULT_PAGE = 20
MAX_PAGE = 50

CONDITIONS = ("unopened", "like-new", "used-good", "parts-only", "refurbished")
# Labels the app shows and may send back; both forms are accepted.
CONDITION_LABELS = {
    "unopened": "Unopened",
    "like-new": "Like new",
    "used-good": "Used, good",
    "parts-only": "For parts",
    "refurbished": "Refurbished",
}
_LABEL_KEYS = {
    **{label.lower(): key for key, label in CONDITION_LABELS.items()},
    "used good": "used-good",
    "parts only": "parts-only",
    "like new": "like-new",
}

EDITABLE_FIELDS = (
    "title", "description", "category", "brand", "model", "part_number", "listing_kind", "condition",
    "price", "image_urls", "handover_methods", "pickup_zip", "shipping_price_cents", "quantity",
)


class ListingError(Exception):
    """Request refused; carries the HTTP status and a client-safe detail."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def field_error(field: str, message: str) -> ListingError:
    """422 in the same shape as FastAPI's own validation errors, so the app reads one format."""
    return ListingError(422, [{"loc": ["body", field], "msg": message, "type": "value_error"}])


def get_store():
    """Return the persistence backend (replaced by a fake in the tests)."""
    return listings_store


def condition_key(value: str) -> str:
    """'like-new', 'Like new' or 'LIKE NEW' -> 'like-new'."""
    text = " ".join(str(value or "").replace(",", " ").split()).lower()
    key = text.replace(" ", "-") if text.replace(" ", "-") in CONDITIONS else _LABEL_KEYS.get(text)
    if key is None:
        raise field_error("condition", f"condition must be one of: {', '.join(CONDITIONS)}")
    return key


def posted_ago(created_at: Any, now: int) -> str:
    """'just now', '5m ago', '3h ago', '2d ago', '4w ago' for the "My listings" rows."""
    if not isinstance(created_at, (int, float)):
        return ""
    minutes = max(now - int(created_at), 0) // 60_000
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 60 * 24:
        return f"{minutes // 60}h ago"
    if minutes < 60 * 24 * 7:
        return f"{minutes // (60 * 24)}d ago"
    return f"{minutes // (60 * 24 * 7)}w ago"


def public(listing: dict[str, Any], uid: str, now: int | None = None) -> dict[str, Any]:
    """The listing as the app sees it, without the owner's uid."""
    cents = int(listing.get("price_cents") or 0)
    condition = listing.get("condition") or ""
    return {
        "id": listing["id"],
        "type": "used",
        "title": listing.get("title") or "",
        "description": listing.get("description") or "",
        "category": listing.get("category") or "",
        "brand": listing.get("brand"),
        "model": listing.get("model"),
        "part_number": listing.get("part_number"),
        "listing_kind": listing.get("listing_kind") or "appliance",
        "condition": condition,
        "condition_label": CONDITION_LABELS.get(condition, condition),
        "price": cents / 100,
        "price_cents": cents,
        "quantity": int(listing.get("quantity") or 1),
        "image_urls": [url for url in listing.get("image_urls") or [] if isinstance(url, str)],
        "handover_methods": list(listing.get("handover_methods") or []),
        "pickup_zip": listing.get("pickup_zip"),
        "shipping_price_cents": listing.get("shipping_price_cents"),
        "seller_name": listing.get("seller_name") or "Community seller",
        "status": listing.get("status") or ACTIVE,
        "created_at": listing.get("created_at"),
        "updated_at": listing.get("updated_at"),
        "sold_at": listing.get("sold_at"),
        "posted_ago": posted_ago(listing.get("created_at"), now or checkout_service.now_ms()),
        # No buyer-seller messages yet: the field is there for the app's row.
        "inquiry_count": 0,
        "is_mine": listing.get("owner_uid") == uid,
    }


# ---------- reading ----------

def list_listings(uid: str, q: str = "", category: str | None = None, max_price: float | None = None,
                  status: str | None = None, mine: bool = False, cursor: str | None = None,
                  limit: int = DEFAULT_PAGE) -> dict[str, Any]:
    """One page of listings, newest first; send ``next_cursor`` back as ``cursor``."""
    if status not in (None, ACTIVE, SOLD):
        raise ListingError(422, "status must be active or sold")
    if status == SOLD and not mine:
        # Other people's sold listings are not shown to anyone.
        raise ListingError(422, "status=sold is only available with mine=true")

    store = get_store()
    rows = store.list_by_owner(uid) if mine else store.list_by_status(ACTIVE)
    needle = firebase_store._title_key(q)
    result = []
    for listing in rows:
        if status and (listing.get("status") or ACTIVE) != status:
            continue
        if category and str(listing.get("category") or "").lower() != category.lower():
            continue
        if max_price is not None and int(listing.get("price_cents") or 0) > round(max_price * 100):
            continue
        if needle:
            haystack = firebase_store._title_key(" ".join(
                str(listing.get(field) or "") for field in ("title", "brand", "model", "category", "description")
            ))
            if needle not in haystack:
                continue
        result.append(listing)

    result.sort(key=lambda listing: (-int(listing.get("created_at") or 0), listing["id"]))
    ids = [listing["id"] for listing in result]
    if cursor is not None and cursor not in ids:
        raise ListingError(422, "invalid cursor")
    start = ids.index(cursor) + 1 if cursor else 0
    page = result[start:start + min(max(limit, 1), MAX_PAGE)]
    more = start + len(page) < len(result)
    now = checkout_service.now_ms()
    return {
        "results": [public(listing, uid, now) for listing in page],
        "next_cursor": page[-1]["id"] if more and page else None,
    }


def get_listing(uid: str, listing_id: str) -> dict[str, Any]:
    listing = get_store().get_listing(listing_id)
    # A sold listing is only visible to its owner: for anyone else it is gone.
    if listing is None or (listing.get("status") != ACTIVE and listing.get("owner_uid") != uid):
        raise ListingError(404, "listing not found")
    return public(listing, uid)


# ---------- writing ----------

def _own_listing(uid: str, listing_id: str) -> dict[str, Any]:
    listing = get_store().get_listing(listing_id)
    if listing is None:
        raise ListingError(404, "listing not found")
    if listing.get("owner_uid") != uid:
        raise ListingError(403, "listing is not owned by the current user")
    return listing


def update_listing(uid: str, listing_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    listing = _own_listing(uid, listing_id)
    if listing.get("status") == SOLD:
        raise ListingError(409, "a sold listing cannot be edited")

    changes = {key: value for key, value in changes.items() if key in EDITABLE_FIELDS}
    if not changes:
        raise ListingError(422, "nothing to change")

    fields: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "price":
            fields["price_cents"] = round(float(value) * 100)
        elif key == "condition":
            fields["condition"] = condition_key(value)
        elif key == "image_urls":
            if any(not str(url).startswith("https://") for url in value):
                raise field_error("image_urls", "every image URL must start with https://")
            fields[key] = list(value)
        elif isinstance(value, str):
            fields[key] = value.strip()
        else:
            fields[key] = value
    if "title" in fields:
        if len(fields["title"]) < 3:
            raise field_error("title", "the title must be at least 3 characters")
        fields["title_normalised"] = firebase_store._title_key(fields["title"])
    if "description" in fields and len(fields["description"]) < 30:
        raise field_error("description", "the description must be at least 30 characters")
    fields["updated_at"] = checkout_service.now_ms()

    get_store().update_listing(listing_id, fields)
    return public({**listing, **fields}, uid)


def mark_sold(uid: str, listing_id: str) -> dict[str, Any]:
    """The seller sold it outside the app (or in person): it leaves the marketplace."""
    listing = _own_listing(uid, listing_id)
    if listing.get("status") == SOLD:
        return public(listing, uid)
    now = checkout_service.now_ms()
    fields = {"status": SOLD, "sold_at": now, "updated_at": now}
    get_store().update_listing(listing_id, fields)
    return public({**listing, **fields}, uid)
