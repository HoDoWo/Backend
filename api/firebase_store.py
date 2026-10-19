"""Firestore persistence for generated appliance manuals."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any


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


def _manual_ref(manual_id: str):
    return get_firestore_client().collection("manuals").document(manual_id)


def save_manual(user_uid: str, manual: dict[str, Any]) -> str:
    """Persist a generated manual and return its Firestore document id."""
    if not user_uid:
        raise ValueError("authenticated user uid is required")
    ref = get_firestore_client().collection("manuals").document()
    ref.set(
        {
            "user_uid": user_uid,
            "appliance_name": manual.get("appliance_name") or "Household Appliance",
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
