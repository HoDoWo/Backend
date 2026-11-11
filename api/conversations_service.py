"""Buyer-seller conversations and offers on second-hand listings, like Vinted.

* A buyer opens a conversation from a listing ("Message the seller"); there is
  one conversation per listing and buyer, and the seller answers from their inbox.
* In the conversation the buyer can make an offer. The seller accepts it,
  declines it or makes a counter-offer, which the buyer then accepts or declines.
* An offer waits for an answer for ``OFFER_PENDING_HOURS``; an accepted offer
  can be paid for ``OFFER_VALID_HOURS``: the buyer adds the listing to the cart
  and the cart and the checkout charge the offered price (``accepted_price``).
  After the payment the offer is used.
* Only listings of other users can be negotiated; partner products keep their
  fixed price.

Who is who stays on the server: the app sees "buying" / "selling", the other
person's display name and ``mine`` on each message, never a uid.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

import checkout_service
import conversations_store
import forum_service
import listings_store

BUYER = "buyer"
SELLER = "seller"
SYSTEM = "system"

PENDING = "pending"
ACCEPTED = "accepted"
DECLINED = "declined"
COUNTERED = "countered"
EXPIRED = "expired"
USED = "used"

OFFER_PENDING_HOURS = 24
OFFER_VALID_HOURS = 48
# An offer must be at least this share of the listing price, and below it.
MIN_OFFER_PERCENT = 50
MIN_OFFER_CENTS = 100
MAX_OFFERS = 10
TEXT_LIMITS = (1, 1000)
PREVIEW_LENGTH = 120
DEFAULT_PAGE = 100
HOUR_MS = 3_600_000


class ConversationError(Exception):
    """Request refused; carries the HTTP status and a client-safe detail."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def get_store():
    """Return the persistence backend (replaced by a fake in the tests)."""
    return conversations_store


def get_listings():
    """Return the listings backend (replaced by a fake in the tests)."""
    return listings_store


def _now() -> int:
    return checkout_service.now_ms()


def _uid(user: dict[str, Any]) -> str:
    return str(user.get("uid") or user.get("sub") or "")


def _clean(text: str | None) -> str:
    try:
        return forum_service.clean_text(text, TEXT_LIMITS, "message")
    except forum_service.ForumError as exc:
        raise ConversationError(exc.status_code, exc.detail) from exc


def _euros(cents: int) -> str:
    """"€80.00", as the app writes prices."""
    return f"€{cents / 100:.2f}"


def _image(listing: dict[str, Any]) -> str | None:
    images = listing.get("image_urls")
    return images[0] if isinstance(images, list) and images else listing.get("image_url")


def _other(role: str) -> str:
    return SELLER if role == BUYER else BUYER


# ---------- offers ----------

def offer_state(conversation: dict[str, Any], now: int) -> dict[str, Any] | None:
    """The current offer with its status as of ``now`` (pending offers expire, paid ones are used)."""
    offer = conversation.get("offer")
    if not isinstance(offer, dict):
        return None
    offer = dict(offer)
    if offer.get("status") == PENDING and now >= int(offer.get("expires_at") or 0):
        offer["status"] = EXPIRED
    elif offer.get("status") == ACCEPTED:
        if conversation.get("offer_used") == offer.get("offer_id"):
            offer["status"] = USED
        elif now >= int(offer.get("valid_until") or 0):
            offer["status"] = EXPIRED
    return offer


def accepted_price(uid: str, listing_id: str, list_price_cents: int) -> tuple[int, str] | None:
    """The price ``uid`` agreed with the seller of this listing, and the offer id, or None.

    Used by the cart and the checkout. When the seller has lowered the listing
    below the offer since, the listing price is cheaper and wins.
    """
    conversation = get_store().get_conversation(conversations_store.conversation_id(listing_id, uid))
    if conversation is None or conversation.get("buyer_uid") != uid:
        return None
    offer = offer_state(conversation, _now())
    if offer is None or offer["status"] != ACCEPTED:
        return None
    amount = int(offer["amount_cents"])
    return (amount, str(offer["offer_id"])) if amount < list_price_cents else None


def _public_offer(offer: dict[str, Any] | None, role: str) -> dict[str, Any] | None:
    if offer is None:
        return None
    mine = offer.get("from") == role
    return {
        "offer_id": offer.get("offer_id"),
        "amount_cents": offer.get("amount_cents"),
        "from": "me" if mine else "them",
        "status": offer["status"],
        "created_at": offer.get("created_at"),
        "expires_at": offer.get("expires_at"),
        "valid_until": offer.get("valid_until"),
        "can_answer": offer["status"] == PENDING and not mine,
        # The buyer pays an accepted offer from the cart.
        "can_pay": offer["status"] == ACCEPTED and role == BUYER,
    }


