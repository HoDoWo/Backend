"""Marketplace search and user listing endpoints."""

from __future__ import annotations

from typing import Literal

import auth
import firebase_store
import schemas
from fastapi import APIRouter, HTTPException, Query, Request

marketplace_router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


@marketplace_router.get("/search", response_model=schemas.MarketplaceSearchResponse)
async def search_marketplace(
    q: str = Query(default="", max_length=120),
    type: Literal["all", "new", "used", "part"] = "all",
    category: str | None = Query(default=None, max_length=80),
    brand: str | None = Query(default=None, max_length=80),
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
            seller_name=str(user.get("name") or user.get("email") or "Community seller"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="marketplace persistence is unavailable") from exc


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
