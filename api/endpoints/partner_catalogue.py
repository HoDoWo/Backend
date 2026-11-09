"""Official partner product endpoints (#39).

Reads are open to every signed-in user. ``POST`` and ``PATCH`` are for
partner accounts (their own products) and admins; anyone else gets 403.
"""

from typing import Annotated, Any, Optional

import config
import fastapi
import firebase_store
import logging_config
import partner_products
import schemas
from endpoints import checkout

logger = logging_config.get_logger(__name__)

partner_products_router = fastapi.APIRouter(prefix="/api/marketplace/products", tags=["partner products"])

ProductId = Annotated[str, fastapi.Path(min_length=1, max_length=130, pattern=r"^[A-Za-z0-9._-]+$")]
Filter = Annotated[Optional[str], fastapi.Query(max_length=80)]

_UNAVAILABLE = (firebase_store.FirebaseConfigurationError, config.ConfigurationError)


def _run(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except partner_products.ProductError as exc:
        raise fastapi.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except _UNAVAILABLE as exc:
        logger.error("The partner catalogue is misconfigured: %s", exc)
        raise fastapi.HTTPException(status_code=503, detail="the catalogue is unavailable") from exc


@partner_products_router.get("", response_model=dict)
async def list_products(
    request: fastapi.Request,
    q: Annotated[str, fastapi.Query(max_length=120)] = "",
    brand: Filter = None,
    category: Filter = None,
    partner: Filter = None,
    cursor: Annotated[Optional[str], fastapi.Query(max_length=130)] = None,
    limit: Annotated[int, fastapi.Query(ge=1, le=partner_products.MAX_PAGE)] = partner_products.DEFAULT_PAGE,
) -> dict[str, Any]:
    """Partner catalogue, filtered and paginated; send ``next_cursor`` back as ``cursor``."""
    checkout._uid(request)
    return _run(partner_products.list_products, request.state.user, q, brand, category, partner, cursor, limit)


@partner_products_router.get("/{product_id}", response_model=dict)
async def get_product(request: fastapi.Request, product_id: ProductId) -> dict[str, Any]:
    checkout._uid(request)
    return _run(partner_products.get_product, request.state.user, product_id)


@partner_products_router.post("", response_model=dict, status_code=201)
async def create_product(request: fastapi.Request, req: schemas.PartnerProductCreateRequest) -> dict[str, Any]:
    uid = checkout._uid(request)
    return _run(partner_products.create_product, request.state.user, uid, req.model_dump())


@partner_products_router.patch("/{product_id}", response_model=dict)
async def update_product(
    request: fastapi.Request, req: schemas.PartnerProductUpdateRequest, product_id: ProductId
) -> dict[str, Any]:
    uid = checkout._uid(request)
    return _run(partner_products.update_product, request.state.user, uid, product_id, req.model_dump(exclude_unset=True))
