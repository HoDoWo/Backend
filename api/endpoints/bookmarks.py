from typing import Any

import auth
import fastapi
import firebase_store
import logging_config
import schemas

logger = logging_config.get_logger(__name__)

bookmark_router = fastapi.APIRouter()

@bookmark_router.patch("/api/manuals/{manual_id}/bookmark", response_model=dict)
async def toggle_bookmark(
    manual_id: str, req: schemas.BookmarkRequest, request: fastapi.Request
) -> dict[str, Any]:
    """Set the bookmark state for a manual owned by the authenticated user."""
    user = auth.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise fastapi.HTTPException(status_code=401, detail="authenticated user id is missing")
    try:
        is_bookmarked = firebase_store.update_bookmark(uid, manual_id, req.is_bookmarked)
    except LookupError as exc:
        raise fastapi.HTTPException(status_code=404, detail="manual not found") from exc
    except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
        logger.error("Bookmark update failed: %s", exc)
        raise fastapi.HTTPException(status_code=503, detail="manual persistence is unavailable") from exc
    return {
        "status": "success",
        "manual_id": manual_id,
        "is_bookmarked": is_bookmarked,
    }