"""Appliance forum (#44): discussions by appliance category, also shown on product pages.

Every thread belongs to one of the fixed ``CATEGORIES`` (appliance types). A
thread can also be about one marketplace product (``product_type`` +
``product_id``, the same pair the cart uses): it is then listed on that
product's detail page as well, and its category comes from the product.

Anyone signed in can open a thread, reply, upvote and report. Only the author
can edit or delete a post. A post reported by ``REPORTS_TO_HIDE`` different
users is hidden automatically; an admin (Firebase custom claim
``admin: true`` or ``role: "admin"``) can hide or restore any post (see ``roles``).

What the app receives never contains a uid: only the author's display name,
an optional badge and ``is_mine``. Badges are decided here, when the post is
written, so a client cannot claim one:

    "seller"           the author owns the second-hand listing the thread is about
    "verified_buyer"   the author has a paid order containing that product
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import checkout_service
import forum_store
import marketplace_store
import roles

# Fixed list (#44): id -> name shown in the app. Ids match the catalogue
# "category" values with "-" instead of spaces, so a product maps to one.
CATEGORIES: dict[str, str] = {
    "washing-machine": "Washing machines",
    "dryer": "Dryers",
    "dishwasher": "Dishwashers",
    "refrigerator": "Fridges and freezers",
    "oven": "Ovens",
    "hob": "Hobs",
    "microwave": "Microwaves",
    "coffee-machine": "Coffee machines",
    "other": "Other appliances",
}
FALLBACK_CATEGORY = "other"

REPORTS_TO_HIDE = 3
DEFAULT_PAGE = 20
MAX_PAGE = 50
MAX_NAME = 60
DEFAULT_AUTHOR = "HoDoWo user"

TITLE_LIMITS = (8, 150)
BODY_LIMITS = (0, 2000)
REPLY_LIMITS = (2, 2000)
MODEL_LIMITS = (0, 80)
NOTE_LIMITS = (0, 300)

VISIBLE = forum_store.VISIBLE
HIDDEN = "hidden"
DELETED = "deleted"

SORTS = ("recent", "popular")

_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]+")


class ForumError(Exception):
    """Forum action refused; carries the HTTP status and a client-safe detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def get_store():
    """Return the forum persistence backend (replaced by a fake in the tests)."""
    return forum_store


# helpers

def product_key(product_type: str, product_id: str) -> str:
    return f"{product_type}:{product_id}"


def category_of(product: dict[str, Any]) -> str:
    """Forum category of a catalogue product ("washing machine" -> "washing-machine")."""
    key = "-".join(str(product.get("category") or "").lower().split())
    return key if key in CATEGORIES else FALLBACK_CATEGORY


