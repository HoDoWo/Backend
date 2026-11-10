"""AI endpoints used by the marketplace sell form.

The routes live under the listings prefix but are separate from listing CRUD:
they never write to Firestore. Authentication is enforced by the global JWT
middleware in ``index.py``.
"""

from __future__ import annotations

import pathlib

import fastapi

import config
import listing_ai
import logging_config
import request_helpers
import schemas


logger = logging_config.get_logger(__name__)

listing_ai_router = fastapi.APIRouter(
    prefix="/api/marketplace/listings", tags=["listings-ai"]
)


@listing_ai_router.post("/scan-label", response_model=schemas.ListingScanResponse)
@config.ai_rate_limit
async def scan_label(request: fastapi.Request) -> dict:
    """Read brand, model and part number from a photo of the label."""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise fastapi.HTTPException(
            status_code=415, detail="send the label photo as multipart/form-data"
        )

    form = await request.form()
    image = request_helpers.find_uploaded_image(form)
    if image is None:
        logger.warning("Rejected label scan without an image")
        raise fastapi.HTTPException(status_code=422, detail="an image upload is required")

    user_context = str(form.get("context") or "").strip()[:300]
    image_path = await request_helpers.save_uploaded_image(image, "listing-label.jpg")
    try:
        return listing_ai.scan_model_label(image_path, user_context)
    except (RuntimeError, FileNotFoundError) as exc:
        logger.error("Label scan failed: %s", exc)
        raise fastapi.HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        pathlib.Path(image_path).unlink(missing_ok=True)


@listing_ai_router.post("/polish", response_model=schemas.ListingPolishResponse)
@config.ai_rate_limit
async def polish_listing(
    payload: schemas.ListingPolishRequest, request: fastapi.Request
) -> dict:
    """Rewrite the seller's narrative so it reads better."""
    try:
        return {"description": listing_ai.polish_listing(payload.description)}
    except RuntimeError as exc:
        logger.error("Listing polish failed: %s", exc)
        raise fastapi.HTTPException(status_code=502, detail=str(exc)) from exc


@listing_ai_router.post("/estimate", response_model=schemas.ListingEstimateResponse)
@config.ai_rate_limit
async def estimate_listing(
    payload: schemas.ListingEstimateRequest, request: fastapi.Request
) -> dict:
    """Estimate a fair USD price range from every field already filled in."""
    try:
        return listing_ai.estimate_listing(payload.model_dump())
    except RuntimeError as exc:
        logger.error("Listing price estimate failed: %s", exc)
        raise fastapi.HTTPException(status_code=502, detail=str(exc)) from exc