# ---------- reading ----------

def _role(conversation: dict[str, Any] | None, uid: str) -> str:
    """The user's side of the conversation; 404 for anyone else (it does not exist for them)."""
    if conversation is not None and uid:
        if conversation.get("buyer_uid") == uid:
            return BUYER
        if conversation.get("seller_uid") == uid:
            return SELLER
    raise ConversationError(404, "conversation not found")


def _public(conversation: dict[str, Any], role: str, now: int,
            listing: dict[str, Any] | None = None) -> dict[str, Any]:
    offer = offer_state(conversation, now)
    last = conversation.get("last_message") or None
    status = (listing or {}).get("status") or conversation.get("listing_status") or "active"
    price = (listing or {}).get("price_cents", conversation.get("listing_price_cents"))
    return {
        "id": conversation["id"],
        "role": "buying" if role == BUYER else "selling",
        "other_name": conversation.get(f"{_other(role)}_name") or "HoDoWo user",
        "listing": {
            "id": conversation.get("listing_id"),
            "title": conversation.get("listing_title") or "",
            "image_url": conversation.get("listing_image_url"),
            "price_cents": price,
            "status": status,
        },
        "last_message": {
            "text": last.get("text"),
            "kind": last.get("kind"),
            "mine": last.get("from") == role,
            "created_at": last.get("created_at"),
        } if last else None,
        "unread": int(conversation.get(f"unread_{role}") or 0),
        "offer": _public_offer(offer, role),
        # A new offer: the buyer when nothing is pending or agreed, the seller only as a counter-offer.
        "can_offer": status == "active" and _can_offer(offer, role) is None,
        "updated_at": conversation.get("updated_at"),
    }


def list_conversations(uid: str) -> dict[str, Any]:
    """The user's inbox, most recent activity first."""
    now = _now()
    rows = []
    for conversation in get_store().list_for_user(uid):
        role = _role(conversation, uid)
        rows.append(_public(conversation, role, now))
    rows.sort(key=lambda row: (-(row["updated_at"] or 0), row["id"]))
    return {"results": rows, "unread": sum(row["unread"] for row in rows)}


def _public_message(message: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "id": message["id"],
        "kind": message.get("kind") or "text",
        "text": message.get("text"),
        "amount_cents": message.get("amount_cents"),
        "mine": message.get("from") == role,
        "from": "system" if message.get("from") == SYSTEM else ("me" if message.get("from") == role else "them"),
        "created_at": message.get("created_at"),
    }


def get_conversation(uid: str, conv_id: str) -> dict[str, Any]:
    """The conversation with its newest messages; reading it marks it as read."""
    store = get_store()
    conversation = store.get_conversation(conv_id)
    role = _role(conversation, uid)
    if conversation.get(f"unread_{role}"):
        store.update_conversation(conv_id, {f"unread_{role}": 0})
        conversation[f"unread_{role}"] = 0
    listing = get_listings().get_listing(conversation.get("listing_id") or "")
    body = _public(conversation, role, _now(), listing or {"status": "removed"})
    body["messages"] = [_public_message(m, role) for m in store.list_messages(conv_id, DEFAULT_PAGE)]
    return body


# ---------- writing ----------

def _post(conversation: dict[str, Any], sender: str, kind: str, text: str,
          amount_cents: int | None = None, fields: dict[str, Any] | None = None) -> None:
    """Add a message and update the conversation's preview, time and unread counter."""
    now = _now()
    message = {"from": sender, "kind": kind, "text": text, "amount_cents": amount_cents, "created_at": now}
    changes = {
        "last_message": {"from": sender, "kind": kind, "text": text[:PREVIEW_LENGTH], "created_at": now},
        "updated_at": now,
        **(fields or {}),
    }
    if sender == SYSTEM:
        unread = None
    else:
        unread = _other(sender)
    get_store().add_message(conversation["id"], message, changes, unread)
    conversation.update(changes)
    if unread:
        conversation[f"unread_{unread}"] = int(conversation.get(f"unread_{unread}") or 0) + 1


