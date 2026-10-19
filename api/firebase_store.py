"""Firestore persistence for generated appliance manuals."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

# Names used when the appliance was not recognised: guides saved under them
# belong to different appliances, so they are never grouped together.
GENERIC_APPLIANCE_NAMES = frozenset({"", "appliance", "household-appliance"})


class FirebaseConfigurationError(RuntimeError):
    """Raised when Firebase cannot be configured for persistence."""


@lru_cache(maxsize=1)
def get_firestore_client():
    """Return a lazily initialized Firestore client."""
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError as exc:
        raise FirebaseConfigurationError(
            "firebase-admin is required for manual persistence"
        ) from exc

    if not firebase_admin._apps:
        service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        project_id = os.getenv("FIREBASE_PROJECT_ID", "").strip() or None
        if service_account_json:
            try:
                credential = credentials.Certificate(json.loads(service_account_json))
            except (json.JSONDecodeError, ValueError) as exc:
                raise FirebaseConfigurationError(
                    "FIREBASE_SERVICE_ACCOUNT_JSON must contain valid service-account JSON"
                ) from exc
            firebase_admin.initialize_app(credential, {"projectId": project_id} if project_id else None)
        else:
            firebase_admin.initialize_app(
                options={"projectId": project_id} if project_id else None
            )
    return firestore.client()


def appliance_key(appliance_name: str | None) -> str:
    """Normalised key that groups the manuals of the same appliance (#40).

    "Bosch Washing machine (WAN28)" -> "bosch-washing-machine-wan28".
    Returns "" for missing or generic names such as "Household Appliance".
    """
    key = re.sub(r"[^a-z0-9]+", "-", str(appliance_name or "").lower()).strip("-")
    return "" if key in GENERIC_APPLIANCE_NAMES else key


def _manual_ref(manual_id: str):
    return get_firestore_client().collection("manuals").document(manual_id)


def save_manual(user_uid: str, manual: dict[str, Any]) -> str:
    """Persist a generated manual and return its Firestore document id."""
    if not user_uid:
        raise ValueError("authenticated user uid is required")
    ref = get_firestore_client().collection("manuals").document()
    appliance_name = manual.get("appliance_name") or "Household Appliance"
    ref.set(
        {
            "user_uid": user_uid,
            "appliance_name": appliance_name,
            "appliance_key": appliance_key(appliance_name),
            "issue_title": manual.get("issue_title") or "Appliance Guide",
            "overview": manual.get("overview") or manual.get("reply") or "",
            "steps": manual.get("steps") or [],
            "warnings": manual.get("warnings") or [],
            "image_url": manual.get("image_url") or manual.get("image"),
            "is_bookmarked": bool(manual.get("is_bookmarked", False)),
            "created_at": firestore_server_timestamp(),
        }
    )
    return ref.id


def firestore_server_timestamp():
    from firebase_admin import firestore

    return firestore.SERVER_TIMESTAMP


def update_bookmark(user_uid: str, manual_id: str, is_bookmarked: bool) -> bool:
    ref = _manual_ref(manual_id)
    snapshot = ref.get()
    if not snapshot.exists or snapshot.to_dict().get("user_uid") != user_uid:
        raise LookupError("manual not found")
    ref.update({"is_bookmarked": is_bookmarked})
    return is_bookmarked


def set_rating(user_uid: str, manual_id: str, rating: int) -> int:
    if rating not in (-1, 1):
        raise ValueError("rating must be 1 or -1")
    ref = _manual_ref(manual_id)
    snapshot = ref.get()
    if not snapshot.exists or snapshot.to_dict().get("user_uid") != user_uid:
        raise LookupError("manual not found")

    rating_ref = ref.collection("ratings").document(user_uid)
    current = rating_ref.get()
    if current.exists and current.to_dict().get("rating") == rating:
        rating_ref.delete()
        return 0
    rating_ref.set({"user_uid": user_uid, "rating": rating})
    return rating


def _rating_score(manual_ref) -> int:
    """Sum of the like (+1) and dislike (-1) votes stored under a manual."""
    total = 0
    for vote in manual_ref.collection("ratings").stream():
        value = (vote.to_dict() or {}).get("rating")
        if value in (-1, 1):
            total += value
    return total


def find_rated_manuals(appliance_name: str | None, limit: int = 20) -> list[dict[str, Any]]:
    """Manuals of the same appliance that users rated, newest first (#40).

    Reads the manuals of every user (the backend uses the admin SDK), matching
    on ``appliance_key``. Manuals saved before that field existed are found by
    their exact ``appliance_name``. Unrated manuals (score 0) are left out.
    """
    key = appliance_key(appliance_name)
    if not key:
        return []

    from google.cloud.firestore_v1.base_query import FieldFilter

    manuals = get_firestore_client().collection("manuals")
    snapshots = list(
        manuals.where(filter=FieldFilter("appliance_key", "==", key)).limit(limit).stream()
    )
    if not snapshots:
        snapshots = list(
            manuals.where(filter=FieldFilter("appliance_name", "==", appliance_name))
            .limit(limit)
            .stream()
        )

    rated = []
    for snapshot in snapshots:
        score = _rating_score(snapshot.reference)
        if score:
            rated.append({**(snapshot.to_dict() or {}), "id": snapshot.id, "score": score})

    oldest = datetime.min.replace(tzinfo=timezone.utc)

    def created(manual: dict[str, Any]) -> datetime:
        value = manual.get("created_at")
        return value if isinstance(value, datetime) else oldest

    return sorted(rated, key=created, reverse=True)
