from __future__ import annotations

from datetime import datetime
from typing import Any

import time

import auth
import fastapi
import firebase_store
import logging_config
from fastapi import HTTPException

logger = logging_config.get_logger(__name__)

manuals_router = fastapi.APIRouter()

DEFAULT_LIMIT = 5
MAX_LIMIT = 20

# The popular list is the same for everybody: it is computed at most every 10 minutes.
POPULAR_CACHE_SECONDS = 600
_popular_cache: dict[str, Any] = {}


def _serialize(manual: dict[str, Any]) -> dict[str, Any]:
    created_at = manual.get("created_at")
    return {
        "id": manual.get("id"),
        "appliance_name": manual.get("appliance_name") or "Household Appliance",
        "issue_title": manual.get("issue_title") or "Appliance Guide",
        "overview": manual.get("overview") or "",
        "steps": manual.get("steps") or [],
        "warnings": manual.get("warnings") or [],
        "image_url": manual.get("image_url"),
        "is_bookmarked": bool(manual.get("is_bookmarked", False)),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
        # Epoch milliseconds: easy to use on Android without java.time (minSdk 24).
        "created_at_ms": int(created_at.timestamp() * 1000) if isinstance(created_at, datetime) else None,
    }


@manuals_router.get("/api/manuals")
async def list_manuals(
    request: fastapi.Request,
    limit: int = DEFAULT_LIMIT,
    bookmarked: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Newest manuals of the authenticated user (only bookmarked ones on request)."""
    uid = _uid(request)
    limit = max(1, min(limit, MAX_LIMIT))
    try:
        manuals = firebase_store.list_user_manuals(uid, limit, bookmarked_only=bookmarked)
    except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
        logger.error("Manual listing failed: %s", exc)
        raise HTTPException(status_code=503, detail="manual persistence is unavailable") from exc

    return {"manuals": [_serialize(manual) for manual in manuals]}


def _uid(request: fastapi.Request) -> str:
    user = auth.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="authenticated user id is missing")
    return uid


@manuals_router.get("/api/manuals/popular")
async def list_popular_manuals(
    request: fastapi.Request,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, list[dict[str, Any]]]:
    """Most searched and most saved guides among all users (Home, "Most popular")."""
    _uid(request)
    limit = max(1, min(limit, MAX_LIMIT))

    cached = _popular_cache.get("manuals")
    if cached is None or time.monotonic() - _popular_cache.get("at", 0.0) > POPULAR_CACHE_SECONDS:
        try:
            cached = firebase_store.popular_manuals(MAX_LIMIT)
        except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
            logger.error("Popular manuals failed: %s", exc)
            raise HTTPException(status_code=503, detail="manual persistence is unavailable") from exc
        _popular_cache.update(manuals=cached, at=time.monotonic())

    return {
        "manuals": [
            {
                **_serialize(manual),
                "is_bookmarked": False,
                "search_count": manual.get("search_count", 0),
                "save_count": manual.get("save_count", 0),
            }
            for manual in cached[:limit]
        ]
    }