def clean_text(text: str | None, limits: tuple[int, int], field: str) -> str:
    """Drop control characters and extra blank space, then check the length.

    The app shows posts as plain text, never as HTML, so nothing is escaped
    here; the text is stored as the user wrote it, minus invisible characters.
    """
    kept = "".join(
        char for char in (text or "")
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
    lines = [_SPACES.sub(" ", line).strip() for line in kept.replace("\r", "").split("\n")]
    cleaned = _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
    shortest, longest = limits
    if len(cleaned) < shortest:
        raise ForumError(422, f"the {field} must be at least {shortest} characters")
    if len(cleaned) > longest:
        raise ForumError(422, f"the {field} can be at most {longest} characters")
    return cleaned


def author_name(user: dict[str, Any]) -> str:
    """Display name from the token; never the email."""
    raw = "".join(c for c in str(user.get("name") or "") if not unicodedata.category(c).startswith("C"))
    return " ".join(raw.split())[:MAX_NAME].strip() or DEFAULT_AUTHOR


def is_admin(user: dict[str, Any]) -> bool:
    return roles.is_admin(user)


def _vote_id(uid: str, thread_id: str, reply_id: str | None = None) -> str:
    return f"{uid}_{thread_id}_{reply_id}" if reply_id else f"{uid}_{thread_id}"


def _limit(limit: int) -> int:
    return min(max(limit, 1), MAX_PAGE)


def _check_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ForumError(404, "category not found")


def _check_sort(sort: str) -> None:
    if sort not in SORTS:
        raise ForumError(422, "sort must be recent or popular")


def _product(product_type: str | None, product_id: str | None) -> dict[str, Any] | None:
    if product_type not in marketplace_store.PRODUCT_SOURCES or not product_id:
        return None
    return checkout_service.get_store().get_item(product_type, product_id)


def _badge(uid: str, thread: dict[str, Any], item: dict[str, Any] | None) -> str | None:
    product_type, product_id = thread.get("product_type"), thread.get("product_id")
    if not product_type or not product_id:
        return None
    if item and product_type == "used" and item.get("seller_id") == uid:
        return "seller"
    for order in checkout_service.get_store().list_user_orders(uid, checkout_service.ORDER_HISTORY_LIMIT):
        if order.get("status") != "paid":
            continue
        for line in order.get("items") or []:
            if line.get("product_type") == product_type and line.get("product_id") == product_id:
                return "verified_buyer"
    return None


def _count(value: Any) -> int:
    return max(int(value or 0), 0)


def _public_reply(reply: dict[str, Any], uid: str, votes: set[str], thread_id: str) -> dict[str, Any]:
    return {
        "reply_id": reply["reply_id"],
        "body": reply.get("body", ""),
        "author_name": reply.get("author_name") or DEFAULT_AUTHOR,
        "author_badge": reply.get("author_badge"),
        "created_at": reply.get("created_at"),
        "edited_at": reply.get("edited_at"),
        "upvotes": _count(reply.get("upvotes")),
        "voted_by_me": _vote_id(uid, thread_id, reply["reply_id"]) in votes,
        "is_mine": reply.get("uid") == uid,
    }


def _public_thread(thread: dict[str, Any], uid: str, votes: set[str]) -> dict[str, Any]:
    thread_id = thread["thread_id"]
    product = None
    if thread.get("product_type") and thread.get("product_id"):
        product = {"product_type": thread["product_type"], "product_id": thread["product_id"],
                   "title": thread.get("product_title") or ""}
    return {
        "thread_id": thread_id,
        "category": thread.get("category") or FALLBACK_CATEGORY,
        "product": product,
        "appliance_model": thread.get("appliance_model") or None,
        "title": thread.get("title", ""),
        "body": thread.get("body") or None,
        "author_name": thread.get("author_name") or DEFAULT_AUTHOR,
        "author_badge": thread.get("author_badge"),
        "created_at": thread.get("created_at"),
        "edited_at": thread.get("edited_at"),
        "last_activity_at": thread.get("last_activity_at") or thread.get("created_at"),
        "upvotes": _count(thread.get("upvotes")),
        "reply_count": _count(thread.get("reply_count")),
        "voted_by_me": _vote_id(uid, thread_id) in votes,
        "is_mine": thread.get("uid") == uid,
    }


def _shown(post: dict[str, Any]) -> bool:
    return post.get("status", VISIBLE) == VISIBLE


def _visible_thread(thread_id: str) -> dict[str, Any]:
    thread = get_store().get_thread(thread_id)
    # Hidden and deleted threads look missing, like any id that never existed.
    if thread is None or not _shown(thread):
        raise ForumError(404, "thread not found")
    return thread


def _visible_reply(thread_id: str, reply_id: str) -> dict[str, Any]:
    reply = get_store().get_reply(thread_id, reply_id)
    if reply is None or not _shown(reply):
        raise ForumError(404, "reply not found")
    return reply


def _target(thread_id: str, reply_id: str | None) -> dict[str, Any]:
    thread = _visible_thread(thread_id)
    return _visible_reply(thread_id, reply_id) if reply_id else thread


def _own_target(uid: str, thread_id: str, reply_id: str | None) -> dict[str, Any]:
    post = _target(thread_id, reply_id)
    # Someone else's post is reported as missing, so ids cannot be probed.
    if post.get("uid") != uid:
        raise ForumError(404, "reply not found" if reply_id else "thread not found")
    return post


def _paged(action, *args):
    try:
        return action(*args)
    except forum_store.InvalidCursor as exc:
        raise ForumError(422, "invalid cursor") from exc


# reading

def list_categories() -> dict[str, Any]:
    return {"categories": [{"id": key, "name": name} for key, name in CATEGORIES.items()]}


def list_category_threads(uid: str, category: str, sort: str = "recent", limit: int = DEFAULT_PAGE,
                          cursor: str | None = None) -> dict[str, Any]:
    """One page of a category's threads; pass ``next_cursor`` back to get the next one."""
    _check_category(category)
    _check_sort(sort)
    store = get_store()
    threads, more = _paged(store.list_category_threads, category, sort, _limit(limit), cursor)
    votes = store.voted([_vote_id(uid, t["thread_id"]) for t in threads])
    return {
        "threads": [_public_thread(t, uid, votes) for t in threads],
        "next_cursor": threads[-1]["thread_id"] if more and threads else None,
    }


def _top_reply(replies: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Most upvoted reply; the oldest wins a tie (it was there first)."""
    shown = [reply for reply in replies if _shown(reply)]
    if not shown:
        return None
    return min(shown, key=lambda reply: (-(reply.get("upvotes") or 0), reply.get("created_at") or 0))


def list_product_threads(uid: str, product_type: str, product_id: str, sort: str = "popular",
                         limit: int = DEFAULT_PAGE) -> dict[str, Any]:
    """Questions about one product for its detail page, each with its best reply."""
    _check_sort(sort)
    store = get_store()
    threads = [t for t in store.list_product_threads(product_key(product_type, product_id)) if _shown(t)]
    if sort == "popular":
        threads.sort(key=lambda t: (-(t.get("upvotes") or 0), -(t.get("last_activity_at") or 0)))
    else:
        threads.sort(key=lambda t: -(t.get("last_activity_at") or 0))
    page = threads[:_limit(limit)]

    tops = {t["thread_id"]: _top_reply(store.all_replies(t["thread_id"])) for t in page if t.get("reply_count")}
    vote_ids = [_vote_id(uid, t["thread_id"]) for t in page]
    vote_ids += [_vote_id(uid, tid, top["reply_id"]) for tid, top in tops.items() if top]
    votes = store.voted(vote_ids)

    result = []
    for thread in page:
        public = _public_thread(thread, uid, votes)
        top = tops.get(thread["thread_id"])
        public["top_reply"] = _public_reply(top, uid, votes, thread["thread_id"]) if top else None
        result.append(public)
    return {"threads": result, "total": len(threads)}


def get_thread(uid: str, thread_id: str) -> dict[str, Any]:
    thread = _visible_thread(thread_id)
    return _public_thread(thread, uid, get_store().voted([_vote_id(uid, thread_id)]))


def list_replies(uid: str, thread_id: str, limit: int = DEFAULT_PAGE, cursor: str | None = None) -> dict[str, Any]:
    """One page of a thread's replies, oldest first."""
    _visible_thread(thread_id)
    store = get_store()
    replies, more = _paged(store.list_replies, thread_id, _limit(limit), cursor)
    votes = store.voted([_vote_id(uid, thread_id, r["reply_id"]) for r in replies])
    return {
        "replies": [_public_reply(r, uid, votes, thread_id) for r in replies],
        "next_cursor": replies[-1]["reply_id"] if more and replies else None,
    }


# writing

def create_thread(user: dict[str, Any], uid: str, title: str, body: str | None = None,
                  category: str | None = None, appliance_model: str | None = None,
                  product_type: str | None = None, product_id: str | None = None) -> dict[str, Any]:
    """Open a thread in a category, or about a product (its category is then the product's)."""
    item = None
    if product_type or product_id:
        if not (product_type and product_id):
            raise ForumError(422, "product_type and product_id go together")
        item = _product(product_type, product_id)
        if item is None:
            raise ForumError(404, "product not found")
        if product_type == "used" and item.get("status") != "active":
            raise ForumError(409, "this listing is no longer active")
        category = category_of(item)
        appliance_model = appliance_model or " ".join(
            str(item.get(field) or "") for field in ("brand", "model")
        ).strip()
    elif not category:
        raise ForumError(422, "choose a category or a product")
    _check_category(category)

    now = checkout_service.now_ms()
    data = {
        "category": category,
        "product_type": product_type,
        "product_id": product_id,
        "product_key": product_key(product_type, product_id) if item else None,
        "product_title": str(item.get("title") or "")[:150] if item else None,
        "appliance_model": clean_text(appliance_model, MODEL_LIMITS, "appliance model") or None,
        "uid": uid,
        "author_name": author_name(user),
        "title": clean_text(title, TITLE_LIMITS, "title"),
        "body": clean_text(body, BODY_LIMITS, "text"),
        "status": VISIBLE,
        "upvotes": 0,
        "reply_count": 0,
        "report_count": 0,
        "created_at": now,
        "last_activity_at": now,
    }
    data["author_badge"] = _badge(uid, data, item)
    thread_id = get_store().create_thread(data)
    return _public_thread({**data, "thread_id": thread_id}, uid, set())


def create_reply(user: dict[str, Any], uid: str, thread_id: str, body: str) -> dict[str, Any]:
    thread = _visible_thread(thread_id)
    data = {
        "uid": uid,
        "author_name": author_name(user),
        "author_badge": _badge(uid, thread, _product(thread.get("product_type"), thread.get("product_id"))),
        "body": clean_text(body, REPLY_LIMITS, "reply"),
        "status": VISIBLE,
        "upvotes": 0,
        "report_count": 0,
        "created_at": checkout_service.now_ms(),
    }
    reply_id = get_store().create_reply(thread_id, data)
    return _public_reply({**data, "reply_id": reply_id}, uid, set(), thread_id)


def edit_thread(uid: str, thread_id: str, title: str | None, body: str | None) -> dict[str, Any]:
    _own_target(uid, thread_id, None)
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = clean_text(title, TITLE_LIMITS, "title")
    if body is not None:
        fields["body"] = clean_text(body, BODY_LIMITS, "text")
    if not fields:
        raise ForumError(422, "nothing to change")
    fields["edited_at"] = checkout_service.now_ms()
    get_store().update_post(thread_id, None, fields)
    return get_thread(uid, thread_id)


def edit_reply(uid: str, thread_id: str, reply_id: str, body: str) -> dict[str, Any]:
    reply = _own_target(uid, thread_id, reply_id)
    fields = {"body": clean_text(body, REPLY_LIMITS, "reply"), "edited_at": checkout_service.now_ms()}
    get_store().update_post(thread_id, reply_id, fields)
    return _public_reply({**reply, **fields}, uid, get_store().voted([_vote_id(uid, thread_id, reply_id)]), thread_id)


def delete_post(uid: str, thread_id: str, reply_id: str | None = None) -> None:
    _own_target(uid, thread_id, reply_id)
    get_store().set_status(thread_id, reply_id, DELETED, -1 if reply_id else 0)


def vote(uid: str, thread_id: str, reply_id: str | None = None, up: bool = True) -> dict[str, Any]:
    """Add (``up``) or withdraw an upvote; repeating either is harmless."""
    store = get_store()
    post = _target(thread_id, reply_id)
    if post.get("uid") == uid:
        raise ForumError(409, "you cannot upvote your own post")

    vote_id = _vote_id(uid, thread_id, reply_id)
    if up:
        changed = store.add_vote(vote_id, {"uid": uid, "thread_id": thread_id, "reply_id": reply_id,
                                           "created_at": checkout_service.now_ms()}, thread_id, reply_id)
    else:
        changed = store.remove_vote(vote_id, thread_id, reply_id)

    upvotes = _count(post.get("upvotes")) + ((1 if up else -1) if changed else 0)
    return {"upvotes": max(upvotes, 0), "voted_by_me": up}


def report(uid: str, thread_id: str, reply_id: str | None, reason: str, note: str | None) -> dict[str, Any]:
    """Store a report; after REPORTS_TO_HIDE different users the post is hidden."""
    store = get_store()
    post = _target(thread_id, reply_id)
    if post.get("uid") == uid:
        raise ForumError(409, "you cannot report your own post")

    data = {
        "uid": uid,
        "thread_id": thread_id,
        "reply_id": reply_id,
        "reason": reason,
        "note": clean_text(note, NOTE_LIMITS, "note"),
        "created_at": checkout_service.now_ms(),
    }
    if store.add_report(_vote_id(uid, thread_id, reply_id), data, thread_id, reply_id):
        if _count(post.get("report_count")) + 1 >= REPORTS_TO_HIDE:
            store.set_status(thread_id, reply_id, HIDDEN, -1 if reply_id else 0)
    # Same answer for a first and a repeated report: nothing to learn from it.
    return {"reported": True}


def moderate(user: dict[str, Any], thread_id: str, reply_id: str | None, action: str) -> dict[str, Any]:
    """Admin only: hide a post, or restore a hidden one."""
    if not is_admin(user):
        raise ForumError(403, "only an admin can moderate the forum")
    store = get_store()
    thread = store.get_thread(thread_id)
    post = store.get_reply(thread_id, reply_id) if reply_id and thread else thread
    if post is None or post.get("status") == DELETED:
        raise ForumError(404, "reply not found" if reply_id else "thread not found")

    status = HIDDEN if action == "hide" else VISIBLE
    if post.get("status", VISIBLE) != status:
        change = (-1 if status == HIDDEN else 1) if reply_id else 0
        store.set_status(thread_id, reply_id, status, change)
    return {"status": status}
