"""Second-hand listing endpoints (#35): browse, read, edit, mark as sold.

``POST`` (create) and ``DELETE`` stay in ``endpoints/marketplace.py``. Every
route needs a signed-in user; changes are for the listing's owner only.
"""

from typing import Annotated, Any, Literal, Optional

import config
import fastapi
import firebase_store
import listings_service
import logging_config
import schemas
from endpoints import checkout

logger = logging_config.get_logger(__name__)

listings_router = fastapi.APIRouter(prefix="/api/marketplace/listings", tags=["listings"])

ListingId = Annotated[str, fastapi.Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

_UNAVAILABLE = (firebase_store.FirebaseConfigurationError, config.ConfigurationError)


def _run(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except listings_service.ListingError as exc:
        raise fastapi.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except _UNAVAILABLE as exc:
        logger.error("Listings are misconfigured: %s", exc)
        raise fastapi.HTTPException(status_code=503, detail="marketplace persistence is unavailable") from exc


@listings_router.get("", response_model=dict)
async def list_listings(
    request: fastapi.Request,
    q: Annotated[str, fastapi.Query(max_length=120)] = "",
    category: Annotated[Optional[str], fastapi.Query(max_length=80)] = None,
    max_price: Annotated[Optional[float], fastapi.Query(ge=0)] = None,
    status: Optional[Literal["active", "sold"]] = None,
    mine: bool = False,
    cursor: Annotated[Optional[str], fastapi.Query(max_length=64)] = None,
    limit: Annotated[int, fastapi.Query(ge=1, le=listings_service.MAX_PAGE)] = listings_service.DEFAULT_PAGE,
) -> dict[str, Any]:
    """Active listings, or all of the user's with ``mine=true``; newest first, paginated."""
    uid = checkout._uid(request)
    return _run(listings_service.list_listings, uid, q, category, max_price, status, mine, cursor, limit)


@listings_router.get("/{listing_id}", response_model=dict)
async def get_listing(request: fastapi.Request, listing_id: ListingId) -> dict[str, Any]:
    return _run(listings_service.get_listing, checkout._uid(request), listing_id)


@listings_router.patch("/{listing_id}", response_model=dict)
async def update_listing(
    request: fastapi.Request, req: schemas.MarketplaceListingUpdateRequest, listing_id: ListingId
) -> dict[str, Any]:
    """Owner only; a sold listing cannot be edited (409)."""
    return _run(listings_service.update_listing, checkout._uid(request), listing_id,
                req.model_dump(exclude_unset=True))


@listings_router.post("/{listing_id}/sold", response_model=dict)
async def mark_sold(request: fastapi.Request, listing_id: ListingId) -> dict[str, Any]:
    """Owner only; repeating it is harmless."""
    return _run(listings_service.mark_sold, checkout._uid(request), listing_id)
