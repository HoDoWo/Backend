"""Marketplace search and user listing endpoints."""

from __future__ import annotations

import pathlib
from typing import Literal

import auth
import firebase_store
import request_helpers
import schemas
from fastapi import APIRouter, HTTPException, Query, Request, Response

marketplace_router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


@marketplace_router.get("/search", response_model=schemas.MarketplaceSearchResponse)
async def search_marketplace(
    q: str = Query(default="", max_length=120),
    type: Literal["all", "new", "used", "part", "appliance"] = "all",
    category: str | None = Query(default=None, max_length=80),
    brand: str | None = Query(default=None, max_length=80),
    condition: str | None = Query(default=None, max_length=50),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> schemas.MarketplaceSearchResponse:
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(status_code=422, detail="min_price cannot exceed max_price")
    try:
        result = firebase_store.search_marketplace(
            query=q,
            item_type=type,
            category=category,
            brand=brand,
            condition=condition,
            min_price=min_price,
            max_price=max_price,
            cursor=cursor,
            limit=limit,
        )
    except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="marketplace persistence is unavailable") from exc
    return schemas.MarketplaceSearchResponse(**result)


@marketplace_router.post(
    "/listings",
    response_model=schemas.MarketplaceListingResponse,
    status_code=201,
)
async def create_listing(payload: schemas.MarketplaceListingRequest, request: Request):
    user = auth.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="authenticated user id is missing")
    try:
        return firebase_store.create_marketplace_listing(
            uid,
            payload.model_dump(mode="json"),
            # The email is never shown to other users.
            seller_name=str(user.get("name") or "").strip()[:80] or "Community seller",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="marketplace persistence is unavailable") from exc


@marketplace_router.post("/listings/images", status_code=201)
async def upload_listing_image(request: Request):
    """Store one sell-form photo and return the URL to put in ``image_urls``.

    Photos are stored server-side so the app does not need Firebase Storage
    rules or a configured bucket to publish a listing.
    """
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="send the photo as multipart/form-data")

    form = await request.form()
    image = request_helpers.find_uploaded_image(form)
    if image is None:
        raise HTTPException(status_code=422, detail="an image upload is required")

    user = auth.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="authenticated user id is missing")

    image_path = await request_helpers.save_uploaded_image(image, "listing.jpg")
    try:
        image_bytes = pathlib.Path(image_path).read_bytes()
        image_id = firebase_store.upload_listing_image(
            uid, image_bytes, image.content_type or "image/jpeg"
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="image storage is unavailable") from exc
    finally:
        pathlib.Path(image_path).unlink(missing_ok=True)

    base_url = str(request.base_url).rstrip("/")
    return {"url": f"{base_url}/api/marketplace/listings/images/{image_id}"}


@marketplace_router.get("/listings/images/{image_id}")
async def get_listing_image(image_id: str):
    """Serve a stored listing photo. Public: marketplace photos are not private."""
    try:
        found = firebase_store.get_listing_image(image_id)
    except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="image storage is unavailable") from exc
    if found is None:
        raise HTTPException(status_code=404, detail="image not found")

    image_bytes, content_type = found
    return Response(
        content=image_bytes,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@marketplace_router.delete("/listings/{listing_id}", status_code=204)
async def delete_listing(listing_id: str, request: Request):
    user = auth.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    try:
        deleted = firebase_store.delete_marketplace_listing(uid, listing_id)
    except LookupError as exc:
        raise HTTPException(status_code=403, detail="listing is not owned by the current user") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="listing not found")
