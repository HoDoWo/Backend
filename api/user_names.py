"""The names users see of each other: the profile name, never the email.

A Google account brings its name in the token; an email account has none
there until the user sets one in the Profile screen, which saves it in
``users/{uid}.displayName`` (and in Firebase Auth). So when a stored name is
missing or still the placeholder ("Community seller", "HoDoWo user") it is
looked up by uid at read time, and a name set later shows on old listings
and chats too.

Lists resolve their names in one go: ``prefetch`` reads every missing
profile with one Firestore ``get_all`` and the few left without a name with
one Firebase Auth ``get_users``, so a page costs at most two calls, not two
per row. The cache is bounded (least recently used names are dropped).
"""

from __future__ import annotations

import logging
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

MAX_NAME = 60
COMMUNITY_SELLER = "Community seller"
HODOWO_USER = "HoDoWo user"
PLACEHOLDERS = frozenset({COMMUNITY_SELLER, HODOWO_USER, ""})
CACHE_SECONDS = 300
CACHE_SIZE = 1000
# Firebase Auth answers at most 100 identifiers per get_users call.
AUTH_BATCH = 100

logger = logging.getLogger(__name__)

_cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()


def clean(raw: Any) -> str:
    """Printable characters only, single spaces, at most MAX_NAME characters."""
    text = "".join(c for c in str(raw or "") if not unicodedata.category(c).startswith("C"))
    return " ".join(text.split())[:MAX_NAME].strip()


def is_placeholder(stored: Any) -> bool:
    return clean(stored) in PLACEHOLDERS


def of_user(user: dict[str, Any]) -> str | None:
    """The name of the signed-in user: the token's, else the profile's."""
    uid = str(user.get("uid") or user.get("sub") or "")
    return clean(user.get("name")) or lookup(uid)


def resolve(stored: Any, uid: Any, default: str) -> str:
    """[stored] unless it is a placeholder; then the owner's current name, else [default]."""
    name = clean(stored)
    if name and name not in PLACEHOLDERS:
        return name
    return lookup(str(uid or "")) or default


def prefetch(uids: Iterable[Any]) -> None:
    """Loads the names of [uids] not cached yet, in one batch: call it before resolving a list."""
    now = time.monotonic()
    missing = list(dict.fromkeys(str(uid) for uid in uids if uid and _cached(str(uid), now) is _MISS))
    if not missing:
        return
    names = _fetch_many(missing)
    for uid in missing:
        _remember(uid, names.get(uid), now)


def lookup(uid: str) -> str | None:
    """The profile name of [uid], cached for a few minutes; None when there is none."""
    if not uid:
        return None
    hit = _cached(uid, time.monotonic())
    if hit is not _MISS:
        return hit
    prefetch([uid])
    hit = _cached(uid, time.monotonic())
    return None if hit is _MISS else hit


_MISS: Any = object()


def _cached(uid: str, now: float) -> Any:
    hit = _cache.get(uid)
    if hit is None or now - hit[0] >= CACHE_SECONDS:
        return _MISS
    _cache.move_to_end(uid)
    return hit[1]


def _remember(uid: str, name: str | None, now: float) -> None:
    _cache[uid] = (now, name)
    _cache.move_to_end(uid)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


def _fetch_many(uids: list[str]) -> dict[str, str | None]:
    """Profile names (users/{uid}.displayName), then Firebase Auth for the rest. Never raises."""
    names: dict[str, str | None] = {}
    try:
        import firebase_store

        client = firebase_store.get_firestore_client()
        refs = [client.collection("users").document(uid) for uid in uids]
        for snapshot in client.get_all(refs):
            if snapshot.exists:
                names[snapshot.id] = clean((snapshot.to_dict() or {}).get("displayName")) or None
    except Exception:  # a missing name must never break a listing or a chat
        logger.warning("profile names unavailable", exc_info=True)
    rest = [uid for uid in uids if not names.get(uid)]
    for start in range(0, len(rest), AUTH_BATCH):
        try:
            from firebase_admin import auth as firebase_auth

            identifiers = [firebase_auth.UidIdentifier(uid) for uid in rest[start:start + AUTH_BATCH]]
            for record in firebase_auth.get_users(identifiers).users:
                names[record.uid] = clean(record.display_name) or None
        except Exception:  # Auth unavailable: the placeholder stays
            logger.warning("auth display names unavailable", exc_info=True)
            break
    return names