def start_conversation(user: dict[str, Any], listing_id: str, text: str | None = None) -> dict[str, Any]:
    """Open (or reopen) the buyer's conversation about a listing, optionally with a first message."""
    uid = _uid(user)
    listing = get_listings().get_listing(listing_id)
    if listing is None or listing.get("status") != "active":
        raise ConversationError(404, "listing not found")
    if listing.get("owner_uid") == uid:
        raise ConversationError(422, "this is your listing")
    first = _clean(text) if text is not None else None

    store = get_store()
    conv_id = conversations_store.conversation_id(listing_id, uid)
    now = _now()
    store.create_conversation(conv_id, {
        "listing_id": listing_id,
        "listing_title": str(listing.get("title") or ""),
        "listing_image_url": _image(listing),
        "listing_price_cents": int(listing.get("price_cents") or 0),
        "buyer_uid": uid,
        "buyer_name": forum_service.author_name(user),
        "seller_uid": listing.get("owner_uid"),
        "seller_name": str(listing.get("seller_name") or "Community seller"),
        "unread_buyer": 0,
        "unread_seller": 0,
        "offers_made": 0,
        "offer": None,
        "last_message": None,
        "created_at": now,
        "updated_at": now,
    })
    if first:
        conversation = store.get_conversation(conv_id)
        _post(conversation, BUYER, "text", first)
    return get_conversation(uid, conv_id)


def send_message(uid: str, conv_id: str, text: str) -> dict[str, Any]:
    conversation = get_store().get_conversation(conv_id)
    role = _role(conversation, uid)
    _post(conversation, role, "text", _clean(text))
    return get_conversation(uid, conv_id)


def _can_offer(offer: dict[str, Any] | None, role: str) -> str | None:
    """Why the user cannot make an offer now, or None."""
    status = offer["status"] if offer else None
    if status == ACCEPTED:
        return "an offer is already accepted: pay it from the cart"
    if status == PENDING and offer.get("from") == role:
        return "wait for the answer to your offer"
    if role == SELLER and status != PENDING:
        return "you can only answer an offer of the buyer"
    return None


def make_offer(uid: str, conv_id: str, amount_cents: int) -> dict[str, Any]:
    """A buyer's offer, or the seller's counter-offer to a pending one."""
    store = get_store()
    conversation = store.get_conversation(conv_id)
    role = _role(conversation, uid)
    listing = get_listings().get_listing(conversation.get("listing_id") or "")
    if listing is None or listing.get("status") != "active":
        raise ConversationError(409, "the listing is no longer for sale")

    now = _now()
    current = offer_state(conversation, now)
    problem = _can_offer(current, role)
    if problem:
        raise ConversationError(409, problem)
    if int(conversation.get("offers_made") or 0) >= MAX_OFFERS:
        raise ConversationError(409, "too many offers in this conversation")

    price = int(listing.get("price_cents") or 0)
    lowest = max(MIN_OFFER_CENTS, math.ceil(price * MIN_OFFER_PERCENT / 100))
    if amount_cents >= price:
        raise ConversationError(422, f"an offer must be below the price ({_euros(price)})")
    if amount_cents < lowest:
        raise ConversationError(422, f"an offer must be at least {_euros(lowest)}")

    offer = {
        "offer_id": uuid.uuid4().hex[:16],
        "amount_cents": amount_cents,
        "from": role,
        "status": PENDING,
        "created_at": now,
        "expires_at": now + OFFER_PENDING_HOURS * HOUR_MS,
        "valid_until": None,
    }
    kind = "counter_offer" if current and current["status"] == PENDING else "offer"
    _post(conversation, role, kind, f"Offered {_euros(amount_cents)}", amount_cents, {
        "offer": offer,
        "offers_made": int(conversation.get("offers_made") or 0) + 1,
        "listing_price_cents": price,
    })
    return get_conversation(uid, conv_id)


def answer_offer(uid: str, conv_id: str, accept: bool) -> dict[str, Any]:
    """Accept or decline the other person's pending offer."""
    store = get_store()
    conversation = store.get_conversation(conv_id)
    role = _role(conversation, uid)
    now = _now()
    offer = offer_state(conversation, now)
    if offer is None or offer["status"] != PENDING:
        raise ConversationError(409, "there is no offer to answer")
    if offer.get("from") == role:
        raise ConversationError(403, "you cannot answer your own offer")
    if accept:
        listing = get_listings().get_listing(conversation.get("listing_id") or "")
        if listing is None or listing.get("status") != "active":
            raise ConversationError(409, "the listing is no longer for sale")

    stored = dict(conversation["offer"])
    amount = int(stored["amount_cents"])
    if accept:
        stored.update(status=ACCEPTED, answered_at=now, valid_until=now + OFFER_VALID_HOURS * HOUR_MS)
        text = f"Offer of {_euros(amount)} accepted"
    else:
        stored.update(status=DECLINED, answered_at=now)
        text = f"Offer of {_euros(amount)} declined"
    _post(conversation, role, "offer_accepted" if accept else "offer_declined", text, amount, {"offer": stored})
    return get_conversation(uid, conv_id)
