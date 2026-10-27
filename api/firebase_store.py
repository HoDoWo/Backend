from __future__ import annotations

import functools
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.firestore_v1 import base_query

logger = logging.getLogger(__name__)

# Names used when the appliance was not recognised: guides saved under them
# belong to different appliances, so they are never grouped together.
GENERIC_APPLIANCE_NAMES = frozenset({"", "appliance", "household-appliance"})


class FirebaseConfigurationError(RuntimeError):
    """Raised when Firebase cannot be configured for persistence."""


@functools.lru_cache(maxsize=1)
def get_firestore_client():
    """Return a lazily initialized Firestore client."""

    if not firebase_admin._apps:
        service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        project_id = os.getenv("FIREBASE_PROJECT_ID", "").strip() or None
        # Cloud Storage bucket for the manual photos (e.g. "my-app.firebasestorage.app").
        storage_bucket = os.getenv("FIREBASE_STORAGE_BUCKET", "").strip() or None
        options = {
            key: value
            for key, value in (("projectId", project_id), ("storageBucket", storage_bucket))
            if value
        } or None
        if service_account_json:
            try:
                credential = credentials.Certificate(json.loads(service_account_json))
            except (json.JSONDecodeError, ValueError) as exc:
                raise FirebaseConfigurationError(
                    "FIREBASE_SERVICE_ACCOUNT_JSON must contain valid service-account JSON"
                ) from exc
            firebase_admin.initialize_app(credential, options)
        else:
            firebase_admin.initialize_app(options=options)
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



def save_manual(user_uid: str, manual: dict[str, Any], image_bytes: bytes | None = None) -> str:
    """Persist a generated manual and return its Firestore document id.

    With ``image_bytes`` the user's photo is also uploaded to Cloud Storage.
    The upload is best effort: when Storage is not configured or fails, the
    manual is still saved (without ``image_path``), so it keeps appearing in
    the user's Home and history.
    """
    if not user_uid:
        raise ValueError("authenticated user uid is required")

    ref = get_firestore_client().collection("manuals").document()
    image_storage_path = _upload_manual_image(user_uid, ref.id, image_bytes) if image_bytes else None

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
            # Picture shown on the Home cards (the generated guide image).
            "image_url": manual.get("image_url") or manual.get("image"),
            # The user's own photo in Cloud Storage, when it could be uploaded.
            "image_path": image_storage_path,
            "is_bookmarked": bool(manual.get("is_bookmarked", False)),
            "created_at": firestore_server_timestamp(),
        }
    )
    return ref.id


def manual_image_blob(user_uid: str, manual_id: str):
    """Cloud Storage object that holds the user's photo of a manual."""
    return storage.bucket().blob(f"manuals/{user_uid}/{manual_id}.jpg")


def _upload_manual_image(user_uid: str, manual_id: str, image_bytes: bytes) -> str | None:
    """Uploads the photo of a manual; returns its Storage path, or None when it fails."""
    try:
        blob = manual_image_blob(user_uid, manual_id)
        blob.upload_from_string(image_bytes, content_type="image/jpeg")
        return blob.name
    except Exception as exc:  # noqa: BLE001 - Storage problems must never lose the manual.
        logger.warning("Manual photo could not be uploaded to Cloud Storage: %s", exc)
        return None


def firestore_server_timestamp():

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


# How many of a user's manuals are read to pick the newest ones. Sorting is
# done here, so the query needs no composite index.
USER_MANUALS_TO_READ = 100


def _created(manual: dict[str, Any]) -> datetime:
    value = manual.get("created_at")
    return value if isinstance(value, datetime) else datetime.min.replace(tzinfo=timezone.utc)


def list_user_manuals(
    user_uid: str, limit: int, bookmarked_only: bool = False
) -> list[dict[str, Any]]:
    """Newest manuals of one user (with their Firestore id), for the Home screen."""
    if not user_uid:
        raise ValueError("authenticated user uid is required")


    query = get_firestore_client().collection("manuals").where(
        filter=base_query.FieldFilter("user_uid", "==", user_uid)
    )
    manuals = [
        {**(snapshot.to_dict() or {}), "id": snapshot.id}
        for snapshot in query.limit(USER_MANUALS_TO_READ).stream()
    ]
    if bookmarked_only:
        manuals = [manual for manual in manuals if manual.get("is_bookmarked")]
    return sorted(manuals, key=_created, reverse=True)[:limit]


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


    manuals = get_firestore_client().collection("manuals")
    snapshots = list(
        manuals.where(filter=base_query.FieldFilter("appliance_key", "==", key)).limit(limit).stream()
    )
    if not snapshots:
        snapshots = list(
            manuals.where(filter=base_query.FieldFilter("appliance_name", "==", appliance_name))
            .limit(limit)
            .stream()
        )

    rated = []
    for snapshot in snapshots:
        score = _rating_score(snapshot.reference)
        if score:
            rated.append({**(snapshot.to_dict() or {}), "id": snapshot.id, "score": score})

    return sorted(rated, key=_created, reverse=True)


# How many of the newest manuals (of all users) are read to rank the popular ones.
POPULAR_MANUALS_TO_READ = 300

# Titles too short to mean anything ("?", "ok") are not ranked.
MIN_POPULAR_TITLE_LENGTH = 4


def _title_key(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def _is_web_image(url: Any) -> bool:
    """Only real web images are shared: data URIs are large copies of user photos."""
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def popular_manuals(limit: int) -> list[dict[str, Any]]:
    """Most searched and most saved guides among all the users of the app.

    Manuals of the same recognised appliance with the same issue title count as the
    same guide. ``search_count`` is how many times it was generated, ``save_count``
    how many users bookmarked it; a save weighs twice a search. The newest manual of
    each group (preferably one with a web image) represents it. Nothing that
    identifies a user is returned.
    """
    from google.cloud.firestore_v1 import Query

    snapshots = (
        get_firestore_client()
        .collection("manuals")
        .order_by("created_at", direction=Query.DESCENDING)
        .limit(POPULAR_MANUALS_TO_READ)
        .stream()
    )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        manual = {**(snapshot.to_dict() or {}), "id": snapshot.id}
        key = manual.get("appliance_key") or appliance_key(manual.get("appliance_name"))
        title = _title_key(manual.get("issue_title"))
        if not key or len(title) < MIN_POPULAR_TITLE_LENGTH or not manual.get("steps"):
            continue
        groups.setdefault((key, title), []).append(manual)

    ranked = []
    for manuals in groups.values():
        manuals.sort(key=_created, reverse=True)
        best = next((m for m in manuals if _is_web_image(m.get("image_url"))), manuals[0])
        searches = len(manuals)
        saves = sum(1 for m in manuals if m.get("is_bookmarked"))
        ranked.append(
            {
                "id": best["id"],
                "appliance_name": best.get("appliance_name"),
                "issue_title": best.get("issue_title"),
                "overview": best.get("overview"),
                "steps": best.get("steps"),
                "warnings": best.get("warnings"),
                "image_url": best.get("image_url") if _is_web_image(best.get("image_url")) else None,
                "created_at": manuals[0].get("created_at"),
                "search_count": searches,
                "save_count": saves,
                "score": searches + 2 * saves,
            }
        )

    ranked.sort(key=lambda m: (m["score"], _created(m)), reverse=True)
    return ranked[:limit]
