"""The names users see of each other: the profile name, never the email.

A Google account brings its name in the token; an email account has none
there until the user sets one in the Profile screen, which saves it in
``users/{uid}.displayName`` (and in Firebase Auth). So when a stored name is
missing or still the placeholder ("Community seller", "HoDoWo user") it is
looked up by uid at read time, and a name set later shows on old listings
and chats too.
"""

from __future__ import annotations

import time
import unicodedata
from typing import Any

MAX_NAME = 60
COMMUNITY_SELLER = "Community seller"
HODOWO_USER = "HoDoWo user"
PLACEHOLDERS = frozenset({COMMUNITY_SELLER, HODOWO_USER, ""})
CACHE_SECONDS = 300

_cache: dict[str, tuple[float, str | None]] = {}


def clean(raw: Any) -> str:
    """Printable characters only, single spaces, at most MAX_NAME characters."""
    text = "".join(c for c in str(raw or "") if not unicodedata.category(c).startswith("C"))
    return " ".join(text.split())[:MAX_NAME].strip()


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


def lookup(uid: str) -> str | None:
    """The profile name of [uid], cached for a few minutes; None when there is none."""
    if not uid:
        return None
    now = time.monotonic()
    hit = _cache.get(uid)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    name = _fetch(uid)
    _cache[uid] = (now, name)
    return name


def _fetch(uid: str) -> str | None:
    """users/{uid}.displayName, else the Firebase Auth display name. Never raises."""
    try:
        import firebase_store

        profile = firebase_store.get_firestore_client().collection("users").document(uid).get()
        name = clean((profile.to_dict() or {}).get("displayName")) if profile.exists else ""
        if name:
            return name
    except Exception:  # noqa: BLE001 - a missing name must never break a listing or a chat
        pass
    try:
        from firebase_admin import auth as firebase_auth

        return clean(firebase_auth.get_user(uid).display_name) or None
    except Exception:  # noqa: BLE001
        return None
