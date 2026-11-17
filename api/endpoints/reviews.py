"""Product reviews: read by every signed-in user, written only by who bought the product."""

from typing import Annotated, Any, Literal

import config
import fastapi
import firebase_store
import logging_config
import reviews_service
import schemas
from endpoints import checkout

logger = logging_config.get_logger(__name__)

reviews_router = fastapi.APIRouter(prefix="/api/marketplace/products", tags=["reviews"])

ProductType = Literal["new", "used", "part"]
ProductId = Annotated[str, fastapi.Path(min_length=1, max_length=128, pattern=r"^[^/]+$")]

_UNAVAILABLE = (firebase_store.FirebaseConfigurationError, config.ConfigurationError)


def _run(action, *args):
    try:
        return action(*args)
    except reviews_service.ReviewError as exc:
        raise fastapi.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except _UNAVAILABLE as exc:
        logger.error("Reviews are misconfigured: %s", exc)
        raise fastapi.HTTPException(status_code=503, detail="reviews are unavailable") from exc


@reviews_router.get("/{product_type}/{product_id}/reviews", response_model=dict)
async def list_reviews(request: fastapi.Request, product_type: ProductType, product_id: ProductId) -> dict[str, Any]:
    """Average, stars and the newest reviews; whether the user can write one."""
    return _run(reviews_service.list_reviews, checkout._uid(request), product_type, product_id)


@reviews_router.put("/{product_type}/{product_id}/reviews/mine", response_model=dict)
@config.forum_rate_limit
async def save_my_review(
    request: fastapi.Request, req: schemas.ReviewRequest, product_type: ProductType, product_id: ProductId,
) -> dict[str, Any]:
    """Write or change the user's review (only after buying the product)."""
    return _run(
        reviews_service.save_review, request.state.user, checkout._uid(request),
        product_type, product_id, req.rating, req.text,
    )


@reviews_router.delete("/{product_type}/{product_id}/reviews/mine", status_code=204)
@config.forum_rate_limit
async def delete_my_review(request: fastapi.Request, product_type: ProductType, product_id: ProductId) -> None:
    _run(reviews_service.delete_review, checkout._uid(request), product_type, product_id)
