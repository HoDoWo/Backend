import fastapi
import langchain_protocol
from logging import getLogger
import firebase_store, index, schemas

rate_router = fastapi.APIRouter()
logger = getLogger(__name__)
@rate_router.post("/api/manuals/{manual_id}/rate", response_model=dict)
async def rate_manual(
    manual_id: str, req: schemas.RatingRequest, request: fastapi.Request
) -> dict[str, langchain_protocol.Any]:
    """Toggle or change the authenticated user's like/dislike rating."""
    user = index.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise fastapi.HTTPException(status_code=401, detail="authenticated user id is missing")
    if req.rating not in (-1, 1):
        raise fastapi.HTTPException(status_code=422, detail="rating must be 1 or -1")
    try:
        current_rating = firebase_store.set_rating(uid, manual_id, req.rating)
    except LookupError as exc:
        raise fastapi.HTTPException(status_code=404, detail="manual not found") from exc
    except (firebase_store.FirebaseConfigurationError, RuntimeError, ValueError) as exc:
        logger.error("Manual rating failed: %s", exc)
        raise fastapi.HTTPException(status_code=503, detail="manual persistence is unavailable") from exc
    return {
        "status": "success",
        "manual_id": manual_id,
        "current_rating": current_rating,
    }